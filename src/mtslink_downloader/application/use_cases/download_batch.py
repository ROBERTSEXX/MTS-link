"""Сценарий: скачать список ссылок, не останавливаясь на ошибках."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor

from mtslink_downloader.application.fallback import FallbackDownloader
from mtslink_downloader.application.ports import (
    BatchReportWriter,
    CompletedCatalog,
    ProgressReporter,
)
from mtslink_downloader.domain.errors import AllStrategiesFailedError, MtsLinkError
from mtslink_downloader.domain.models import (
    BatchSummary,
    DownloadJob,
    DownloadSettings,
    JobResult,
    JobStatus,
)


class DownloadBatch:
    """Обрабатывает каждую ссылку через цепочку способов скачивания.

    Ошибка одной ссылки не прерывает пакет: она попадает в отчёт и в список
    неудачных ссылок, который можно передать на повторный запуск.
    """

    def __init__(
        self,
        downloader: FallbackDownloader,
        catalog: CompletedCatalog,
        reporter: ProgressReporter,
        report_writer: BatchReportWriter,
    ) -> None:
        self._downloader = downloader
        self._catalog = catalog
        self._reporter = reporter
        self._report_writer = report_writer
        self._lock = threading.Lock()

    def execute(
        self,
        jobs: Sequence[DownloadJob],
        settings: DownloadSettings,
        parallel_jobs: int = 1,
    ) -> BatchSummary:
        if not jobs:
            raise MtsLinkError("Список ссылок пуст.")
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        total = len(jobs)
        workers = max(1, min(parallel_jobs, total))
        if workers == 1:
            results = [self._run_one(job, index, total, settings) for index, job in enumerate(jobs, 1)]
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mtslink") as pool:
                futures = [
                    pool.submit(self._run_one, job, index, total, settings)
                    for index, job in enumerate(jobs, 1)
                ]
                results = [future.result() for future in futures]
        summary = BatchSummary(tuple(results))
        self._report_writer.write(summary)
        return summary

    def _run_one(
        self, job: DownloadJob, index: int, total: int, settings: DownloadSettings
    ) -> JobResult:
        with self._lock:
            self._reporter.job_started(job, index, total)

        if not settings.overwrite:
            existing = self._catalog.completed_outputs(job.link.url)
            if existing:
                return self._finish(
                    JobResult(job=job, status=JobStatus.SKIPPED, outputs=tuple(existing))
                )

        def listener(strategy: str, error: str | None) -> None:
            with self._lock:
                if error is None:
                    self._reporter.strategy_started(job, strategy)
                else:
                    self._reporter.strategy_failed(job, strategy, error)

        try:
            outcome = self._downloader.download(job, settings, listener)
        except AllStrategiesFailedError as exc:
            return self._finish(
                JobResult(job=job, status=JobStatus.FAILED, attempts=exc.attempts, error=str(exc))
            )
        except MtsLinkError as exc:
            return self._finish(JobResult(job=job, status=JobStatus.FAILED, error=str(exc)))

        with self._lock:
            self._catalog.mark_completed(job.link.url, outcome.strategy, outcome.outputs)
        return self._finish(
            JobResult(
                job=job,
                status=JobStatus.DONE,
                strategy=outcome.strategy,
                outputs=outcome.outputs,
                attempts=outcome.attempts,
            )
        )

    def _finish(self, result: JobResult) -> JobResult:
        with self._lock:
            self._reporter.job_finished(result)
        return result
