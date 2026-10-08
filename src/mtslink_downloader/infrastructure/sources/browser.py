"""Получение журнала записи через настоящий браузер."""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

from mtslink_downloader.domain.errors import AccessDeniedError, RecordUnavailableError
from mtslink_downloader.domain.links import RecordingLink
from mtslink_downloader.domain.models import MediaAccess, RecordDocument
from mtslink_downloader.infrastructure.browser.playwright_browser import (
    BrowserSession,
    LoginPrompt,
    PlaywrightBrowser,
    try_start_playback,
)
from mtslink_downloader.infrastructure.sources.endpoints import (
    DEFAULT_API_HOSTS,
    RECORD_RESPONSE_RE,
    looks_like_record,
    record_endpoints,
)

LOG = logging.getLogger(__name__)


class BrowserRecordSource:
    """Открывает страницу как пользователь и забирает журнал записи.

    1. Слушает ответы, которые запрашивает сам плеер.
    2. Если ответа нет — повторяет запросы ко всем API-адресам из контекста
       браузера (с его cookies).
    3. При ``login_prompt`` открывает окно, ждёт вход и пробует снова.

    Вместе с журналом возвращаются cookies и User-Agent браузера, чтобы
    медиафайлы скачивались с теми же правами.
    """

    name = "browser"

    def __init__(
        self,
        browser: PlaywrightBrowser,
        login_prompt: LoginPrompt | None = None,
        wait_seconds: float = 12.0,
        api_hosts: tuple[str, ...] = DEFAULT_API_HOSTS,
    ) -> None:
        self._browser = browser
        self._login_prompt = login_prompt
        self._wait_seconds = wait_seconds
        self._api_hosts = api_hosts

    def fetch(self, link: RecordingLink) -> RecordDocument:
        document = self._fetch_once(link, headless=None, allow_login=False)
        if document is not None:
            return document
        if self._login_prompt is None:
            raise AccessDeniedError(
                "Браузер не получил журнал записи. Если запись закрыта, запустите с --login "
                "и войдите в МТС Линк в открывшемся окне."
            )
        document = self._fetch_once(link, headless=False, allow_login=True)
        if document is None:
            raise RecordUnavailableError("Журнал записи не получен даже после входа в браузере.")
        return document

    def _fetch_once(
        self, link: RecordingLink, headless: bool | None, allow_login: bool
    ) -> RecordDocument | None:
        with self._browser.session(headless=headless) as session:
            responses: list[Any] = []
            session.page.on(
                "response",
                lambda response: responses.append(response)
                if RECORD_RESPONSE_RE.search(response.url)
                else None,
            )
            LOG.info("Открываю страницу записи в браузере")
            try:
                session.page.goto(link.url, wait_until="domcontentloaded")
            except Exception as exc:  # noqa: BLE001
                raise RecordUnavailableError(f"Страница записи не открылась: {exc}") from exc

            if allow_login and self._login_prompt is not None:
                self._login_prompt(
                    "Войдите в МТС Линк в открытом окне браузера и нажмите Enter здесь"
                )
                responses.clear()
                with contextlib.suppress(Exception):
                    session.page.goto(link.url, wait_until="domcontentloaded")

            data = self._wait_for_player(session, responses)
            if data is None:
                data = self._request_from_context(session, link)
            if data is None:
                return None
            return RecordDocument(
                data=data,
                source="browser",
                access=MediaAccess(
                    referer=link.url,
                    user_agent=session.user_agent(),
                    cookies=session.cookies(),
                ),
            )

    def _wait_for_player(self, session: BrowserSession, responses: list[Any]) -> Any:
        deadline = time.monotonic() + self._wait_seconds
        clicked = False
        while time.monotonic() < deadline:
            for response in list(responses):
                with contextlib.suppress(Exception):
                    data = response.json()
                    if looks_like_record(data):
                        LOG.info("Журнал записи перехвачен из запроса плеера")
                        return data
            if not clicked:
                try_start_playback(session.page)
                clicked = True
            session.page.wait_for_timeout(500)
        return None

    def _request_from_context(self, session: BrowserSession, link: RecordingLink) -> Any:
        for endpoint in record_endpoints(link, self._api_hosts):
            try:
                response = session.context.request.get(
                    endpoint, headers={"Referer": link.url}, timeout=60_000
                )
            except Exception as exc:  # noqa: BLE001
                LOG.debug("Запрос %s из браузера не удался: %s", endpoint, exc)
                continue
            if not response.ok:
                LOG.debug("Браузерный запрос %s: HTTP %s", endpoint, response.status)
                continue
            with contextlib.suppress(Exception):
                data = response.json()
                if looks_like_record(data):
                    LOG.info("Журнал записи получен запросом из браузера")
                    return data
        return None
