"""Запуск Chromium через Playwright с постоянным профилем.

Профиль хранит вход в МТС Линк между ссылками и между запусками, поэтому
для пакета приватных записей войти нужно один раз. Playwright — опциональная
зависимость: без неё браузерные способы сообщают о недоступности, а цепочка
переходит к следующему способу.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mtslink_downloader.domain.errors import StrategyUnavailableError
from mtslink_downloader.domain.models import SessionCookie

LoginPrompt = Callable[[str], None]


@dataclass(frozen=True)
class BrowserOptions:
    headless: bool = True
    profile_dir: Path | None = None
    channel: str | None = None
    executable_path: str | None = None
    navigation_timeout_ms: int = 60_000


@dataclass
class BrowserSession:
    """Открытая страница и её контекст (для cookies и API-запросов)."""

    context: Any
    page: Any

    def cookies(self) -> tuple[SessionCookie, ...]:
        result = []
        for cookie in self.context.cookies():
            result.append(
                SessionCookie(
                    name=str(cookie.get("name", "")),
                    value=str(cookie.get("value", "")),
                    domain=str(cookie.get("domain", "")),
                )
            )
        return tuple(result)

    def user_agent(self) -> str:
        return str(self.page.evaluate("() => navigator.userAgent"))


class PlaywrightBrowser:
    """Фабрика браузерных сессий; одновременно работает одна сессия.

    Постоянный профиль Chromium нельзя открыть дважды, поэтому при
    параллельной загрузке браузерные шаги выполняются по очереди.
    """

    _lock = threading.Lock()

    def __init__(self, options: BrowserOptions) -> None:
        self._options = options

    @property
    def options(self) -> BrowserOptions:
        return self._options

    @contextlib.contextmanager
    def session(self, headless: bool | None = None) -> Iterator[BrowserSession]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise StrategyUnavailableError(
                "Playwright не установлен: pip install playwright && "
                "python -m playwright install chromium"
            ) from exc

        use_headless = self._options.headless if headless is None else headless
        launch_args: dict[str, Any] = {"headless": use_headless}
        if self._options.channel:
            launch_args["channel"] = self._options.channel
        if self._options.executable_path:
            launch_args["executable_path"] = self._options.executable_path

        with self._lock, sync_playwright() as playwright:
            browser = None
            try:
                if self._options.profile_dir:
                    self._options.profile_dir.mkdir(parents=True, exist_ok=True)
                    context = playwright.chromium.launch_persistent_context(
                        str(self._options.profile_dir), **launch_args
                    )
                else:
                    browser = playwright.chromium.launch(**launch_args)
                    context = browser.new_context()
            except Exception as exc:  # noqa: BLE001 - нет браузера/драйвера
                raise StrategyUnavailableError(
                    f"Не удалось запустить браузер: {exc}. "
                    "Выполните python -m playwright install chromium "
                    "или укажите --browser-channel chrome."
                ) from exc
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(self._options.navigation_timeout_ms)
                yield BrowserSession(context=context, page=page)
            finally:
                with contextlib.suppress(Exception):
                    context.close()
                if browser is not None:
                    with contextlib.suppress(Exception):
                        browser.close()


def try_start_playback(page: Any) -> None:
    """Нажать «воспроизвести», чтобы плеер запросил журнал и медиа."""

    selectors = (
        "button[aria-label*='Play' i]",
        "button[aria-label*='Воспроизв' i]",
        "[class*='play' i][role='button']",
        "[class*='PlayButton' i]",
        "video",
    )
    for selector in selectors:
        with contextlib.suppress(Exception):
            locator = page.locator(selector)
            if locator.count():
                locator.first.click(timeout=2_000)
                break
    with contextlib.suppress(Exception):
        page.evaluate(
            "() => document.querySelectorAll('video').forEach(v => {"
            " v.muted = true; const p = v.play(); if (p) p.catch(() => {}); })"
        )
