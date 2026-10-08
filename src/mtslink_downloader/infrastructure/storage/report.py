"""Отчёт о пакете и список неудачных ссылок для повторного запуска."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from mtslink_downloader.domain.models import BatchSummary, JobStatus


class JsonBatchReportWriter:
    """Пишет ``mtslink-report.json`` и ``mtslink-failed.txt`` в папку пакета.

    ``mtslink-failed.txt`` имеет формат входного списка, поэтому повтор
    выглядит как ``mtslink-dl -i downloads/mtslink-failed.txt``.
    """

    REPORT_NAME = "mtslink-report.json"
    FAILED_NAME = "mtslink-failed.txt"

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir

    def write(self, summary: BatchSummary) -> None:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        report = {
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "done": summary.count(JobStatus.DONE),
            "skipped": summary.count(JobStatus.SKIPPED),
            "failed": summary.count(JobStatus.FAILED),
            "items": [
                {
                    "url": result.job.link.url,
                    "name": result.job.name,
                    "status": result.status.value,
                    "strategy": result.strategy,
                    "outputs": [str(path) for path in result.outputs],
                    "attempts": [
                        {"strategy": item.strategy, "ok": item.succeeded, "message": item.message}
                        for item in result.attempts
                    ],
                    "error": result.error,
                }
                for result in summary.results
            ],
        }
        (self._output_dir / self.REPORT_NAME).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        failed_path = self._output_dir / self.FAILED_NAME
        if summary.failed:
            lines = ["# Ссылки, которые не удалось скачать. Повтор: mtslink-dl -i " + failed_path.name]
            for result in summary.failed:
                lines.append(
                    f"{result.job.link.url} {result.job.name}" if result.job.name else result.job.link.url
                )
            failed_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            failed_path.unlink(missing_ok=True)
