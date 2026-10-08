"""Способ для прямых ссылок на медиафайл или плейлист."""

from __future__ import annotations

from pathlib import Path

from mtslink_downloader.domain.links import LinkKind, RecordingLink
from mtslink_downloader.domain.models import (
    DownloadJob,
    DownloadSettings,
    MediaAccess,
    SessionCookie,
)
from mtslink_downloader.infrastructure.storage.naming import OutputNamer
from mtslink_downloader.infrastructure.strategies.media_saver import MediaUrlSaver


class DirectMediaStrategy:
    """Ссылка уже ведёт на .mp4/.m3u8/.mpd — сохраняем её напрямую."""

    name = "direct"

    def __init__(self, saver: MediaUrlSaver, cookies: tuple[SessionCookie, ...] = ()) -> None:
        self._saver = saver
        self._cookies = cookies

    def supports(self, link: RecordingLink) -> bool:
        return link.kind is LinkKind.DIRECT_MEDIA

    def download(self, job: DownloadJob, settings: DownloadSettings) -> list[Path]:
        destination = OutputNamer.main(OutputNamer(settings.output_dir).stem(job, None))
        if destination.exists() and not settings.overwrite:
            return [destination]
        access = MediaAccess(referer=job.link.origin + "/", cookies=self._cookies)
        return [self._saver.save(job.link.url, destination, access)]
