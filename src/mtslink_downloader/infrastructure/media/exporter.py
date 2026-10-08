"""Выполнение плана экспорта: какие файлы и в каком порядке собрать."""

from __future__ import annotations

import contextlib
import logging
import shutil
from pathlib import Path

from mtslink_downloader.application.export_plan import ExportPlan, MainKind
from mtslink_downloader.domain.errors import MediaProcessingError
from mtslink_downloader.domain.models import (
    AudioStream,
    DownloadJob,
    DownloadSettings,
    MediaSegment,
    RecordDocument,
    Recording,
    VideoStream,
)
from mtslink_downloader.infrastructure.media.assembler import (
    LocalSegment,
    SegmentPreparer,
    TrackBuilder,
    window,
)
from mtslink_downloader.infrastructure.media.composite import CompositeRenderer
from mtslink_downloader.infrastructure.media.fetching import SegmentFetcher, copy_atomically
from mtslink_downloader.infrastructure.media.mixing import Muxer
from mtslink_downloader.infrastructure.storage.naming import OutputNamer

LOG = logging.getLogger(__name__)

Stream = VideoStream | AudioStream


class FfmpegRecordingExporter:
    """Собирает файлы записи по ``ExportPlan``.

    Все файлы записи скачиваются один раз и раскладываются по шкале: из
    этого набора строятся и отдельные источники, и основной файл. Звук
    основного файла — сведение всех участников, как в плеере МТС Линк.
    Уже существующие результаты не пересобираются (если не включена
    перезапись), а скачанные файлы кэшируются для продолжения после сбоя.
    """

    def __init__(
        self,
        preparer: SegmentPreparer,
        tracks: TrackBuilder,
        composite: CompositeRenderer,
        muxer: Muxer,
        fetcher: SegmentFetcher,
    ) -> None:
        self._preparer = preparer
        self._tracks = tracks
        self._composite = composite
        self._muxer = muxer
        self._fetcher = fetcher

    def export(
        self,
        recording: Recording,
        plan: ExportPlan,
        document: RecordDocument,
        job: DownloadJob,
        settings: DownloadSettings,
    ) -> list[Path]:
        namer = OutputNamer(settings.output_dir)
        stem = namer.stem(job, recording.title)
        stem.parent.mkdir(parents=True, exist_ok=True)
        work = namer.work_dir(job)
        work.mkdir(parents=True, exist_ok=True)
        outputs: list[Path] = []

        def missing(path: Path) -> bool:
            return settings.overwrite or not path.exists()

        main_path = namer.main(stem)
        videos = [s for s in plan.separate_videos if missing(namer.video(stem, s.key))]
        audios = [s for s in plan.separate_audios if missing(namer.audio(stem, s.key))]
        build_main = missing(main_path)

        # Картинка нужна только тем потокам, из которых строится видео; у
        # остальных скачивается лишь звук.
        video_keys = {stream.key for stream in videos}
        if build_main and plan.main_video is not None:
            video_keys.add(plan.main_video.key)
            if plan.main_kind is MainKind.COMPOSITE and recording.screen is not None:
                video_keys.add(recording.screen.key)

        streams: list[Stream] = [*recording.video_streams, *recording.audio_streams]
        wanted: list[tuple[MediaSegment, bool]] = []
        if build_main or videos or audios:
            wanted = [
                (segment, stream.key in video_keys)
                for stream in streams
                for segment in stream.segments
            ]
        prepared = self._preparer.prepare(
            wanted, work / "cache", document.access, recording.duration
        )
        local: dict[str, list[LocalSegment]] = {
            stream.key: [prepared[id(seg)] for seg in stream.segments if id(seg) in prepared]
            for stream in streams
        }
        duration = recording.duration
        if duration <= 0:
            ends = [span[1] for items in local.values() if (span := window(items))]
            duration = max(ends, default=0.0)

        for stream in plan.separate_videos:
            path = namer.video(stem, stream.key)
            if stream.key in {item.key for item in videos}:
                self._separate_video(stream, local[stream.key], path, work)
            outputs.append(path)
        for audio in plan.separate_audios:
            path = namer.audio(stem, audio.key)
            if audio.key in {item.key for item in audios}:
                self._separate_audio(audio, local[audio.key], path, work)
            outputs.append(path)
        for presentation in plan.presentations:
            # МТС Линк хранит исходник как есть: PDF, PPTX и т.д.
            suffix = Path(presentation.file_name).suffix.lower() or ".pdf"
            path = namer.presentation(stem, presentation.key, suffix)
            if missing(path):
                temp = work / f"{presentation.key}{suffix}"
                self._fetcher.fetch_file(presentation.source_url, temp, document.access)
                copy_atomically(temp, path)
            outputs.append(path)

        if build_main:
            all_locals = [item for items in local.values() for item in items]
            self._build_main(recording, plan, local, all_locals, duration, main_path, work, document)
        else:
            LOG.info("Уже есть, пропускаю: %s", main_path.name)
        outputs.append(main_path)

        if not settings.keep_work_files:
            shutil.rmtree(work, ignore_errors=True)
            with contextlib.suppress(OSError):
                work.parent.rmdir()
        return outputs

    # ------------------------------------------------------------------

    def _separate_video(
        self, stream: VideoStream, locals_: list[LocalSegment], destination: Path, work: Path
    ) -> None:
        span = window(locals_)
        if span is None:
            raise MediaProcessingError(f"У потока «{stream.title}» нет скачанных файлов.")
        start, end = span
        video = self._tracks.video(locals_, work / f"{stream.key}-video.mp4", start, end, work)
        audio = self._tracks.audio(locals_, work / f"{stream.key}-audio.m4a", start, end)
        self._muxer.mux(video, audio, destination)

    def _separate_audio(
        self, stream: AudioStream, locals_: list[LocalSegment], destination: Path, work: Path
    ) -> None:
        span = window(locals_)
        if span is None:
            raise MediaProcessingError(f"У потока «{stream.title}» нет скачанных файлов.")
        audio = self._tracks.audio(locals_, work / f"{stream.key}-audio.m4a", *span)
        self._muxer.audio_file(audio, destination)

    def _build_main(
        self,
        recording: Recording,
        plan: ExportPlan,
        local: dict[str, list[LocalSegment]],
        all_locals: list[LocalSegment],
        duration: float,
        destination: Path,
        work: Path,
        document: RecordDocument,
    ) -> None:
        if duration <= 0:
            raise MediaProcessingError("Не удалось определить длительность записи.")
        audio = self._tracks.audio(all_locals, work / "main-audio.m4a", 0.0, duration)

        main_locals = local[plan.main_video.key] if plan.main_video is not None else []
        if plan.main_kind is MainKind.AUDIO_ONLY or not any(i.has_video for i in main_locals):
            if plan.main_kind is not MainKind.AUDIO_ONLY:
                LOG.warning("В основном потоке нет картинки — будет чёрный фон со звуком")
            self._muxer.black_video(audio, destination, duration)
            return
        video = self._tracks.video(main_locals, work / "main-video.mp4", 0.0, duration, work)

        if plan.main_kind is MainKind.VIDEO:
            self._muxer.mux(video, audio, destination)
            return

        # Сводное видео: материалы основным кадром, спикер в углу.
        normalized = work / "speaker-normalized.mp4"
        self._composite.normalize_speaker(video, normalized)
        screen = None
        screen_stream = recording.screen
        if screen_stream is not None:
            span = window(local[screen_stream.key])
            if span is not None:
                screen_stream.start_time, screen_stream.duration = span[0], span[1] - span[0]
                screen_path = self._tracks.video(
                    local[screen_stream.key], work / "screen-video.mp4", span[0], span[1], work
                )
                screen = (screen_path, screen_stream)
        self._composite.render(
            speaker=normalized,
            audio=audio,
            screen=screen,
            presentations=recording.presentations,
            duration=duration,
            destination=destination,
            work_dir=work,
            access=document.access,
        )
