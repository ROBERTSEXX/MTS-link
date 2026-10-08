"""Порты: узкие контракты, которые реализует инфраструктура.

Каждый протокол описывает одну обязанность (принцип разделения
интерфейсов), а сценарии зависят только от них (инверсия зависимостей).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from mtslink_downloader.application.export_plan import ExportPlan
from mtslink_downloader.domain.links import RecordingLink
from mtslink_downloader.domain.models import (
    BatchSummary,
    DownloadJob,
    DownloadSettings,
    JobResult,
    RecordDocument,
    Recording,
)


class RecordSource(Protocol):
    """Получает JSON-описание записи (API, браузер и т.д.)."""

    name: str

    def fetch(self, link: RecordingLink) -> RecordDocument: ...


class RecordParser(Protocol):
    """Превращает JSON записи в доменную модель ``Recording``."""

    def parse(self, document: RecordDocument) -> Recording: ...


class RecordingEnricher(Protocol):
    """Дополняет модель метаданными медиа: кодек, размер, длительность."""

    def enrich(self, recording: Recording, document: RecordDocument) -> None: ...


class RecordingExporter(Protocol):
    """Скачивает и собирает файлы по готовому плану."""

    def export(
        self,
        recording: Recording,
        plan: ExportPlan,
        document: RecordDocument,
        job: DownloadJob,
        settings: DownloadSettings,
    ) -> list[Path]: ...


class DownloadStrategy(Protocol):
    """Один способ скачать ссылку целиком."""

    name: str

    def supports(self, link: RecordingLink) -> bool: ...

    def download(self, job: DownloadJob, settings: DownloadSettings) -> list[Path]: ...


class CompletedCatalog(Protocol):
    """Помнит уже скачанные ссылки, чтобы повторный запуск их пропускал."""

    def completed_outputs(self, url: str) -> list[Path] | None: ...

    def mark_completed(self, url: str, strategy: str, outputs: Sequence[Path]) -> None: ...


class ProgressReporter(Protocol):
    """Сообщает пользователю о ходе пакетной загрузки."""

    def job_started(self, job: DownloadJob, index: int, total: int) -> None: ...

    def strategy_started(self, job: DownloadJob, strategy: str) -> None: ...

    def strategy_failed(self, job: DownloadJob, strategy: str, message: str) -> None: ...

    def job_finished(self, result: JobResult) -> None: ...


class BatchReportWriter(Protocol):
    """Сохраняет итог пакета (отчёт и список неудачных ссылок)."""

    def write(self, summary: BatchSummary) -> None: ...
