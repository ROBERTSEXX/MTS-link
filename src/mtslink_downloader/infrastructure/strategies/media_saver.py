"""Сохранение одного найденного медиа-URL в готовый MP4."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from mtslink_downloader.domain.errors import MediaProcessingError, MtsLinkError
from mtslink_downloader.domain.models import MediaAccess
from mtslink_downloader.infrastructure.http import HttpClient
from mtslink_downloader.infrastructure.media.ffmpeg import Ffmpeg, remote_input_args

LOG = logging.getLogger(__name__)


class MediaUrlSaver:
    """Копирует поток по URL (MP4, HLS, DASH) в MP4 без перекодирования.

    Если ffmpeg не справился с прямым файлом, файл скачивается байтами и
    перепаковывается локально.
    """

    def __init__(self, ffmpeg: Ffmpeg, http: HttpClient) -> None:
        self._ffmpeg = ffmpeg
        self._http = http

    def save(self, url: str, destination: Path, access: MediaAccess) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.stem + ".part.mp4")
        errors: list[str] = []
        try:
            self._ffmpeg.run(
                [*remote_input_args(url, access.headers_for(url)),
                 "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
                 "-movflags", "+faststart", "-y", str(partial)],
                f"Сохранение потока {url.split('?', 1)[0].rsplit('/', 1)[-1]}",
            )
            self._validate(partial)
        except MtsLinkError as exc:
            errors.append(str(exc))
            partial.unlink(missing_ok=True)
            if url.split("?", 1)[0].lower().endswith((".m3u8", ".mpd")):
                raise MediaProcessingError("; ".join(errors)) from exc
            raw = destination.with_name(destination.name + ".raw")
            try:
                self._http.download(url, raw, access.headers_for(url))
                self._ffmpeg.run(
                    ["-i", str(raw), "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
                     "-movflags", "+faststart", "-y", str(partial)],
                    "Перепаковка скачанного файла",
                )
                self._validate(partial)
            except MtsLinkError as second:
                partial.unlink(missing_ok=True)
                raise MediaProcessingError(f"{errors[0]}; {second}") from second
            finally:
                raw.unlink(missing_ok=True)
        os.replace(partial, destination)
        return destination

    def _validate(self, path: Path) -> None:
        if not path.exists() or path.stat().st_size == 0:
            raise MediaProcessingError("получен пустой файл")
        types = self._ffmpeg.stream_types(path)
        if not types & {"video", "audio"}:
            raise MediaProcessingError("в файле нет ни видео, ни звука")
        if self._ffmpeg.duration(path) <= 0.5:
            raise MediaProcessingError("файл слишком короткий")
