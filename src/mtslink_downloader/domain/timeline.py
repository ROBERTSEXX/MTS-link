"""Чистые функции построения временной шкалы сводного ролика."""

from __future__ import annotations

from collections.abc import Sequence

from mtslink_downloader.domain.models import CompositeSegment, PresentationStream, VideoStream


def presentation_image_at(
    presentations: Sequence[PresentationStream], relative_time: float
) -> str | None:
    """Вернуть URL слайда, активного в заданный момент записи."""

    for presentation in presentations:
        current_image: str | None = None
        for update in presentation.updates:
            if update.relative_time > relative_time:
                break
            if not update.is_active:
                current_image = None
            elif update.image_url:
                current_image = update.image_url
        if current_image:
            return current_image
    return None


def build_composite_timeline(
    total_duration: float,
    presentations: Sequence[PresentationStream],
    screen_stream: VideoStream | None,
) -> list[CompositeSegment]:
    """Разбить запись на участки с одним основным изображением.

    Приоритет материала: screen share, затем активный слайд, затем камера
    спикера. Соседние участки с одинаковым содержимым объединяются, чтобы не
    перекодировать лишние фрагменты.
    """

    if total_duration <= 0:
        return []

    boundaries = {0.0, total_duration}
    for presentation in presentations:
        boundaries.add(max(0.0, min(total_duration, presentation.start_time)))
        boundaries.add(max(0.0, min(total_duration, presentation.end_time)))
        for update in presentation.updates:
            if 0.0 < update.relative_time < total_duration:
                boundaries.add(update.relative_time)
    has_screen = screen_stream is not None and screen_stream.duration > 0
    if screen_stream is not None and has_screen:
        boundaries.add(max(0.0, min(total_duration, screen_stream.start_time)))
        boundaries.add(max(0.0, min(total_duration, screen_stream.end_time)))

    ordered = sorted(boundaries)
    segments: list[CompositeSegment] = []
    for start_time, end_time in zip(ordered, ordered[1:], strict=False):
        duration = end_time - start_time
        if duration <= 0.05:
            continue
        # Середина интервала относит границу события к следующему состоянию.
        midpoint = start_time + duration / 2
        image_url = presentation_image_at(presentations, midpoint)
        if (
            screen_stream is not None
            and has_screen
            and screen_stream.start_time <= midpoint < screen_stream.end_time
        ):
            kind = "screen"
            image_url = None
        elif image_url:
            kind = "presentation"
        else:
            kind = "speaker"

        previous = segments[-1] if segments else None
        if (
            previous is not None
            and previous.kind == kind
            and previous.image_url == image_url
            and abs(previous.start_time + previous.duration - start_time) < 0.01
        ):
            previous.duration += duration
        else:
            segments.append(
                CompositeSegment(
                    kind=kind, start_time=start_time, duration=duration, image_url=image_url
                )
            )
    return segments
