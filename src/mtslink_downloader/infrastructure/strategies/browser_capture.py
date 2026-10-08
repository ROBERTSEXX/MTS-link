"""Способ «как плеер»: перехват медиазапросов страницы в браузере."""

from __future__ import annotations

import contextlib
import logging
import re
import time
from pathlib import Path

from mtslink_downloader.domain.errors import MediaProcessingError, MtsLinkError, RecordUnavailableError
from mtslink_downloader.domain.links import LinkKind, RecordingLink
from mtslink_downloader.domain.models import DownloadJob, DownloadSettings, MediaAccess
from mtslink_downloader.infrastructure.browser.playwright_browser import (
    PlaywrightBrowser,
    try_start_playback,
)
from mtslink_downloader.infrastructure.media.ffmpeg import Ffmpeg
from mtslink_downloader.infrastructure.storage.naming import OutputNamer
from mtslink_downloader.infrastructure.strategies.media_saver import MediaUrlSaver

LOG = logging.getLogger(__name__)

_MEDIA_URL_RE = re.compile(r"\.(m3u8|mpd|mp4|m4v|webm|m4a)(?:$|[?#])", re.IGNORECASE)
_MEDIA_TYPES = ("video/", "audio/", "mpegurl", "dash+xml")


def _rank(url: str) -> int:
    path = url.split("?", 1)[0].lower()
    if path.endswith(".m3u8"):
        return 0 if "master" in path or "index" in path or "playlist" in path else 1
    if path.endswith(".mpd"):
        return 2
    return 3


class BrowserCaptureStrategy:
    """Открывает страницу, запускает плеер и сохраняет пойманный поток.

    Подходит для любых страниц со встроенным плеером. Для записей МТС Линк
    это последний рубеж: плеер может запрашивать только часть файлов, поэтому
    среди кандидатов выбирается самый длинный поток.
    """

    name = "sniff"

    def __init__(
        self,
        browser: PlaywrightBrowser,
        ffmpeg: Ffmpeg,
        saver: MediaUrlSaver,
        wait_seconds: float = 20.0,
    ) -> None:
        self._browser = browser
        self._ffmpeg = ffmpeg
        self._saver = saver
        self._wait_seconds = wait_seconds

    def supports(self, link: RecordingLink) -> bool:
        return link.kind in {LinkKind.RECORDING, LinkKind.PAGE}

    def download(self, job: DownloadJob, settings: DownloadSettings) -> list[Path]:
        urls, title, access = self._capture(job.link)
        if not urls:
            raise RecordUnavailableError("плеер не запросил ни одного медиафайла")
        candidates = self._order(urls, access)
        destination = OutputNamer.main(OutputNamer(settings.output_dir).stem(job, title))
        if destination.exists() and not settings.overwrite:
            return [destination]
        errors: list[str] = []
        for url in candidates[:5]:
            try:
                return [self._saver.save(url, destination, access)]
            except MtsLinkError as exc:
                errors.append(str(exc))
        raise MediaProcessingError("пойманные потоки не сохранились: " + "; ".join(errors[-2:]))

    def _capture(self, link: RecordingLink) -> tuple[list[str], str, MediaAccess]:
        found: dict[str, None] = {}

        def remember(url: str) -> None:
            if url.startswith(("http://", "https://")):
                found.setdefault(url, None)

        def on_request(request: object) -> None:
            url = str(getattr(request, "url", ""))
            resource = str(getattr(request, "resource_type", ""))
            if resource == "media" or _MEDIA_URL_RE.search(url):
                remember(url)

        def on_response(response: object) -> None:
            with contextlib.suppress(Exception):
                content_type = str(response.headers.get("content-type", "")).lower()  # type: ignore[attr-defined]
                if any(marker in content_type for marker in _MEDIA_TYPES):
                    remember(str(response.url))  # type: ignore[attr-defined]

        with self._browser.session() as session:
            session.page.on("request", on_request)
            session.page.on("response", on_response)
            try:
                session.page.goto(link.url, wait_until="domcontentloaded")
            except Exception as exc:  # noqa: BLE001
                raise RecordUnavailableError(f"страница не открылась: {exc}") from exc
            deadline = time.monotonic() + self._wait_seconds
            try_start_playback(session.page)
            while time.monotonic() < deadline:
                session.page.wait_for_timeout(1_000)
                with contextlib.suppress(Exception):
                    for src in session.page.evaluate(
                        "() => Array.from(document.querySelectorAll('video, audio, source'))"
                        ".map(e => e.currentSrc || e.src).filter(Boolean)"
                    ):
                        remember(str(src))
                if any(_rank(url) <= 1 for url in found):
                    break
            title = ""
            with contextlib.suppress(Exception):
                title = str(session.page.title()).strip()
            access = MediaAccess(
                referer=link.url, user_agent=session.user_agent(), cookies=session.cookies()
            )
        LOG.info("Перехвачено медиаадресов: %d", len(found))
        return list(found), title, access

    def _order(self, urls: list[str], access: MediaAccess) -> list[str]:
        """Плейлисты первыми, затем файлы по убыванию длительности."""

        def duration(url: str) -> float:
            metadata = self._ffmpeg.probe_remote(url, access.headers_for(url), "v:0")
            value = metadata.get("duration")
            return float(value) if isinstance(value, (int, float)) else 0.0

        playlists = [url for url in urls if _rank(url) <= 2]
        files = sorted((url for url in urls if _rank(url) > 2), key=duration, reverse=True)
        return sorted(playlists, key=_rank) + files
