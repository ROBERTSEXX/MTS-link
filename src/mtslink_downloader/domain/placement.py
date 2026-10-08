"""Раскладка физических файлов по временной шкале записи.

Каждый файл записи (media-session) — это кусок реального времени
``[start_epoch, start_epoch + duration]``. Шкала записи — реальное время от
старта мероприятия, из которого вырезаны паузы (``cuts``). Раскладка
переводит файл в один или несколько кусков ``SegmentPiece``: какая часть
файла (``file_offset``) в какой момент записи (``timeline_start``) звучит
и показывается.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from mtslink_downloader.domain.models import MediaSegment, SegmentPiece

# Расхождение между пересчётом по реальному времени и relativeTime события,
# после которого данным о времени не доверяем и используем relativeTime.
MAX_CLOCK_DRIFT = 2.0
MIN_PIECE = 0.05


@dataclass(frozen=True)
class EpochTimeline:
    """Перевод реального (unix) времени в позицию на шкале записи."""

    start_epoch: float
    cuts: tuple[tuple[float, float], ...] = ()

    @classmethod
    def build(cls, start_epoch: float, cuts: Sequence[tuple[float, float]]) -> EpochTimeline:
        merged: list[tuple[float, float]] = []
        for begin, end in sorted((max(a, start_epoch), b) for a, b in cuts if b > start_epoch):
            if end <= begin:
                continue
            if merged and begin <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((begin, end))
        return cls(start_epoch=start_epoch, cuts=tuple(merged))

    def relative(self, epoch: float) -> float:
        removed = sum(max(0.0, min(end, epoch) - begin) for begin, end in self.cuts if begin < epoch)
        return max(0.0, epoch - self.start_epoch - removed)

    def pieces(self, start_epoch: float, duration: float) -> list[SegmentPiece]:
        """Части файла, попадающие в шкалу записи (вне вырезанных пауз)."""

        file_end = start_epoch + duration
        kept: list[tuple[float, float]] = []
        cursor = max(start_epoch, self.start_epoch)
        for begin, end in self.cuts:
            if end <= cursor:
                continue
            if begin >= file_end:
                break
            if begin > cursor:
                kept.append((cursor, min(begin, file_end)))
            cursor = max(cursor, end)
        if cursor < file_end:
            kept.append((cursor, file_end))
        return [
            SegmentPiece(
                file_offset=begin - start_epoch,
                timeline_start=self.relative(begin),
                length=end - begin,
            )
            for begin, end in kept
            if end - begin > MIN_PIECE
        ]


def attach_epoch_pieces(segment: MediaSegment, timeline: EpochTimeline) -> None:
    """Заполнить ``segment.pieces``, если у файла известны время и длительность."""

    if segment.start_epoch is None or not segment.known_duration:
        return
    pieces = timeline.pieces(segment.start_epoch, segment.known_duration)
    if not pieces:
        segment.pieces = []
        return
    # Файл, начавшийся после старта записи, должен лечь туда же, куда указывает
    # relativeTime события. Иначе часы сервера и журнала не согласованы.
    started_inside = segment.start_epoch >= timeline.start_epoch and not any(
        begin <= segment.start_epoch < end for begin, end in timeline.cuts
    )
    if started_inside and abs(pieces[0].timeline_start - segment.relative_time) > MAX_CLOCK_DRIFT:
        return
    segment.pieces = pieces


def place_segment(
    segment: MediaSegment, file_duration: float, record_duration: float
) -> list[SegmentPiece]:
    """Куски файла на шкале записи с учётом его реальной длительности.

    Если у сегмента нет точных данных о времени, используются правила
    журнала: преролл первого файла (``trim_duration``) и ограничение
    перекрывающихся snapshots (``max_duration``).
    """

    if segment.pieces is not None:
        pieces = [
            SegmentPiece(
                file_offset=piece.file_offset,
                timeline_start=piece.timeline_start,
                length=min(piece.length, file_duration - piece.file_offset),
            )
            for piece in segment.pieces
        ]
    elif segment.trim_duration is not None:
        length = min(segment.trim_duration, file_duration)
        pieces = [
            SegmentPiece(
                file_offset=max(0.0, file_duration - length),
                timeline_start=segment.relative_time,
                length=length,
            )
        ]
    else:
        length = file_duration
        if segment.max_duration is not None:
            length = min(length, segment.max_duration)
        pieces = [SegmentPiece(file_offset=0.0, timeline_start=segment.relative_time, length=length)]
    return clip_pieces(pieces, 0.0, record_duration if record_duration > 0 else float("inf"))


def clip_pieces(pieces: Sequence[SegmentPiece], start: float, end: float) -> list[SegmentPiece]:
    """Обрезать куски окном ``[start, end]`` шкалы записи."""

    result: list[SegmentPiece] = []
    for piece in pieces:
        begin = max(piece.timeline_start, start)
        finish = min(piece.timeline_end, end)
        if finish - begin <= MIN_PIECE:
            continue
        result.append(
            SegmentPiece(
                file_offset=piece.file_offset + (begin - piece.timeline_start),
                timeline_start=begin,
                length=finish - begin,
            )
        )
    return result


def without_overlaps(pieces: Sequence[tuple[SegmentPiece, object]]) -> list[tuple[SegmentPiece, object]]:
    """Для видео: позже начавшийся кусок перекрывает предыдущий.

    Звук сводится целиком, а картинка в каждый момент одна, поэтому
    предыдущий кусок обрезается началом следующего.
    """

    # В каждый момент показываем самый поздно начавшийся из активных кусков;
    # когда он заканчивается, снова виден предыдущий (если он ещё идёт).
    boundaries = sorted(
        {p.timeline_start for p, _ in pieces} | {p.timeline_end for p, _ in pieces}
    )
    result: list[tuple[SegmentPiece, object]] = []
    for begin, end in zip(boundaries, boundaries[1:], strict=False):
        if end - begin <= 1e-6:
            continue
        middle = (begin + end) / 2
        active = [item for item in pieces if item[0].timeline_start <= middle < item[0].timeline_end]
        if not active:
            continue
        piece, source = max(active, key=lambda item: item[0].timeline_start)
        offset = piece.file_offset + (begin - piece.timeline_start)
        previous = result[-1] if result else None
        if (
            previous is not None
            and previous[1] is source
            and abs(previous[0].timeline_end - begin) < 1e-6
            and abs(previous[0].file_offset + previous[0].length - offset) < 1e-3
        ):
            merged = SegmentPiece(
                previous[0].file_offset, previous[0].timeline_start, previous[0].length + end - begin
            )
            result[-1] = (merged, source)
        else:
            result.append((SegmentPiece(offset, begin, end - begin), source))
    return [(piece, source) for piece, source in result if piece.length > MIN_PIECE]
