"""Сценарий: только разобрать запись и показать найденные источники."""

from __future__ import annotations

from dataclasses import dataclass

from mtslink_downloader.application.export_plan import ExportPlan, ExportPlanner
from mtslink_downloader.application.ports import RecordingEnricher, RecordParser, RecordSource
from mtslink_downloader.domain.errors import InvalidLinkError
from mtslink_downloader.domain.models import DownloadJob, ExportMode, Recording


@dataclass(frozen=True)
class Inspection:
    recording: Recording
    plan: ExportPlan
    source: str


class InspectRecording:
    """Получает описание записи без скачивания медиа (режим ``--dry-run``)."""

    def __init__(
        self,
        source: RecordSource,
        parser: RecordParser,
        enricher: RecordingEnricher,
        planner: ExportPlanner,
    ) -> None:
        self._source = source
        self._parser = parser
        self._enricher = enricher
        self._planner = planner

    def execute(self, job: DownloadJob, mode: ExportMode) -> Inspection:
        if not job.link.is_recording:
            raise InvalidLinkError(
                "Предпросмотр доступен только для ссылок на записи МТС Линк (record-new/...)."
            )
        document = self._source.fetch(job.link)
        recording = self._parser.parse(document)
        self._enricher.enrich(recording, document)
        return Inspection(recording, self._planner.plan(recording, mode), document.source)
