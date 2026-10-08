"""Сборка дорожек по временной шкале записи.

Файлы участников в МТС Линк идут одновременно, поэтому их нельзя просто
склеить друг за другом. Вместо этого каждый файл раскладывается по шкале
записи (``SegmentPiece``), затем:

* видеодорожка собирается из кусков с картинкой, пустые места — чёрные;
* звук сводится из всех кусков со звуком, каждый в своё время.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from mtslink_downloader.domain.errors import MediaProcessingError
from mtslink_downloader.domain.models import MediaAccess, MediaSegment, SegmentPiece
from mtslink_downloader.domain.placement import clip_pieces, place_segment, without_overlaps
from mtslink_downloader.infrastructure.media.editing import Concatenator, SegmentEditor
from mtslink_downloader.infrastructure.media.fetching import SegmentFetcher
from mtslink_downloader.infrastructure.media.ffmpeg import Ffmpeg, format_duration
from mtslink_downloader.infrastructure.media.mixing import AudioMixer, MixInput

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class LocalSegment:
    """Скачанный файл, его дорожки и положение на шкале записи."""

    segment: MediaSegment
    path: Path
    types: frozenset[str]
    pieces: tuple[SegmentPiece, ...]

    @property
    def has_video(self) -> bool:
        return "video" in self.types

    @property
    def has_audio(self) -> bool:
        return "audio" in self.types


class SegmentPreparer:
    """Скачивает сегменты параллельно и раскладывает их по шкале."""

    def __init__(self, ffmpeg: Ffmpeg, fetcher: SegmentFetcher, workers: int = 4) -> None:
        self._ffmpeg = ffmpeg
        self._fetcher = fetcher
        self._workers = max(1, workers)

    def prepare(
        self,
        segments: Sequence[tuple[MediaSegment, bool]],
        cache_dir: Path,
        access: MediaAccess,
        record_duration: float,
    ) -> dict[int, LocalSegment]:
        """``(сегмент, нужна ли картинка)`` → локальные файлы по ``id(segment)``."""

        total = len(segments)
        done = [0]
        lock = threading.Lock()

        def one(item: tuple[MediaSegment, bool]) -> tuple[int, LocalSegment | None]:
            segment, want_video = item
            local = self._prepare_one(segment, want_video, cache_dir, access, record_duration)
            with lock:
                done[0] += 1
                if done[0] == total or done[0] % max(1, total // 10) == 0:
                    LOG.info("  скачано файлов: %d из %d", done[0], total)
            return id(segment), local

        LOG.info("Скачивание файлов записи: %d (по %d одновременно)", total, self._workers)
        with ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="segment") as pool:
            results = list(pool.map(one, segments))
        return {key: local for key, local in results if local is not None}

    def _prepare_one(
        self,
        segment: MediaSegment,
        want_video: bool,
        cache_dir: Path,
        access: MediaAccess,
        record_duration: float,
    ) -> LocalSegment | None:
        if segment.pieces == []:
            return None  # файл целиком внутри вырезанной паузы
        url = segment.any_url
        remote_types, _ = self._ffmpeg.remote_stream_types(url, access.headers_for(url))
        if want_video and ("video" in remote_types or not remote_types):
            path = self._fetcher.fetch_media(segment, cache_dir, access)
        elif remote_types and "audio" not in remote_types:
            return None  # нужен только звук, а в файле его нет (например, экран)
        else:
            path = self._fetcher.fetch_audio(segment, cache_dir, access)
        types = frozenset(self._ffmpeg.stream_types(path))
        duration = self._ffmpeg.duration(path)
        pieces = tuple(place_segment(segment, duration, record_duration))
        return LocalSegment(segment=segment, path=path, types=types, pieces=pieces)


class TrackBuilder:
    """Строит видеодорожку и сведённый звук для окна шкалы записи."""

    def __init__(
        self,
        ffmpeg: Ffmpeg,
        editor: SegmentEditor,
        concatenator: Concatenator,
        mixer: AudioMixer,
    ) -> None:
        self._ffmpeg = ffmpeg
        self._editor = editor
        self._concat = concatenator
        self._mixer = mixer

    def video(
        self,
        locals_: Sequence[LocalSegment],
        destination: Path,
        start: float,
        end: float,
        work_dir: Path,
    ) -> Path:
        """Видеодорожка без звука на всё окно ``[start, end]``."""

        if self._ffmpeg.is_complete(destination, end - start):
            LOG.info("Видеодорожка %s уже собрана, беру готовую", destination.stem)
            return destination
        candidates: list[tuple[SegmentPiece, object]] = [
            (piece, local)
            for local in locals_
            if local.has_video
            for piece in clip_pieces(local.pieces, start, end)
        ]
        visible = without_overlaps(candidates)
        size = self._size(locals_)
        parts_dir = work_dir / f"{destination.stem}-parts"
        parts_dir.mkdir(parents=True, exist_ok=True)
        parts: list[Path] = []
        position = start
        for index, (piece, source) in enumerate(visible, start=1):
            assert isinstance(source, LocalSegment)
            if piece.timeline_start - position > 0.05:
                gap = parts_dir / f"{index:04d}-gap.mp4"
                self._editor.video_gap(gap, piece.timeline_start - position, size, with_audio=False)
                parts.append(gap)
            cut = parts_dir / f"{index:04d}-video.mp4"
            self._editor.cut_video(source.path, cut, piece.file_offset, piece.length, video_only=True)
            parts.append(cut)
            position = piece.timeline_end
        if end - position > 0.05:
            gap = parts_dir / "tail-gap.mp4"
            self._editor.video_gap(gap, end - position, size, with_audio=False)
            parts.append(gap)
        if not parts:
            raise MediaProcessingError("Нет данных для видеодорожки.")
        LOG.info(
            "Видеодорожка %s: %d фрагментов с картинкой, %s",
            destination.stem, len(visible), format_duration(end - start),
        )
        self._concat.video(parts, destination, end - start)
        return destination

    def audio(
        self, locals_: Sequence[LocalSegment], destination: Path, start: float, end: float
    ) -> Path:
        """Звук всех файлов со звуком, каждый на своём месте окна."""

        if self._ffmpeg.is_complete(destination, end - start):
            LOG.info("Звук %s уже сведён, беру готовый", destination.stem)
            return destination
        inputs = [
            MixInput(
                path=local.path,
                file_offset=piece.file_offset,
                length=piece.length,
                delay=piece.timeline_start - start,
            )
            for local in locals_
            if local.has_audio
            for piece in clip_pieces(local.pieces, start, end)
        ]
        LOG.info("Сведение звука: %d фрагментов, %s", len(inputs), format_duration(end - start))
        self._mixer.mix_pieces(inputs, destination, end - start)
        return destination

    def _size(self, locals_: Sequence[LocalSegment]) -> tuple[int, int]:
        for local in locals_:
            if local.has_video:
                width, height = self._ffmpeg.video_size(local.path)
                if width and height:
                    return max(2, width - width % 2), max(2, height - height % 2)
        return 1280, 720


def window(locals_: Sequence[LocalSegment]) -> tuple[float, float] | None:
    """Интервал шкалы, который занимают файлы потока."""

    pieces = [piece for local in locals_ for piece in local.pieces]
    if not pieces:
        return None
    return min(p.timeline_start for p in pieces), max(p.timeline_end for p in pieces)
