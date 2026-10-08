"""Сведение звука и упаковка дорожек в итоговый контейнер."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from mtslink_downloader.domain.errors import MediaProcessingError
from mtslink_downloader.infrastructure.media.editing import AUDIO_ARGS, H264_ARGS
from mtslink_downloader.infrastructure.media.ffmpeg import Ffmpeg


class AudioMixer:
    """Накладывает голоса участников на основной звук по их времени старта."""

    def __init__(self, ffmpeg: Ffmpeg) -> None:
        self._ffmpeg = ffmpeg

    def mix(
        self,
        main: Path | None,
        extras: Sequence[tuple[Path, float]],
        destination: Path,
        duration: float,
    ) -> None:
        inputs: list[str] = []
        filters: list[str] = []
        labels: list[str] = []
        index = 0
        if main is not None:
            inputs += ["-i", str(main)]
            filters.append(f"[{index}:a:0]aresample=48000,asetpts=PTS-STARTPTS[main]")
            labels.append("[main]")
            index += 1
        else:
            # Без основного звука база — тишина нужной длины, чтобы задержки
            # участников отсчитывались от начала записи.
            inputs += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            filters.append(f"[{index}:a:0]asetpts=PTS-STARTPTS[main]")
            labels.append("[main]")
            index += 1
        for path, start_time in extras:
            inputs += ["-i", str(path)]
            delay_ms = max(0, round(start_time * 1000))
            label = f"[p{index}]"
            filters.append(
                f"[{index}:a:0]aresample=48000,asetpts=PTS-STARTPTS,adelay={delay_ms}:all=1{label}"
            )
            labels.append(label)
            index += 1
        if len(labels) == 1 and main is None:
            raise MediaProcessingError("Нет аудиодорожек для сведения.")
        filters.append(
            "".join(labels)
            + f"amix=inputs={len(labels)}:duration=first:dropout_transition=0:normalize=0,"
            "aresample=async=1:first_pts=0[mixed]"
        )
        args = [*inputs, "-filter_complex", ";".join(filters), "-map", "[mixed]", "-vn"]
        if duration > 0:
            args += ["-t", f"{duration:.3f}"]
        args += [*AUDIO_ARGS, "-movflags", "+faststart", "-y", str(destination)]
        self._ffmpeg.run(args, "Сведение звука спикера и участников")


class Muxer:
    """Собирает итоговый MP4 из готовых дорожек."""

    def __init__(self, ffmpeg: Ffmpeg) -> None:
        self._ffmpeg = ffmpeg

    def replace_audio(self, video: Path, audio: Path, destination: Path) -> None:
        """Видео копируется без перекодирования, звук берётся сведённый."""

        self._ffmpeg.run(
            ["-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
             "-c:v", "copy", *AUDIO_ARGS, "-shortest", "-movflags", "+faststart",
             "-y", str(destination)],
            "Упаковка видео со сведённым звуком",
        )

    def black_video(self, audio: Path, destination: Path, duration: float) -> None:
        """MP4 из одного звука: чёрный кадр 1280×720 на всю длительность."""

        self._ffmpeg.run(
            ["-f", "lavfi", "-i", f"color=c=black:s=1280x720:r=5:d={duration:.3f}",
             "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
             *H264_ARGS, *AUDIO_ARGS, "-shortest", "-movflags", "+faststart",
             "-y", str(destination)],
            "Создание видео из аудиозаписи",
        )
