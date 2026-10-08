"""Загрузка cookies для приватных записей."""

from __future__ import annotations

from http.cookiejar import LoadError, MozillaCookieJar
from pathlib import Path

from mtslink_downloader.domain.errors import InvalidLinkError
from mtslink_downloader.domain.models import SessionCookie

MTS_LINK_DOMAIN = ".mts-link.ru"


def session_cookie(session_id: str) -> SessionCookie:
    """Cookie ``sessionId`` из браузера (способ mtslinker/mtser)."""

    return SessionCookie(name="sessionId", value=session_id.strip(), domain=MTS_LINK_DOMAIN)


def load_cookies_file(path: Path) -> tuple[SessionCookie, ...]:
    """Прочитать cookies.txt в формате Netscape (экспорт расширений браузера)."""

    jar = MozillaCookieJar()
    try:
        jar.load(str(path), ignore_discard=True, ignore_expires=True)
    except (OSError, LoadError) as exc:
        raise InvalidLinkError(f"Не удалось прочитать cookies из {path}: {exc}") from exc
    return tuple(
        SessionCookie(name=cookie.name, value=cookie.value or "", domain=cookie.domain)
        for cookie in jar
    )
