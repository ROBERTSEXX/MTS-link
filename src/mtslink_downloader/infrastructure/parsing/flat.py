"""Запасной разбор «любой URL в событии» — способ mtslinker и mtser.

Если в журнале нет привычных ``mediasession``-структур (другая версия API,
ответ ``record-files/.../flow``), берутся все ``data.url``/``data.hlsUrl``
из событий с их ``relativeTime``. Тип файла определяется ffprobe: файлы с
видео становятся камерой, файлы только со звуком — отдельными аудиопотоками.
"""

from __future__ import annotations

import logging
from typing import Any

from mtslink_downloader.domain.models import (
    SPEAKER_KEY,
    AudioStream,
    MediaSegment,
    RecordDocument,
    Recording,
    VideoStream,
)
from mtslink_downloader.infrastructure.media.ffmpeg import Ffmpeg

LOG = logging.getLogger(__name__)

_NOT_MEDIA = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ppt", ".pptx", ".doc",
              ".docx", ".xls", ".xlsx", ".zip", ".txt", ".html", ".json")


def _float(value: Any) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


class FlatUrlRecordParser:
    """Строит потоки из всех медиаадресов журнала с проверкой через ffprobe."""

    def __init__(self, ffmpeg: Ffmpeg) -> None:
        self._ffmpeg = ffmpeg

    def parse(self, document: RecordDocument) -> Recording:
        record = document.data
        total_duration = _float(record.get("duration"))
        title = str(record.get("name") or "").strip()
        candidates = self._candidates(record.get("eventLogs") or [])
        LOG.info("Запасной разбор: найдено адресов-кандидатов %d", len(candidates))

        video_segments: list[tuple[MediaSegment, float | None]] = []
        audio_segments: list[tuple[MediaSegment, float | None]] = []
        for segment in candidates:
            url = segment.any_url
            types, duration = self._ffmpeg.remote_stream_types(url, document.access.headers_for(url))
            if "video" in types:
                video_segments.append((segment, duration))
            elif "audio" in types:
                audio_segments.append((segment, duration))
            else:
                LOG.debug("Адрес не является медиа: %s", url.split("?", 1)[0])

        if total_duration <= 0:
            ends = [seg.relative_time + (dur or 0.0) for seg, dur in video_segments + audio_segments]
            total_duration = max(ends, default=0.0)

        video_streams: list[VideoStream] = []
        if video_segments:
            segments = self._limit_overlaps([seg for seg, _ in video_segments], total_duration)
            video_streams.append(
                VideoStream(
                    key=SPEAKER_KEY,
                    title="Видео записи",
                    segments=segments,
                    duration=total_duration,
                    start_time=0.0,
                    has_audio=True,
                )
            )
        audio_streams = [
            AudioStream(
                key=f"audio-{index}",
                title=f"Аудиофрагмент {index}",
                segments=[segment],
                duration=duration or 0.0,
                start_time=segment.relative_time,
            )
            for index, (segment, duration) in enumerate(
                sorted(audio_segments, key=lambda item: item[0].relative_time), start=1
            )
        ]
        return Recording(
            title=title,
            duration=total_duration,
            video_streams=video_streams,
            audio_streams=audio_streams,
        )

    @staticmethod
    def _candidates(event_logs: list[Any]) -> list[MediaSegment]:
        seen: set[str] = set()
        result: list[MediaSegment] = []

        def visit(container: Any, relative_time: float) -> None:
            if not isinstance(container, dict):
                return
            url = container.get("url")
            hls_url = container.get("hlsUrl")
            source = url if isinstance(url, str) and url.startswith("http") else ""
            hls = hls_url if isinstance(hls_url, str) and hls_url.startswith("http") else None
            key = (source or hls or "").split("?", 1)[0]
            if not key or key in seen or key.lower().endswith(_NOT_MEDIA):
                return
            seen.add(key)
            result.append(MediaSegment(source_url=source, hls_url=hls, relative_time=relative_time))

        for event in event_logs:
            if not isinstance(event, dict):
                continue
            relative_time = _float(event.get("relativeTime"))
            visit(event.get("data"), relative_time)
            snapshot = event.get("snapshot")
            data = snapshot.get("data") if isinstance(snapshot, dict) else None
            if isinstance(data, dict):
                for value in data.values():
                    for item in value if isinstance(value, list) else [value]:
                        visit(item, relative_time)
        result.sort(key=lambda item: item.relative_time)
        return result

    @staticmethod
    def _limit_overlaps(segments: list[MediaSegment], total_duration: float) -> list[MediaSegment]:
        """Каждый файл не длиннее промежутка до следующего (как snapshots)."""

        ordered = sorted(segments, key=lambda item: item.relative_time)
        for index, segment in enumerate(ordered):
            next_time = (
                ordered[index + 1].relative_time if index + 1 < len(ordered) else total_duration
            )
            if next_time > segment.relative_time:
                segment.max_duration = next_time - segment.relative_time
        return ordered
