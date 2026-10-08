"""Скачивание одного медиасегмента всеми доступными способами."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from pathlib import Path

from mtslink_downloader.domain.errors import MediaProcessingError, MtsLinkError
from mtslink_downloader.domain.models import MediaAccess, MediaSegment
from mtslink_downloader.infrastructure.http import HttpClient
from mtslink_downloader.infrastructure.media.ffmpeg import Ffmpeg, remote_input_args

LOG = logging.getLogger(__name__)


def cache_name(url: str, suffix: str) -> str:
    """Стабильное имя файла кэша: подписи в query не влияют на имя."""

    stable = url.split("?", 1)[0]
    return hashlib.sha1(stable.encode("utf-8")).hexdigest()[:20] + suffix


class SegmentFetcher:
    """Получает локальную копию сегмента, перебирая способы по очереди.

    1. ffmpeg читает прямой MP4 и копирует дорожки без перекодирования;
    2. ffmpeg читает HLS-плейлист того же сегмента;
    3. файл скачивается байтами (с докачкой через Range) и затем
       перепаковывается ffmpeg локально.

    Готовые сегменты кэшируются в рабочей папке задания, поэтому повторный
    запуск после обрыва не скачивает их заново.
    """

    def __init__(self, ffmpeg: Ffmpeg, http: HttpClient) -> None:
        self._ffmpeg = ffmpeg
        self._http = http

    def fetch_media(self, segment: MediaSegment, cache_dir: Path, access: MediaAccess) -> Path:
        """Скачать сегмент с видео и/или звуком (``.mp4``)."""

        return self._fetch(segment, cache_dir, access, audio_only=False)

    def fetch_audio(self, segment: MediaSegment, cache_dir: Path, access: MediaAccess) -> Path:
        """Скачать только звуковую дорожку сегмента (``.m4a``)."""

        return self._fetch(segment, cache_dir, access, audio_only=True)

    def fetch_file(self, url: str, destination: Path, access: MediaAccess) -> Path:
        """Скачать обычный файл (PDF, JPG) как есть."""

        if destination.exists() and destination.stat().st_size > 0:
            return destination
        self._http.download(url, destination, access.headers_for(url))
        return destination

    def _fetch(
        self, segment: MediaSegment, cache_dir: Path, access: MediaAccess, audio_only: bool
    ) -> Path:
        if not segment.candidate_urls:
            raise MediaProcessingError("У сегмента нет ни одного URL.")
        cache_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".m4a" if audio_only else ".mp4"
        destination = cache_dir / cache_name(segment.any_url, suffix)
        if destination.exists() and self._is_valid(destination, audio_only):
            LOG.debug("Сегмент уже скачан: %s", destination.name)
            return destination

        errors: list[str] = []
        audio_only_backup: Path | None = None
        for url in segment.candidate_urls:
            for method in (self._via_ffmpeg, self._via_http):
                if method is self._via_http and _is_playlist(url):
                    continue
                temp = destination.with_name(destination.stem + ".tmp" + suffix)
                temp.unlink(missing_ok=True)
                try:
                    method(url, temp, access, audio_only, cache_dir)
                except MtsLinkError as exc:
                    errors.append(str(exc))
                    temp.unlink(missing_ok=True)
                    continue
                types = self._ffmpeg.stream_types(temp)
                wanted = "audio" if audio_only else "video"
                if wanted in types:
                    os.replace(temp, destination)
                    if audio_only_backup:
                        audio_only_backup.unlink(missing_ok=True)
                    return destination
                if not audio_only and "audio" in types and audio_only_backup is None:
                    # Возможно, у запасного URL есть видео; звук сохраняем на случай,
                    # если его нет нигде: тогда к нему добавится чёрный кадр.
                    audio_only_backup = destination.with_name(destination.stem + ".audio" + suffix)
                    os.replace(temp, audio_only_backup)
                    break
                temp.unlink(missing_ok=True)
                errors.append(f"{_short(url)}: нет нужных дорожек ({', '.join(sorted(types)) or '—'})")

        if audio_only_backup is not None:
            os.replace(audio_only_backup, destination)
            LOG.warning("Во всех источниках сегмента только звук — будет добавлен чёрный кадр")
            return destination
        raise MediaProcessingError(
            "Сегмент не скачан ни одним способом: " + "; ".join(errors[-4:])
        )

    def _via_ffmpeg(
        self, url: str, temp: Path, access: MediaAccess, audio_only: bool, cache_dir: Path
    ) -> None:
        maps = ["-map", "0:a:0", "-vn", "-c:a", "copy"] if audio_only else [
            "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy"]
        self._ffmpeg.run(
            [*remote_input_args(url, access.headers_for(url)), *maps,
             "-movflags", "+faststart", "-y", str(temp)],
            f"Скачивание {_short(url).rsplit('/', 1)[-1]}",
        )
        if not temp.exists() or temp.stat().st_size == 0:
            raise MediaProcessingError(f"ffmpeg создал пустой файл для {_short(url)}")

    def _via_http(
        self, url: str, temp: Path, access: MediaAccess, audio_only: bool, cache_dir: Path
    ) -> None:
        raw = cache_dir / cache_name(url, ".raw")
        if not (raw.exists() and raw.stat().st_size > 0):
            LOG.info("Прямое скачивание байтами: %s", _short(url).rsplit("/", 1)[-1])
            self._http.download(url, raw, access.headers_for(url))
        maps = ["-map", "0:a:0", "-vn", "-c:a", "copy"] if audio_only else [
            "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy"]
        try:
            self._ffmpeg.run(
                ["-i", str(raw), *maps, "-movflags", "+faststart", "-y", str(temp)],
                "Перепаковка скачанного файла",
            )
        finally:
            if temp.exists() and temp.stat().st_size > 0:
                raw.unlink(missing_ok=True)

    def _is_valid(self, path: Path, audio_only: bool) -> bool:
        types = self._ffmpeg.stream_types(path)
        return "audio" in types if audio_only else bool(types & {"video", "audio"})


def copy_atomically(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    shutil.copy2(source, partial)
    os.replace(partial, destination)


def _is_playlist(url: str) -> bool:
    return _short(url).lower().endswith((".m3u8", ".mpd"))


def _short(url: str) -> str:
    return url.split("?", 1)[0]
