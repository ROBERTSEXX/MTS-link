"""Метаданные потоков через ffprobe по удалённым URL (без скачивания)."""

from __future__ import annotations

from typing import Any

from mtslink_downloader.domain.models import (
    SPEAKER_KEY,
    MediaSegment,
    RecordDocument,
    Recording,
)
from mtslink_downloader.infrastructure.media.ffmpeg import Ffmpeg


class FfprobeEnricher:
    """Заполняет кодек, размер и активную длительность потоков.

    Длительность экрана и аудио участников нужна для временной шкалы
    сводного видео: эти потоки занимают только часть записи.
    """

    def __init__(self, ffmpeg: Ffmpeg) -> None:
        self._ffmpeg = ffmpeg

    def enrich(self, recording: Recording, document: RecordDocument) -> None:
        access = document.access
        for stream in recording.video_streams:
            if not stream.segments:
                continue
            first = stream.segments[0].any_url
            if stream.width is None or stream.height is None or stream.codec is None:
                metadata = self._ffmpeg.probe_remote(first, access.headers_for(first), "v:0")
                stream.codec = stream.codec or metadata.get("codec_name")
                stream.width = stream.width or _int(metadata.get("width"))
                stream.height = stream.height or _int(metadata.get("height"))
            if stream.key == SPEAKER_KEY and recording.duration > 0:
                stream.duration = recording.duration
            elif stream.duration <= 0:
                stream.duration = self._active_duration(stream.segments, document, "v:0")

        for audio in recording.audio_streams:
            if not audio.segments:
                continue
            first = audio.segments[0].any_url
            if audio.codec is None:
                metadata = self._ffmpeg.probe_remote(first, access.headers_for(first), "a:0")
                audio.codec = metadata.get("codec_name")
            if audio.duration <= 0:
                audio.duration = self._active_duration(audio.segments, document, "a:0")

        if recording.duration <= 0:
            ends = [stream.end_time for stream in recording.video_streams]
            ends += [audio.end_time for audio in recording.audio_streams]
            recording.duration = max(ends, default=0.0)
            speaker = recording.speaker
            if speaker is not None:
                speaker.duration = recording.duration

    def _active_duration(
        self, segments: list[MediaSegment], document: RecordDocument, selector: str
    ) -> float:
        total = 0.0
        for segment in segments:
            url = segment.any_url
            metadata = self._ffmpeg.probe_remote(url, document.access.headers_for(url), selector)
            duration = metadata.get("duration")
            if isinstance(duration, (int, float)):
                effective = float(duration)
                if segment.max_duration is not None:
                    effective = min(effective, segment.max_duration)
                total += effective
        if segments and segments[0].trim_duration is not None:
            total = min(total, segments[0].trim_duration)
        return max(0.0, total)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
