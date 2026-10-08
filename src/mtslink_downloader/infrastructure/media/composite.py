"""Сводное видео: материалы основным кадром, спикер — окном в углу."""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Sequence
from pathlib import Path

from mtslink_downloader.domain.errors import MediaProcessingError, MtsLinkError
from mtslink_downloader.domain.models import (
    CompositeSegment,
    MediaAccess,
    PresentationStream,
    VideoStream,
)
from mtslink_downloader.domain.timeline import build_composite_timeline
from mtslink_downloader.infrastructure.media.editing import AUDIO_ARGS, NORMALIZED_FPS, Concatenator
from mtslink_downloader.infrastructure.media.fetching import SegmentFetcher, copy_atomically
from mtslink_downloader.infrastructure.media.ffmpeg import Ffmpeg, format_duration

LOG = logging.getLogger(__name__)

WIDTH = 1280
HEIGHT = 720
PIP_WIDTH = 160
MARGIN = 16
FPS = 25


def fit_filter(width: int = WIDTH, height: int = HEIGHT) -> str:
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
    )


def pip_filter() -> str:
    return (
        f"scale={PIP_WIDTH}:-2:force_original_aspect_ratio=decrease,"
        f"pad={PIP_WIDTH}:{PIP_WIDTH * 9 // 16}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
    )


def encode_args() -> list[str]:
    return [
        "-r", str(FPS),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        *AUDIO_ARGS,
        "-af", "aresample=async=1:first_pts=0",
        "-max_interleave_delta", "0", "-avoid_negative_ts", "make_zero",
        "-y",
    ]


