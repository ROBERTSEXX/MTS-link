"""Локальная обработка сегментов: обрезка, заполнители, нормализация, склейка."""

from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

from mtslink_downloader.domain.errors import MediaProcessingError
from mtslink_downloader.infrastructure.media.ffmpeg import Ffmpeg, format_duration

LOG = logging.getLogger(__name__)

NORMALIZED_FPS = 30
AUDIO_ARGS = ("-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2")
H264_ARGS = ("-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p")
VP9_ARGS = ("-c:v", "libvpx-vp9", "-deadline", "good", "-cpu-used", "4", "-crf", "18", "-b:v", "0")
STABLE_TS_ARGS = ("-max_interleave_delta", "0", "-avoid_negative_ts", "make_zero")


def _concat_list(paths: Sequence[Path], list_path: Path) -> Path:
    lines = []
    for path in paths:
        escaped = str(path.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return list_path


class SegmentEditor:
    """Операции над одним локальным файлом.

    Сначала всегда пробуется быстрое копирование потоков; если результат
    потерял нужную дорожку (seek между ключевыми кадрами), фрагмент
    перекодируется. Так длинные сегменты остаются без потери качества.
    """

    def __init__(self, ffmpeg: Ffmpeg) -> None:
        self._ffmpeg = ffmpeg

    # -- восстановление дорожек ------------------------------------------

    def ensure_tracks(
        self,
        source: Path,
        destination: Path,
        want_audio: bool,
        size: tuple[int, int] = (1280, 720),
    ) -> Path:
        """Добавить чёрное видео к аудио-only сегменту или тишину к немому."""

        types = self._ffmpeg.stream_types(source)
        has_video, has_audio = "video" in types, "audio" in types
        if has_video and (has_audio or not want_audio):
            return source
        if not has_video and not has_audio:
            raise MediaProcessingError(f"Сегмент {source.name} не содержит ни видео, ни звука.")

        duration = self._ffmpeg.duration(source)
        width, height = size
        common = [
            "-t",
            f"{duration:.3f}",
            *H264_ARGS,
            "-r",
            str(NORMALIZED_FPS),
            *AUDIO_ARGS,
            *STABLE_TS_ARGS,
            "-movflags",
            "+faststart",
            "-y",
            str(destination),
        ]
        if not has_video:
            LOG.warning("Сегмент %s содержит только звук: добавляю чёрный кадр", source.name)
            self._ffmpeg.run(
                [
                    "-f", "lavfi",
                    "-i", f"color=c=black:s={width}x{height}:r={NORMALIZED_FPS}:d={duration:.3f}",
                    "-i", str(source),
                    "-map", "0:v:0", "-map", "1:a:0",
                    *common,
                ],
                f"Добавление видеоряда к {source.name}",
            )
        else:
            LOG.warning("Сегмент %s без звука: добавляю тишину", source.name)
            self._ffmpeg.run(
                [
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                    "-i", str(source),
                    "-map", "1:v:0", "-map", "0:a:0",
                    *common,
                ],
                f"Добавление тишины к {source.name}",
            )
        return destination

    # -- заполнители ---------------------------------------------------------

    def video_gap(
        self, destination: Path, duration: float, size: tuple[int, int], with_audio: bool
    ) -> None:
        width, height = size
        args = [
            "-f", "lavfi",
            "-i", f"color=c=black:s={width}x{height}:r={NORMALIZED_FPS}:d={duration:.3f}",
        ]
        if with_audio:
            args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                     "-map", "0:v:0", "-map", "1:a:0"]
        else:
            args += ["-map", "0:v:0"]
        args += ["-t", f"{duration:.3f}", *H264_ARGS, "-r", str(NORMALIZED_FPS)]
        if with_audio:
            args += list(AUDIO_ARGS)
        args += ["-avoid_negative_ts", "make_zero", "-y", str(destination)]
        self._ffmpeg.run(args, f"Заполнение паузы видео ({format_duration(duration)})")

    def silence(self, destination: Path, duration: float) -> None:
        self._ffmpeg.run(
            [
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-t", f"{duration:.3f}", *AUDIO_ARGS,
                "-movflags", "+faststart", "-y", str(destination),
            ],
            f"Заполнение паузы звука ({format_duration(duration)})",
        )

    # -- обрезка -------------------------------------------------------------

    def keep_tail(self, source: Path, destination: Path, seconds: float) -> None:
        """Оставить последние ``seconds`` секунд (отрезать преролл)."""

        duration = self._ffmpeg.duration(source)
        if seconds <= 0 or seconds >= duration - 0.25:
            shutil.copy2(source, destination)
            return
        self._cut_video(source, destination, start=duration - seconds, seconds=seconds)

    def keep_head(self, source: Path, destination: Path, seconds: float) -> None:
        """Оставить первые ``seconds`` секунд (убрать перекрытие snapshots)."""

        if seconds <= 0:
            raise MediaProcessingError("Нельзя создать сегмент нулевой длительности.")
        duration = self._ffmpeg.duration(source)
        if seconds >= duration - 0.25:
            shutil.copy2(source, destination)
            return
        self._cut_video(source, destination, start=None, seconds=seconds)

    def keep_audio_tail(self, source: Path, destination: Path, seconds: float) -> None:
        duration = self._ffmpeg.duration(source)
        if seconds <= 0 or seconds >= duration - 0.25:
            shutil.copy2(source, destination)
            return
        self._cut_audio(source, destination, start=duration - seconds, seconds=seconds)

    def keep_audio_head(self, source: Path, destination: Path, seconds: float) -> None:
        if seconds <= 0:
            raise MediaProcessingError("Нельзя создать аудиосегмент нулевой длительности.")
        duration = self._ffmpeg.duration(source)
        if seconds >= duration - 0.25:
            shutil.copy2(source, destination)
            return
        self._cut_audio(source, destination, start=None, seconds=seconds)

    def _cut_video(
        self, source: Path, destination: Path, start: float | None, seconds: float
    ) -> None:
        seek = ["-ss", f"{start:.3f}"] if start is not None else []
        window = [*seek, "-t", f"{seconds:.3f}", "-map", "0:v:0", "-map", "0:a:0?"]
        self._ffmpeg.run(
            ["-i", str(source), *window, "-c", "copy",
             "-avoid_negative_ts", "make_zero", "-y", str(destination)],
            f"Обрезка сегмента ({seconds:.1f} с)",
        )
        required = {"video"} | ({"audio"} & self._ffmpeg.stream_types(source))
        if self._copy_is_complete(destination, required, seconds):
            return
        LOG.warning("Обрезка копированием потеряла часть дорожки, перекодирую фрагмент точно")
        destination.unlink(missing_ok=True)
        # Короткий фрагмент сохраняет кодек соседей, иначе concat-copy
        # смешает VP9 и H.264 в одном контейнере.
        if self._ffmpeg.video_codec(source) == "vp9":
            encoder: tuple[str, ...] = (*VP9_ARGS, "-pix_fmt", "yuv420p")
        else:
            encoder = H264_ARGS
        self._ffmpeg.run(
            ["-i", str(source), *window, *encoder, *AUDIO_ARGS,
             "-avoid_negative_ts", "make_zero", "-y", str(destination)],
            f"Перекодирование обрезанного сегмента ({seconds:.1f} с)",
        )

    def _copy_is_complete(self, path: Path, required: set[str], seconds: float) -> bool:
        """Копирование не потеряло начало ни одной дорожки.

        При ``-c copy`` ffmpeg отбрасывает видео до ближайшего ключевого
        кадра: дорожка формально есть, но начинается на несколько секунд
        позже звука. Такой файл после склейки сдвигает всё видео.
        """

        windows = self._ffmpeg.stream_windows(path)
        if not required.issubset(windows):
            return False
        origin = min(start for start, _ in windows.values())
        tolerance = max(0.5, seconds * 0.02)
        for kind in required:
            start, duration = windows[kind]
            if start - origin > tolerance or duration < seconds - tolerance:
                return False
        return True

    def _cut_audio(
        self, source: Path, destination: Path, start: float | None, seconds: float
    ) -> None:
        seek = ["-ss", f"{start:.3f}"] if start is not None else []
        window = [*seek, "-t", f"{seconds:.3f}", "-map", "0:a:0", "-vn"]
        self._ffmpeg.run(
            ["-i", str(source), *window, "-c:a", "copy",
             "-avoid_negative_ts", "make_zero", "-y", str(destination)],
            f"Обрезка аудиосегмента ({seconds:.1f} с)",
        )
        if "audio" in self._ffmpeg.stream_types(destination):
            return
        destination.unlink(missing_ok=True)
        self._ffmpeg.run(
            ["-i", str(source), *window, *AUDIO_ARGS,
             "-avoid_negative_ts", "make_zero", "-y", str(destination)],
            f"Перекодирование аудиосегмента ({seconds:.1f} с)",
        )

    # -- нормализация ----------------------------------------------------

    def normalize(self, source: Path, destination: Path, size: tuple[int, int]) -> None:
        """Привести сегмент к общему H.264/AAC-профилю для безопасной склейки."""

        types = self._ffmpeg.stream_types(source)
        if "video" not in types:
            raise MediaProcessingError(f"Сегмент {source.name} не содержит видеопоток.")
        width, height = size
        args = [
            "-i", str(source), "-map", "0:v:0",
            "-vf",
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,setpts=PTS-STARTPTS",
            "-r", str(NORMALIZED_FPS), *H264_ARGS,
        ]
        if "audio" in types:
            args += ["-map", "0:a:0", "-af", "asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0",
                     *AUDIO_ARGS]
        args += [*STABLE_TS_ARGS, "-movflags", "+faststart", "-y", str(destination)]
        self._ffmpeg.run(args, f"Нормализация сегмента {source.name}")


