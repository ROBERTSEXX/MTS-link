"""Политика выбора: какие файлы получить из записи в каждом режиме."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from mtslink_downloader.domain.errors import RecordUnavailableError
from mtslink_downloader.domain.models import (
    AudioStream,
    ExportMode,
    PresentationStream,
    Recording,
    VideoStream,
)


class MainKind(Enum):
    """Как собирается основной файл ``<имя>.mp4``."""

    COMPOSITE = "composite"
    VIDEO = "video"
    AUDIO_ONLY = "audio-only"


@dataclass(frozen=True)
class ExportPlan:
    """Что именно скачать и собрать для одной записи."""

    main_kind: MainKind
    main_video: VideoStream | None
    mixed_audio: tuple[AudioStream, ...] = ()
    separate_videos: tuple[VideoStream, ...] = ()
    separate_audios: tuple[AudioStream, ...] = ()
    presentations: tuple[PresentationStream, ...] = ()


class ExportPlanner:
    """Выбирает состав результата по режиму и найденным источникам.

    * ``auto`` — один файл: сводное видео, если были слайды или экран,
      иначе камера спикера с голосами участников.
    * ``speaker`` — только камера спикера (+ голоса участников), без
      перекодирования слайдов.
    * ``composite`` — сводное видео, если есть спикер.
    * ``all`` — все отдельные источники и основной файл.
    """

    def plan(self, recording: Recording, mode: ExportMode) -> ExportPlan:
        if recording.is_empty:
            raise RecordUnavailableError("В записи не найдено ни одного видео- или аудиопотока.")

        speaker = recording.speaker
        main_video = speaker or self._longest_video(recording)
        has_materials = bool(recording.presentations) or recording.screen is not None
        wants_composite = mode is ExportMode.COMPOSITE or (
            mode in {ExportMode.AUTO, ExportMode.ALL} and has_materials
        )

        if speaker is not None and wants_composite:
            main_kind = MainKind.COMPOSITE
        elif main_video is not None:
            main_kind = MainKind.VIDEO
        else:
            main_kind = MainKind.AUDIO_ONLY

        separate_videos: tuple[VideoStream, ...] = ()
        separate_audios: tuple[AudioStream, ...] = ()
        presentations: tuple[PresentationStream, ...] = ()
        if mode is ExportMode.ALL:
            # Основной поток в режиме VIDEO и так станет «<имя>.mp4»; второй
            # одинаковый файл с суффиксом не нужен.
            separate_videos = tuple(
                stream
                for stream in recording.video_streams
                if not (main_kind is MainKind.VIDEO and stream is main_video)
            )
            separate_audios = tuple(recording.audio_streams)
            presentations = tuple(recording.presentations)

        return ExportPlan(
            main_kind=main_kind,
            main_video=main_video if main_kind is not MainKind.AUDIO_ONLY else None,
            mixed_audio=tuple(recording.audio_streams),
            separate_videos=separate_videos,
            separate_audios=separate_audios,
            presentations=presentations,
        )

    @staticmethod
    def _longest_video(recording: Recording) -> VideoStream | None:
        if not recording.video_streams:
            return None
        return max(recording.video_streams, key=lambda stream: stream.duration)