class CompositeRenderer:
    """Перекодирует каждый участок временной шкалы и склеивает их."""

    def __init__(self, ffmpeg: Ffmpeg, fetcher: SegmentFetcher, concatenator: Concatenator) -> None:
        self._ffmpeg = ffmpeg
        self._fetcher = fetcher
        self._concat = concatenator

    def normalize_speaker(self, source: Path, destination: Path) -> None:
        """Равномерные PTS камеры: иначе seek по склеенным сегментам «плывёт»."""

        if self._ffmpeg.is_complete(destination, self._ffmpeg.duration(source)):
            LOG.info("Нормализованное видео спикера уже есть, беру готовое")
            return
        partial = destination.with_name(f"{destination.stem}.part{destination.suffix}")
        self._ffmpeg.run(
            ["-i", str(source), "-map", "0:v:0", "-map", "0:a:0?",
             "-vf", f"setpts=PTS-STARTPTS,fps={NORMALIZED_FPS}", "-af", "asetpts=N/SR/TB",
             "-r", str(NORMALIZED_FPS),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
             *AUDIO_ARGS, "-max_interleave_delta", "0", "-avoid_negative_ts", "make_zero",
             "-movflags", "+faststart", "-y", str(partial)],
            "Нормализация временных меток видео спикера",
        )
        os.replace(partial, destination)

    def render(
        self,
        speaker: Path,
        audio: Path,
        screen: tuple[Path, VideoStream] | None,
        presentations: Sequence[PresentationStream],
        duration: float,
        destination: Path,
        work_dir: Path,
        access: MediaAccess,
    ) -> Path:
        screen_stream = screen[1] if screen else None
        timeline = build_composite_timeline(duration, presentations, screen_stream)
        if not timeline:
            raise MediaProcessingError("Не удалось построить временную шкалу сводного видео.")
        LOG.info("Сводное видео: %d участков, %dx%d", len(timeline), WIDTH, HEIGHT)

        slides = self._download_slides(presentations, work_dir / "slides", access)
        rendered_dir = work_dir / "composite"
        rendered_dir.mkdir(parents=True, exist_ok=True)
        rendered: list[Path] = []
        for index, segment in enumerate(timeline, start=1):
            target = rendered_dir / f"segment-{index:05d}.mp4"
            rendered.append(target)
            if self._ffmpeg.is_complete(target, segment.duration, tolerance=0.5):
                continue  # участок уже собран в прошлый запуск
            LOG.info(
                "  участок %d из %d (%s, %s)",
                index, len(timeline), segment.kind, format_duration(segment.duration),
            )
            partial = target.with_name(f"{target.stem}.part{target.suffix}")
            slide = slides.get(segment.image_url or "")
            if segment.kind == "presentation" and slide is None:
                # Слайд не скачался ни по одному адресу: показываем спикера,
                # а не теряем всё сводное видео.
                LOG.warning("Нет изображения слайда — на этом участке будет спикер")
                self.render_speaker(speaker, segment, partial, audio)
            elif segment.kind == "presentation" and slide is not None:
                self.render_material(segment, speaker, slide, partial, 0.0, audio)
            elif segment.kind == "screen":
                if screen is None:
                    raise MediaProcessingError("Временная шкала содержит экран без его файла.")
                offset = max(0.0, segment.start_time - screen[1].start_time)
                self.render_material(segment, speaker, screen[0], partial, offset, audio)
            else:
                self.render_speaker(speaker, segment, partial, audio)
            os.replace(partial, target)

        combined = rendered_dir / "combined.mp4"
        self._concat.video(rendered, combined, duration)
        copy_atomically(combined, destination)
        return destination

    def render_speaker(
        self, speaker: Path, segment: CompositeSegment, destination: Path, audio: Path
    ) -> None:
        inputs = ["-ss", f"{segment.start_time:.3f}", "-i", str(speaker)]
        audio_index = 0
        if audio != speaker:
            # Сведённый звук — глобальная дорожка записи: перематываем её
            # к началу участка вместе с видео.
            inputs += ["-ss", f"{segment.start_time:.3f}", "-i", str(audio)]
            audio_index = 1
        self._ffmpeg.run(
            [*inputs, "-t", f"{segment.duration:.3f}", "-vf", fit_filter(),
             "-map", "0:v:0", "-map", f"{audio_index}:a:0?", *encode_args(), str(destination)],
            f"Участок со спикером ({format_duration(segment.duration)})",
        )

    def render_material(
        self,
        segment: CompositeSegment,
        speaker: Path,
        background: Path,
        destination: Path,
        background_offset: float,
        audio: Path,
    ) -> None:
        if segment.kind == "presentation":
            inputs = ["-loop", "1", "-framerate", str(FPS), "-i", str(background)]
        else:
            inputs = ["-ss", f"{background_offset:.3f}", "-i", str(background)]
        inputs += ["-ss", f"{segment.start_time:.3f}", "-i", str(speaker)]
        audio_index = 1
        if audio != speaker:
            inputs += ["-ss", f"{segment.start_time:.3f}", "-i", str(audio)]
            audio_index = 2
        filter_complex = (
            f"[0:v]{fit_filter()}[background];"
            f"[1:v]{pip_filter()}[speaker];"
            f"[background][speaker]overlay=main_w-overlay_w-{MARGIN}:{MARGIN}:format=auto"
            ":eof_action=pass[v]"
        )
        self._ffmpeg.run(
            [*inputs, "-filter_complex", filter_complex, "-map", "[v]",
             "-map", f"{audio_index}:a:0?", "-t", f"{segment.duration:.3f}",
             *encode_args(), str(destination)],
            f"Участок с материалом и спикером ({format_duration(segment.duration)})",
        )

    def _download_slides(
        self, presentations: Sequence[PresentationStream], directory: Path, access: MediaAccess
    ) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for presentation in presentations:
            for update in presentation.updates:
                if not update.image_url or update.image_url in result:
                    continue
                digest = hashlib.sha256(update.image_url.split("?")[0].encode()).hexdigest()[:16]
                target = directory / f"slide-{digest}{_image_suffix(update.image_url)}"
                for url in (update.image_url, update.alt_image_url):
                    if not url:
                        continue
                    try:
                        self._fetcher.fetch_file(url, target, access)
                    except MtsLinkError as exc:
                        LOG.warning("Слайд «%s» не скачан: %s", update.slide_name or digest, exc)
                        continue
                    result[update.image_url] = target
                    break
        return result


def _image_suffix(url: str) -> str:
    """image2 в ffmpeg выбирает декодер по расширению, поэтому сохраняем его."""

    path = url.split("?", 1)[0].lower()
    for suffix in (".png", ".webp", ".jpeg", ".jpg", ".bmp"):
        if path.endswith(suffix):
            return suffix
    return ".jpg"
