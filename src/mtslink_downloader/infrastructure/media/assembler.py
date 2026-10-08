"""Сборка логического потока из физических сегментов."""

from __future__ import annotations

import logging
from pathlib import Path

from mtslink_downloader.domain.errors import MediaProcessingError
from mtslink_downloader.domain.models import (
    SPEAKER_KEY,
    AudioStream,
    MediaAccess,
    PresentationStream,
    VideoStream,
)
from mtslink_downloader.infrastructure.media.editing import Concatenator, SegmentEditor
from mtslink_downloader.infrastructure.media.fetching import SegmentFetcher, copy_atomically
from mtslink_downloader.infrastructure.media.ffmpeg import Ffmpeg, format_duration

LOG = logging.getLogger(__name__)


class StreamAssembler:
    """Скачивает сегменты потока, выравнивает их по времени и склеивает.

    Пауза между сегментами камеры заполняется чёрным кадром, между
    аудиосегментами — тишиной; преролл первого файла отрезается, а
    перекрывающиеся snapshots ограничиваются началом следующего файла.
    """

    def __init__(
        self,
        ffmpeg: Ffmpeg,
        fetcher: SegmentFetcher,
        editor: SegmentEditor,
        concatenator: Concatenator,
    ) -> None:
        self._ffmpeg = ffmpeg
        self._fetcher = fetcher
        self._editor = editor
        self._concat = concatenator

    def video(
        self, stream: VideoStream, destination: Path, work_dir: Path, access: MediaAccess
    ) -> Path:
        LOG.info("Поток «%s»: сегментов %d", stream.title, len(stream.segments))
        parts_dir = work_dir / f"parts-{stream.key}"
        parts_dir.mkdir(parents=True, exist_ok=True)
        size = (stream.width or 1280, stream.height or 720)
        parts: list[Path] = []
        position = 0.0
        for index, segment in enumerate(stream.segments, start=1):
            if stream.key == SPEAKER_KEY and index > 1 and segment.relative_time > position + 0.5:
                gap_duration = segment.relative_time - position
                gap = parts_dir / f"{index:04d}-gap.mp4"
                self._editor.video_gap(gap, gap_duration, size, with_audio=stream.has_audio)
                parts.append(gap)
                position += gap_duration

            _log_progress(stream.title, index, len(stream.segments))
            downloaded = self._fetcher.fetch_media(segment, work_dir / "cache", access)
            prepared = self._editor.ensure_tracks(
                downloaded, parts_dir / f"{index:04d}-tracks.mp4", stream.has_audio, size
            )
            if segment.max_duration is not None:
                if segment.max_duration <= 0:
                    LOG.warning("Пропускаю snapshot нулевой длины: %s", segment.any_url.split("?")[0])
                    continue
                limited = parts_dir / f"{index:04d}-head.mp4"
                self._editor.keep_head(prepared, limited, segment.max_duration)
                prepared = limited
            if index == 1 and segment.trim_duration is not None:
                trimmed = parts_dir / f"{index:04d}-tail.mp4"
                self._editor.keep_tail(prepared, trimmed, segment.trim_duration)
                prepared = trimmed
            parts.append(prepared)
            position += self._ffmpeg.duration(prepared)

        if not parts:
            raise MediaProcessingError(f"Поток «{stream.title}» не содержит ни одного сегмента.")
        combined = parts_dir / "combined.mp4"
        self._concat.video(parts, combined, stream.duration if stream.key == SPEAKER_KEY else 0.0)
        copy_atomically(combined, destination)
        LOG.info("Готово: %s (%s)", destination.name, format_duration(self._ffmpeg.duration(destination)))
        return destination

    def audio(
        self, stream: AudioStream, destination: Path, work_dir: Path, access: MediaAccess
    ) -> Path:
        LOG.info("Аудиопоток «%s»: сегментов %d", stream.title, len(stream.segments))
        parts_dir = work_dir / f"parts-{stream.key}"
        parts_dir.mkdir(parents=True, exist_ok=True)
        parts: list[Path] = []
        position = 0.0
        for index, segment in enumerate(stream.segments, start=1):
            expected = max(0.0, segment.relative_time - stream.start_time)
            if index > 1 and expected > position + 0.5:
                gap = parts_dir / f"{index:04d}-gap.m4a"
                self._editor.silence(gap, expected - position)
                parts.append(gap)
                position = expected

            _log_progress(stream.title, index, len(stream.segments))
            prepared = self._fetcher.fetch_audio(segment, work_dir / "cache", access)
            if segment.max_duration is not None:
                if segment.max_duration <= 0:
                    continue
                limited = parts_dir / f"{index:04d}-head.m4a"
                self._editor.keep_audio_head(prepared, limited, segment.max_duration)
                prepared = limited
            if index == 1 and segment.trim_duration is not None:
                trimmed = parts_dir / f"{index:04d}-tail.m4a"
                self._editor.keep_audio_tail(prepared, trimmed, segment.trim_duration)
                prepared = trimmed
            parts.append(prepared)
            position += self._ffmpeg.duration(prepared)

        if not parts:
            raise MediaProcessingError(f"Аудиопоток «{stream.title}» пуст.")
        combined = parts_dir / "combined.m4a"
        self._concat.audio(parts, combined, max(stream.duration, position))
        copy_atomically(combined, destination)
        return destination

    def presentation(
        self, stream: PresentationStream, destination: Path, work_dir: Path, access: MediaAccess
    ) -> Path:
        LOG.info("Скачивание презентации «%s»", stream.file_name)
        temp = work_dir / f"{stream.key}.pdf"
        self._fetcher.fetch_file(stream.source_url, temp, access)
        copy_atomically(temp, destination)
        return destination


def _log_progress(title: str, index: int, total: int) -> None:
    step = max(1, total // 10)
    if index == 1 or index == total or index % step == 0:
        LOG.info("  «%s»: сегмент %d из %d", title, index, total)
