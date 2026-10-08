"""Иерархия ошибок, сообщения которых можно показывать пользователю."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


class MtsLinkError(Exception):
    """Базовая ошибка загрузчика с понятным пользователю текстом."""


class InvalidLinkError(MtsLinkError):
    """Строка не похожа на поддерживаемую ссылку."""


class AccessDeniedError(MtsLinkError):
    """Сервис требует авторизацию или отказал в доступе к записи."""


class RecordUnavailableError(MtsLinkError):
    """Описание записи не получено или в нём нет медиаданных."""


class StrategyUnavailableError(MtsLinkError):
    """Способ скачивания не может работать в текущем окружении.

    Например, не установлен Playwright или yt-dlp. Такая ошибка не означает,
    что запись недоступна: цепочка просто переходит к следующему способу.
    """


class MediaProcessingError(MtsLinkError):
    """ffmpeg/ffprobe или сетевое скачивание медиафайла завершились ошибкой."""


@dataclass(frozen=True)
class StrategyAttempt:
    """Результат одной попытки скачать ссылку конкретным способом."""

    strategy: str
    succeeded: bool
    message: str = ""


class AllStrategiesFailedError(MtsLinkError):
    """Ни один из доступных способов не смог скачать ссылку."""

    def __init__(self, attempts: Sequence[StrategyAttempt]) -> None:
        self.attempts = tuple(attempts)
        if self.attempts:
            details = "; ".join(f"{item.strategy}: {item.message}" for item in self.attempts)
        else:
            details = "нет способов, поддерживающих эту ссылку"
        super().__init__(f"Не удалось скачать ни одним способом ({details})")
