"""Цепочки «пробуй следующий способ», не знающие о конкретных способах."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from mtslink_downloader.application.ports import DownloadStrategy, RecordParser, RecordSource
from mtslink_downloader.domain.errors import (
    AccessDeniedError,
    AllStrategiesFailedError,
    MtsLinkError,
    RecordUnavailableError,
    StrategyAttempt,
)
from mtslink_downloader.domain.links import RecordingLink
from mtslink_downloader.domain.models import (
    DownloadJob,
    DownloadSettings,
    RecordDocument,
    Recording,
)

LOG = logging.getLogger(__name__)

StrategyListener = Callable[[str, str | None], None]
"""Вызывается с именем способа и ``None`` при старте или текстом ошибки."""


@dataclass(frozen=True)
class FallbackOutcome:
    strategy: str
    outputs: tuple[Path, ...]
    attempts: tuple[StrategyAttempt, ...]


class FallbackDownloader:
    """Перебирает способы скачивания по порядку до первого успеха.

    Новый способ добавляется в список при сборке приложения — сам класс при
    этом не меняется (принцип открытости/закрытости).
    """

    def __init__(self, strategies: Sequence[DownloadStrategy]) -> None:
        if not strategies:
            raise ValueError("Нужен хотя бы один способ скачивания")
        self._strategies = tuple(strategies)

    @property
    def strategy_names(self) -> tuple[str, ...]:
        return tuple(strategy.name for strategy in self._strategies)

    def download(
        self,
        job: DownloadJob,
        settings: DownloadSettings,
        listener: StrategyListener | None = None,
    ) -> FallbackOutcome:
        attempts: list[StrategyAttempt] = []
        for strategy in self._strategies:
            if not strategy.supports(job.link):
                continue
            if listener:
                listener(strategy.name, None)
            try:
                outputs = strategy.download(job, settings)
            except MtsLinkError as exc:
                message = str(exc)
            except Exception as exc:  # noqa: BLE001 - способ не должен ронять весь пакет
                LOG.debug("Способ %s упал с исключением", strategy.name, exc_info=True)
                message = f"{type(exc).__name__}: {exc}"
            else:
                if outputs:
                    attempts.append(StrategyAttempt(strategy.name, True))
                    return FallbackOutcome(strategy.name, tuple(outputs), tuple(attempts))
                message = "способ не создал ни одного файла"
            attempts.append(StrategyAttempt(strategy.name, False, message))
            if listener:
                listener(strategy.name, message)
        raise AllStrategiesFailedError(attempts)


class FallbackRecordSource:
    """Получает описание записи из первого источника, который справился."""

    def __init__(self, sources: Sequence[RecordSource], name: str = "record") -> None:
        if not sources:
            raise ValueError("Нужен хотя бы один источник описания записи")
        self._sources = tuple(sources)
        self.name = name

    def fetch(self, link: RecordingLink) -> RecordDocument:
        errors: list[str] = []
        denied = False
        for source in self._sources:
            try:
                return source.fetch(link)
            except AccessDeniedError as exc:
                denied = True
                errors.append(f"{source.name}: {exc}")
            except MtsLinkError as exc:
                errors.append(f"{source.name}: {exc}")
        message = "; ".join(errors)
        if denied:
            raise AccessDeniedError(message)
        raise RecordUnavailableError(message)


class FallbackRecordParser:
    """Применяет парсеры по очереди, пока один не найдёт медиапотоки."""

    def __init__(self, parsers: Sequence[RecordParser]) -> None:
        if not parsers:
            raise ValueError("Нужен хотя бы один парсер записи")
        self._parsers = tuple(parsers)

    def parse(self, document: RecordDocument) -> Recording:
        errors: list[str] = []
        for parser in self._parsers:
            try:
                recording = parser.parse(document)
            except MtsLinkError as exc:
                errors.append(str(exc))
                continue
            if not recording.is_empty:
                return recording
            errors.append(f"{type(parser).__name__}: медиапотоки не найдены")
        raise RecordUnavailableError(
            "В описании записи не найдено медиа: " + "; ".join(errors)
        )
