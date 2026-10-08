"""Разбор ссылок МТС Линк без обращения к сети."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse

from mtslink_downloader.domain.errors import InvalidLinkError

# Поддерживаются все известные варианты страницы записи:
#   /j/<org>/<event>/record-new/<session>                 (публичная ссылка)
#   /<org>/<room>/record-new/<session>                    (быстрая встреча)
#   /<org>/<room>/record-new/<session>/record-file/<id>   (обычная запись)
# Префикс пути не проверяется жёстко: МТС Линк встречается и на белых
# доменах компаний, и на старом домене webinar.ru.
_RECORD_PATH_RE = re.compile(
    r"/record-new/(?P<session>\d+)(?:/record-file/(?P<record>\d+))?/?$"
)
# Страница мероприятия без записи: /j/<org>/<event> или /<org>/<event>.
# Её сессии (и записи) приходится спрашивать у API.
_EVENT_PATH_RE = re.compile(r"^/(?:j/)?(?P<org>\d+)/(?P<event>\d+)/?$")
_MEDIA_SUFFIXES = (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".m3u8", ".mpd", ".m4a", ".mp3")


class LinkKind(Enum):
    """Тип ссылки определяет, какие способы скачивания к ней применимы."""

    RECORDING = "recording"
    EVENT = "event"
    DIRECT_MEDIA = "direct-media"
    PAGE = "page"


@dataclass(frozen=True)
class RecordingLink:
    """Нормализованная ссылка из списка пользователя."""

    url: str
    kind: LinkKind
    origin: str
    event_session_id: str | None = None
    record_file_id: str | None = None
    event_id: str | None = None

    @property
    def is_recording(self) -> bool:
        return self.kind is LinkKind.RECORDING

    @property
    def identifier(self) -> str:
        """Короткий стабильный идентификатор для имён файлов и отчётов."""

        if self.record_file_id:
            return f"{self.event_session_id}-{self.record_file_id}"
        if self.event_session_id:
            return self.event_session_id
        path = urlparse(self.url).path.rstrip("/")
        tail = path.rsplit("/", 1)[-1] if path else ""
        return re.sub(r"[^\w.-]+", "_", tail)[:60] or "video"


def parse_link(raw: str) -> RecordingLink:
    """Проверить ссылку и определить её тип.

    Ссылка на запись МТС Линк распознаётся по ``record-new/<id>``. Ссылка,
    путь которой оканчивается расширением медиафайла, считается прямой. Любая
    другая http(s)-страница остаётся ``PAGE``: для неё работают только
    браузерный перехват и yt-dlp.
    """

    url = raw.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise InvalidLinkError(f"Ссылка должна начинаться с http:// или https://: {raw!r}")

    origin = f"{parsed.scheme}://{parsed.netloc}"
    match = _RECORD_PATH_RE.search(parsed.path)
    if match:
        return RecordingLink(
            url=url,
            kind=LinkKind.RECORDING,
            origin=origin,
            event_session_id=match.group("session"),
            record_file_id=match.group("record"),
        )
    event = _EVENT_PATH_RE.match(parsed.path)
    if event:
        return RecordingLink(url=url, kind=LinkKind.EVENT, origin=origin, event_id=event.group("event"))
    if parsed.path.lower().endswith(_MEDIA_SUFFIXES):
        return RecordingLink(url=url, kind=LinkKind.DIRECT_MEDIA, origin=origin)
    return RecordingLink(url=url, kind=LinkKind.PAGE, origin=origin)
