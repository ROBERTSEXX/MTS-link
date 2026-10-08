"""Вывод хода загрузки и каталога записи в терминал."""

from __future__ import annotations

import sys
from typing import TextIO

from mtslink_downloader.application.use_cases.inspect_recording import Inspection
from mtslink_downloader.domain.models import (
    BatchSummary,
    DownloadJob,
    JobResult,
    JobStatus,
)
from mtslink_downloader.infrastructure.media.ffmpeg import format_duration


class ConsoleReporter:
    """Печатает строки вида ``[2/10] …`` — читаемо и при параллельной работе."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout
        self._positions: dict[int, str] = {}

    def _prefix(self, job: DownloadJob) -> str:
        return self._positions.get(id(job), "[?]")

    def _print(self, text: str) -> None:
        print(text, file=self._stream, flush=True)

    def job_started(self, job: DownloadJob, index: int, total: int) -> None:
        self._positions[id(job)] = f"[{index}/{total}]"
        name = f" ({job.name})" if job.name else ""
        self._print(f"\n{self._prefix(job)} {job.link.url}{name}")

    def strategy_started(self, job: DownloadJob, strategy: str) -> None:
        self._print(f"{self._prefix(job)}   → способ «{strategy}»…")

    def strategy_failed(self, job: DownloadJob, strategy: str, message: str) -> None:
        self._print(f"{self._prefix(job)}   ✗ {strategy}: {message}")

    def job_finished(self, result: JobResult) -> None:
        prefix = self._prefix(result.job)
        if result.status is JobStatus.DONE:
            self._print(f"{prefix}   ✓ готово (способ «{result.strategy}»):")
        elif result.status is JobStatus.SKIPPED:
            self._print(f"{prefix}   ↷ уже скачано ранее:")
        elif result.attempts:
            tried = ", ".join(attempt.strategy for attempt in result.attempts)
            self._print(f"{prefix}   ✗ НЕ СКАЧАНО (испробованы способы: {tried})")
        else:
            self._print(f"{prefix}   ✗ НЕ СКАЧАНО: {result.error}")
        for path in result.outputs:
            self._print(f"{prefix}       {path}")

    def summary(self, summary: BatchSummary, report_hint: str) -> None:
        done = summary.count(JobStatus.DONE)
        skipped = summary.count(JobStatus.SKIPPED)
        failed = summary.count(JobStatus.FAILED)
        self._print(f"\nИтого: скачано {done}, пропущено {skipped}, ошибок {failed}.")
        self._print(report_hint)


def print_inspection(job: DownloadJob, inspection: Inspection, stream: TextIO | None = None) -> None:
    """Каталог источников записи (режим ``--dry-run``)."""

    out = stream or sys.stdout
    recording = inspection.recording

    def line(text: str) -> None:
        print(text, file=out, flush=True)

    line(f"\n{job.link.url}")
    line(f"  Название: {recording.title or '—'}")
    line(f"  Длительность: {format_duration(recording.duration)} (журнал: {inspection.source})")
    for video in recording.video_streams:
        size = f"{video.width}x{video.height}" if video.width and video.height else "размер ?"
        line(
            f"  • {video.title}: {video.codec or 'кодек ?'}, {size}, частей {len(video.segments)}, "
            f"{format_duration(video.start_time)}–{format_duration(video.end_time)}"
        )
    for audio in recording.audio_streams:
        line(
            f"  • {audio.title}: {audio.codec or 'кодек ?'}, частей {len(audio.segments)}, "
            f"{format_duration(audio.start_time)}–{format_duration(audio.end_time)}"
        )
    for presentation in recording.presentations:
        line(
            f"  • {presentation.title} «{presentation.file_name}»: слайдов {presentation.slide_count}, "
            f"{format_duration(presentation.start_time)}–{format_duration(presentation.end_time)}"
        )
    plan = inspection.plan
    main = {
        "composite": "сводное видео (материалы + спикер в углу)",
        "video": f"видео «{plan.main_video.title if plan.main_video else '?'}»",
        "audio-only": "звук на чёрном фоне",
    }[plan.main_kind.value]
    sources = len(recording.video_streams) + len(recording.audio_streams)
    line(f"  Будет создано: {main}; звук — все участники ({sources} источников)")
