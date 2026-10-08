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
    MediaAccess,
    RecordDocument,
    Recording,
    VideoStream,
)
from mtslink_downloader.infrastructure.media.assembler import StreamAssembler
from mtslink_downloader.infrastructure.media.composite import CompositeRenderer
from mtslink_downloader.infrastructure.media.fetching import copy_atomically
from mtslink_downloader.infrastructure.media.mixing import AudioMixer, Muxer
from mtslink_downloader.infrastructure.storage.naming import OutputNamer

LOG = logging.getLogger(__name__)


class _Workspace:
    """Локальные файлы одного экспорта; повторно не собирает одно и то же."""

    def __init__(self, root: Path, assembler: StreamAssembler, access: MediaAccess) -> None:
        self.root = root
        self._assembler = assembler
        self._access = access
        self._videos: dict[str, Path] = {}
        self._audios: dict[str, Path] = {}

    def remember_video(self, key: str, path: Path) -> None:
        self._videos[key] = path

    def remember_audio(self, key: str, path: Path) -> None:
        self._audios[key] = path

    def video(self, stream: VideoStream) -> Path:
        if stream.key not in self._videos:
            target = self.root / f"{stream.key}.mp4"
            self._videos[stream.key] = self._assembler.video(stream, target, self.root, self._access)
        return self._videos[stream.key]

    def audio(self, stream: AudioStream) -> Path:
        if stream.key not in self._audios:
            target = self.root / f"{stream.key}.m4a"
            self._audios[stream.key] = self._assembler.audio(stream, target, self.root, self._access)
        return self._audios[stream.key]


class FfmpegRecordingExporter:
    """Собирает файлы записи по ``ExportPlan``.

    Уже существующие результаты не пересобираются (если не включена
    перезапись), поэтому повторный запуск после сбоя доделывает только
    недостающее, а промежуточные сегменты берутся из кэша рабочей папки.
    """

    def __init__(
        self,
        assembler: StreamAssembler,
        composite: CompositeRenderer,
        mixer: AudioMixer,
        muxer: Muxer,
    ) -> None:
        self._assembler = assembler
        self._composite = composite
        self._mixer = mixer
        self._muxer = muxer

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
        work_root = namer.work_dir(job)
        work_root.mkdir(parents=True, exist_ok=True)
        workspace = _Workspace(work_root, self._assembler, document.access)
        outputs: list[Path] = []

        def needed(path: Path) -> bool:
            if path.exists() and not settings.overwrite:
                LOG.info("Уже есть, пропускаю: %s", path.name)
                outputs.append(path)
                return False
            return True

        for stream in plan.separate_videos:
            path = namer.video(stem, stream.key)
            if needed(path):
                copy_atomically(workspace.video(stream), path)
                outputs.append(path)
            workspace.remember_video(stream.key, path)
        for audio in plan.separate_audios:
            path = namer.audio(stem, audio.key)
            if needed(path):
                copy_atomically(workspace.audio(audio), path)
                outputs.append(path)
            workspace.remember_audio(audio.key, path)
        for presentation in plan.presentations:
            path = namer.presentation(stem, presentation.key)
            if needed(path):
                self._assembler.presentation(presentation, path, work_root, document.access)
                outputs.append(path)

        main_path = namer.main(stem)
        if needed(main_path):
            self._build_main(recording, plan, workspace, main_path, document.access)
            outputs.append(main_path)

        if not settings.keep_work_files:
            shutil.rmtree(work_root, ignore_errors=True)
            _remove_if_empty(work_root.parent)
        return outputs

    def _build_main(
        self,
        recording: Recording,
        plan: ExportPlan,
        workspace: _Workspace,
        destination: Path,
        access: MediaAccess,
    ) -> None:
        duration = recording.duration
        extras = [(workspace.audio(stream), stream.start_time) for stream in plan.mixed_audio]

        if plan.main_kind is MainKind.AUDIO_ONLY:
            mixed = workspace.root / "main-audio.m4a"
            self._mixer.mix(None, extras, mixed, duration)
            partial = _partial(destination)
            self._muxer.black_video(mixed, partial, duration)
            partial.replace(destination)
            return

        main_video = plan.main_video
        if main_video is None:
            raise MediaProcessingError("План не содержит основного видео.")
        video_path = workspace.video(main_video)

        if plan.main_kind is MainKind.VIDEO:
            if not extras:
                copy_atomically(video_path, destination)
                return
            mixed = workspace.root / "main-audio.m4a"
            self._mixer.mix(video_path, extras, mixed, duration or main_video.duration)
            partial = _partial(destination)
            self._muxer.replace_audio(video_path, mixed, partial)
            partial.replace(destination)
            return

        # Сводное видео: материалы основным кадром, спикер в углу.
        normalized = workspace.root / "speaker-normalized.mp4"
        if not normalized.exists():
            self._composite.normalize_speaker(video_path, normalized)
        audio_path = normalized
        if extras:
            audio_path = workspace.root / "main-audio.m4a"
            self._mixer.mix(normalized, extras, audio_path, duration)
        screen_stream = recording.screen
        screen = (workspace.video(screen_stream), screen_stream) if screen_stream else None
        self._composite.render(
            speaker=normalized,
            audio=audio_path,
            screen=screen,
            presentations=recording.presentations,
            duration=duration or main_video.duration,
            destination=destination,
            work_dir=workspace.root,
            access=access,
        )


def _remove_if_empty(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.rmdir()


def _partial(destination: Path) -> Path:
    """Временное имя с исходным расширением: по нему ffmpeg выбирает контейнер."""

    return destination.with_name(f"{destination.stem}.part{destination.suffix}")
