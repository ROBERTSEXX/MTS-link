"""Получение журнала записи прямым HTTP-запросом к API МТС Линк."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from mtslink_downloader.domain.errors import AccessDeniedError, MtsLinkError, RecordUnavailableError
from mtslink_downloader.domain.links import RecordingLink
from mtslink_downloader.domain.models import MediaAccess, RecordDocument, SessionCookie
from mtslink_downloader.infrastructure.http import HttpClient
from mtslink_downloader.infrastructure.sources.endpoints import (
    DEFAULT_API_HOSTS,
    looks_like_record,
    record_endpoints,
)

LOG = logging.getLogger(__name__)


class ApiRecordSource:
    """Перебирает все известные API-адреса записи без браузера.

    Для приватных записей передаются cookies пользователя: ``sessionId`` из
    браузера или весь cookies.txt.
    """

    name = "api"

    def __init__(
        self,
        http: HttpClient,
        cookies: tuple[SessionCookie, ...] = (),
        api_hosts: tuple[str, ...] = DEFAULT_API_HOSTS,
        session_id: str | None = None,
    ) -> None:
        self._http = http
        self._cookies = cookies
        self._api_hosts = api_hosts
        self._session_id = session_id

    def fetch(self, link: RecordingLink) -> RecordDocument:
        cookies = self._cookies
        host = urlparse(link.url).hostname
        if self._session_id and host:
            # sessionId относится к сайту записи: для корпоративного домена
            # МТС Линк cookie нужна именно на хосте ссылки.
            cookies = (*cookies, SessionCookie("sessionId", self._session_id, host))
        access = MediaAccess(referer=link.url, cookies=cookies)
        endpoints = record_endpoints(link, self._api_hosts)
        if not endpoints:
            raise RecordUnavailableError("В ссылке нет идентификатора записи.")

        errors: list[str] = []
        denied = False
        for endpoint in endpoints:
            try:
                data = self._http.get_json(endpoint, access.headers_for(endpoint))
            except AccessDeniedError as exc:
                denied = True
                errors.append(str(exc))
                continue
            except MtsLinkError as exc:
                errors.append(str(exc))
                continue
            if looks_like_record(data):
                LOG.info("Журнал записи получен через API: %s", endpoint.split("?", 1)[0])
                return RecordDocument(data=data, source=f"api {endpoint}", access=access)
            errors.append(f"{endpoint.split('?', 1)[0]}: ответ без eventLogs")

        if denied:
            hint = (
                "Запись закрыта. Передайте --session-id (cookie sessionId из браузера), "
                "--cookies cookies.txt или используйте вход через браузер (--login)."
            )
            raise AccessDeniedError(hint)
        raise RecordUnavailableError("API не вернул журнал записи: " + "; ".join(errors[-3:]))
