"""Все известные адреса API, которые отдают JSON-журнал записи."""

from __future__ import annotations

import re

from mtslink_downloader.domain.links import RecordingLink

DEFAULT_API_HOSTS = ("https://my.mts-link.ru", "https://gw.mts-link.ru")

# Ответы с журналом записи, которые запрашивает веб-плеер.
RECORD_RESPONSE_RE = re.compile(
    r"/api/(?:eventsessions/\d+/record|event-sessions/\d+/record-files/\d+/flow)"
)


def record_endpoints(
    link: RecordingLink, api_hosts: tuple[str, ...] = DEFAULT_API_HOSTS
) -> list[str]:
    """Кандидаты в порядке убывания специфичности.

    * ``/api/event-sessions/<id>/record-files/<id>/flow`` — запись-файл
      (способ из mtslinker/mtser);
    * ``/api/eventsessions/<id>/record`` — запись сессии/быстрой встречи
      (способ из download_mts_link.py через gw.mts-link.ru).

    Хост самой ссылки идёт первым: так поддерживаются белые домены компаний.
    """

    if not link.event_session_id:
        return []
    hosts = list(dict.fromkeys((link.origin, *api_hosts)))
    session = link.event_session_id
    paths: list[str] = []
    if link.record_file_id:
        paths.append(
            f"/api/event-sessions/{session}/record-files/{link.record_file_id}/flow"
            "?withoutCuts=false"
        )
    paths.append(f"/api/eventsessions/{session}/record?withoutCuts=false")

    endpoints: list[str] = []
    for path in paths:
        for host in hosts:
            endpoints.append(host.rstrip("/") + path)
    return list(dict.fromkeys(endpoints))


def looks_like_record(data: object) -> bool:
    """Ответ похож на журнал записи, а не на служебный JSON."""

    return isinstance(data, dict) and isinstance(data.get("eventLogs"), list)