class Concatenator:
    """Склейка локальных частей: копированием, а при проблемах — перекодированием."""

    def __init__(self, ffmpeg: Ffmpeg, editor: SegmentEditor) -> None:
        self._ffmpeg = ffmpeg
        self._editor = editor

    def video(self, parts: Sequence[Path], destination: Path, duration: float) -> None:
        if not parts:
            raise MediaProcessingError("Нет частей для склейки видео.")
        list_path = _concat_list(parts, destination.with_suffix(".concat.txt"))
        try:
            try:
                self._require_same_parameters(parts)
                self._run_concat(list_path, destination, duration, video=True)
            except MediaProcessingError as exc:
                LOG.warning("Склейка копированием невозможна (%s), перекодирую части", exc)
                self._normalized_concat(parts, destination, duration)
        finally:
            list_path.unlink(missing_ok=True)

    def audio(self, parts: Sequence[Path], destination: Path, duration: float) -> None:
        if not parts:
            raise MediaProcessingError("Нет частей для склейки аудио.")
        list_path = _concat_list(parts, destination.with_suffix(".concat.txt"))
        try:
            try:
                self._require_same_parameters(parts)
                self._run_concat(list_path, destination, duration, video=False)
            except MediaProcessingError:
                LOG.warning("Склейка аудио копированием невозможна, перекодирую")
                args = ["-f", "concat", "-safe", "0", "-i", str(list_path),
                        "-map", "0:a:0", "-vn",
                        "-af", "asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0",
                        *AUDIO_ARGS, "-movflags", "+faststart"]
                if duration > 0:
                    args += ["-t", f"{duration:.3f}"]
                self._ffmpeg.run([*args, "-y", str(destination)], "Перекодирование аудиочастей")
        finally:
            list_path.unlink(missing_ok=True)

    def _require_same_parameters(self, parts: Sequence[Path]) -> None:
        """concat-copy молча портит файл, если у частей разные кодеки/частоты.

        Например, перекодированный фрагмент 48 кГц стерео и исходник 44,1 кГц
        моно склеиваются без ошибки, но звук в результате короче и с
        искажениями. Поэтому параметры сверяются заранее.
        """

        signatures = {self._ffmpeg.signature(part) for part in parts}
        if len(signatures) > 1:
            raise MediaProcessingError("у частей разные параметры кодирования")

    def _run_concat(self, list_path: Path, destination: Path, duration: float, video: bool) -> None:
        maps = ["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy"] if video else [
            "-map", "0:a:0", "-vn", "-c:a", "copy"]
        args = ["-f", "concat", "-safe", "0", "-i", str(list_path), *maps,
                "-movflags", "+faststart"]
        if duration > 0:
            args += ["-t", f"{duration:.3f}"]
        self._ffmpeg.run([*args, "-y", str(destination)], "Склейка частей")

    def _normalized_concat(self, parts: Sequence[Path], destination: Path, duration: float) -> None:
        with tempfile.TemporaryDirectory(prefix="mtslink-concat-") as temp_name:
            temp_dir = Path(temp_name)
            width, height = self._ffmpeg.video_size(parts[0])
            width = max(2, (width or 1280) - (width or 1280) % 2)
            height = max(2, (height or 720) - (height or 720) % 2)
            normalized = []
            for index, part in enumerate(parts, start=1):
                target = temp_dir / f"part-{index:04d}.mp4"
                self._editor.normalize(part, target, (width, height))
                normalized.append(target)
            list_path = _concat_list(normalized, temp_dir / "parts.txt")
            self._run_concat(list_path, destination, duration, video=True)
