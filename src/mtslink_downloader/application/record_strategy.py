"""Способ скачивания через JSON-журнал записи МТС Линк."""

from __future__ import annotations

from pathlib import Path

from mtslink_downloader.application.export_plan import ExportPlanner
from mtslink_downloader.application.ports import (
    RecordingEnricher,
    RecordingExporter,
    RecordParser,
    RecordSource,
)
from mtslink_downloader.domain.links import RecordingLink
from mtslink_downloader.domain.models import DownloadJob, DownloadSettings


class RecordTimelineStrategy:
    """Описание записи → модель → план → сборка файлов через ffmpeg.

    Источник описания (прямой API или браузер) подставляется снаружи, поэтому
    один класс даёт сразу несколько способов скачивания с разной авторизацией.
    """

    def __init__(
        self,
        name: str,
        source: RecordSource,
        parser: RecordParser,
        enricher: RecordingEnricher,
        planner: ExportPlanner,
        exporter: RecordingExporter,
    ) -> None:
        self.name = name
        self._source = source
        self._parser = parser
        self._enricher = enricher
        self._planner = planner
        self._exporter = exporter

    def supports(self, link: RecordingLink) -> bool:
        return link.is_recording

    def download(self, job: DownloadJob, settings: DownloadSettings) -> list[Path]:
        document = self._source.fetch(job.link)
        recording = self._parser.parse(document)
        self._enricher.enrich(recording, document)
        plan = self._planner.plan(recording, settings.mode)
        return self._exporter.export(recording, plan, document, job, settings)
