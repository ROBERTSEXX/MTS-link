"""Ссылка на мероприятие → ссылки на записи его сессий."""

from __future__ import annotations

import logging
from typing import Any

from mtslink_downloader.domain.errors import MtsLinkError
from mtslink_downloader.domain.links import LinkKind, RecordingLink, parse_link
from mtslink_downloader.domain.models import DownloadJob, MediaAccess, SessionCookie
from mtslink_downloader.infrastructure.http import HttpClient

LOG = logging.getLogger(__name__)


class EventResolver:
    """Раскрывает ``/j/<org>/<event>`` в задания на записи через ``/api/event``.

    Одно мероприятие может содержать несколько сессий; каждая с записью
    становится отдельным заданием с именем сессии.
    """

    def __init__(self, http: HttpClient, cookies: tuple[SessionCookie, ...] = ()) -> None:
        self._http = http
        self._cookies = cookies

    def expand(self, jobs: list[DownloadJob]) -> tuple[list[DownloadJob], list[str]]:
        result: list[DownloadJob] = []
        warnings: list[str] = []
        for job in jobs:
            if job.link.kind is not LinkKind.EVENT:
                result.append(job)
                continue
            try:
                sessions = self._sessions(job.link)
            except MtsLinkError as exc:
                warnings.append(f"{job.link.url}: {exc}")
                result.append(job)  # пусть попробуют браузерные способы
                continue
            if not sessions:
                warnings.append(f"{job.link.url}: у мероприятия нет завершённых сессий")
                continue
            for session_id, name in sessions:
                link = parse_link(f"{job.link.url.rstrip('/')}/record-new/{session_id}")
                title = job.name or (name if len(sessions) > 1 else None)
                result.append(DownloadJob(link=link, name=title))
            LOG.info("Мероприятие %s: сессий %d", job.link.event_id, len(sessions))
        return result, warnings

    def _sessions(self, link: RecordingLink) -> list[tuple[str, str]]:
        url = f"{link.origin}/api/event/{link.event_id}"
        data: Any = self._http.get_json(url, MediaAccess(link.url, cookies=self._cookies).headers_for(url))
        sessions = data.get("eventSessions") if isinstance(data, dict) else None
        found = []
        for session in sessions or []:
            if not isinstance(session, dict) or session.get("id") is None:
                continue
            if session.get("status") not in (None, "STOP"):
                continue
            found.append((str(session["id"]), str(session.get("name") or "")))
        return found
