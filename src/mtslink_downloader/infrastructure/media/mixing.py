"""Сведение звука и упаковка дорожек в итоговый контейнер."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from mtslink_downloader.infrastructure.media.editing import AUDIO_ARGS, H264_ARGS
from mtslink_downloader.infrastructure.media.ffmpeg import Ffmpeg

# ffmpeg держит открытыми все входы одновременно; большие сведения делаются
# по частям, чтобы не упираться в лимиты файловых дескрипторов и памяти.
MAX_INPUTS_PER_PASS = 24


@dataclass(frozen=True)
class MixInput:
    """Кусок ``[file_offset, file_offset + length]`` файла с задержкой ``delay``."""

    path: Path
    file_offset: float
    length: float
    delay: float


class AudioMixer:
    """Сводит куски звука на общую шкалу длиной ``duration``.

    Основа — тишина нужной длины, поэтому результат всегда ровно такой
    длительности, а каждый кусок звучит в свой момент. Громкость участников
    не делится на число входов (``normalize=0``): иначе при десятках
    микрофонов голос спикера стал бы еле слышен. От перегрузки защищает
    лимитер.
    """

    def __init__(self, ffmpeg: Ffmpeg) -> None:
        self._ffmpeg = ffmpeg

    def mix_pieces(self, inputs: Sequence[MixInput], destination: Path, duration: float) -> None:
        if len(inputs) > MAX_INPUTS_PER_PASS:
            self._mix_in_batches(inputs, destination, duration)
            return
        args = ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        filters = ["[0:a]anull[base]"]
        labels = ["[base]"]
        for index, item in enumerate(inputs, start=1):
            args += ["-i", str(item.path)]
            delay_ms = max(0, round(item.delay * 1000))
            filters.append(
                f"[{index}:a:0]atrim=start={item.file_offset:.3f}:duration={item.length:.3f},"
                f"asetpts=PTS-STARTPTS,aresample=48000,"
                f"aformat=sample_rates=48000:channel_layouts=stereo,"
                f"adelay={delay_ms}:all=1[a{index}]"
            )
            labels.append(f"[a{index}]")
        filters.append(
            "".join(labels)
            + f"amix=inputs={len(labels)}:duration=first:dropout_transition=0:normalize=0,"
            "alimiter=limit=0.95:level=disabled[mixed]"
        )
        partial = destination.with_name(f"{destination.stem}.part{destination.suffix}")
        self._ffmpeg.run(
            [*args, "-filter_complex", ";".join(filters), "-map", "[mixed]",
             "-t", f"{duration:.3f}", *AUDIO_ARGS, "-movflags", "+faststart",
             "-y", str(partial)],
            f"Сведение звука ({len(inputs)} фрагментов)",
        )
        os.replace(partial, destination)

    def _mix_in_batches(
        self, inputs: Sequence[MixInput], destination: Path, duration: float
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="mix-", dir=destination.parent) as temp_name:
            partials: list[MixInput] = []
            for number, start in enumerate(range(0, len(inputs), MAX_INPUTS_PER_PASS), start=1):
                partial = Path(temp_name) / f"part-{number:03d}.m4a"
                self.mix_pieces(inputs[start : start + MAX_INPUTS_PER_PASS], partial, duration)
                partials.append(MixInput(partial, 0.0, duration, 0.0))
            self.mix_pieces(partials, destination, duration)


class Muxer:
    """Собирает итоговый MP4 из готовых дорожек."""

    def __init__(self, ffmpeg: Ffmpeg) -> None:
        self._ffmpeg = ffmpeg

    def mux(self, video: Path, audio: Path, destination: Path) -> None:
        """Картинка и звук без перекодирования, запись через временный файл."""

        partial = destination.with_name(f"{destination.stem}.part{destination.suffix}")
        self._ffmpeg.run(
            ["-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
             "-c", "copy", "-movflags", "+faststart", "-y", str(partial)],
            "Упаковка видео и звука",
        )
        os.replace(partial, destination)

    def black_video(self, audio: Path, destination: Path, duration: float) -> None:
        """MP4 из одного звука: чёрный кадр на всю длительность."""

        partial = destination.with_name(f"{destination.stem}.part{destination.suffix}")
        self._ffmpeg.run(
            ["-f", "lavfi", "-i", f"color=c=black:s=1280x720:r=5:d={duration:.3f}",
             "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
             *H264_ARGS, "-c:a", "copy", "-shortest", "-movflags", "+faststart",
             "-y", str(partial)],
            "Создание видео из аудиозаписи",
        )
        os.replace(partial, destination)

    def audio_file(self, audio: Path, destination: Path) -> None:
        partial = destination.with_name(f"{destination.stem}.part{destination.suffix}")
        self._ffmpeg.run(
            ["-i", str(audio), "-map", "0:a:0", "-c", "copy", "-movflags", "+faststart",
             "-y", str(partial)],
            "Сохранение аудиофайла",
        )
        os.replace(partial, destination)
