"""Тонкая обёртка над ffmpeg/ffprobe: запуск команд и чтение метаданных."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from mtslink_downloader.domain.errors import MediaProcessingError

LOG = logging.getLogger(__name__)

# Параметры http-протокола ffmpeg: переподключение при обрывах соединения.
RECONNECT_ARGS = (
    "-reconnect",
    "1",
    "-reconnect_streamed",
    "1",
    "-reconnect_on_network_error",
    "1",
    "-reconnect_delay_max",
    "15",
)


def format_duration(seconds: float) -> str:
    seconds_int = max(0, int(seconds))
    hours, remainder = divmod(seconds_int, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def header_block(headers: dict[str, str]) -> str:
    """Заголовки в формате параметра ``-headers`` ffmpeg."""

    return "".join(f"{key}: {value}\r\n" for key, value in headers.items())


def remote_input_args(url: str, headers: dict[str, str]) -> list[str]:
    """Аргументы для чтения удалённого файла так же, как его читает плеер."""

    args: list[str] = []
    if url.startswith(("http://", "https://")):
        args.extend(RECONNECT_ARGS)
        if headers:
            args.extend(["-headers", header_block(headers)])
    args.extend(["-i", url])
    return args


class Ffmpeg:
    """Запуск ffmpeg и ffprobe с понятными ошибками."""

    def __init__(self, ffmpeg_path: str | None = None, ffprobe_path: str | None = None) -> None:
        self._ffmpeg_path = ffmpeg_path
        self._ffprobe_path = ffprobe_path

    @property
    def ffmpeg(self) -> str:
        path = self._ffmpeg_path or shutil.which("ffmpeg")
        if not path:
            raise MediaProcessingError(
                "Не найден ffmpeg. Установите его (Windows: winget install ffmpeg, "
                "macOS: brew install ffmpeg, Linux: apt install ffmpeg)."
            )
        return path

    @property
    def ffprobe(self) -> str:
        path = self._ffprobe_path or shutil.which("ffprobe")
        if not path:
            raise MediaProcessingError("Не найден ffprobe (обычно ставится вместе с ffmpeg).")
        return path

    def available(self) -> bool:
        return bool((self._ffmpeg_path or shutil.which("ffmpeg")) and (
            self._ffprobe_path or shutil.which("ffprobe")
        ))

    def run(self, args: Sequence[str], description: str) -> None:
        command = [self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", *args]
        LOG.debug(description)
        completed = subprocess.run(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, check=False
        )
        if completed.returncode != 0:
            details = (completed.stderr or "").strip().splitlines()[-3:]
            raise MediaProcessingError(
                f"ffmpeg не смог выполнить операцию «{description}»: " + " | ".join(details)
            )

    # ------------------------------------------------------------------
    # ffprobe
    # ------------------------------------------------------------------

    def _probe(self, args: Sequence[str]) -> dict[str, Any]:
        completed = subprocess.run(
            [self.ffprobe, "-v", "error", *args, "-of", "json"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            return {}
        try:
            data = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def duration(self, path: Path) -> float:
        data = self._probe(["-show_entries", "format=duration", str(path)])
        duration = _to_float((data.get("format") or {}).get("duration"))
        if duration is None:
            raise MediaProcessingError(f"Не удалось определить длительность {path.name}")
        return duration

    def stream_types(self, path: Path) -> set[str]:
        data = self._probe(["-show_entries", "stream=codec_type", str(path)])
        return {
            str(stream.get("codec_type"))
            for stream in data.get("streams") or []
            if isinstance(stream, dict) and stream.get("codec_type")
        }

    def is_complete(self, path: Path, expected_duration: float, tolerance: float = 1.0) -> bool:
        """Готовый промежуточный файл можно взять повторно, не пересобирая.

        Промежуточные файлы пишутся атомарно (через временное имя), поэтому
        файл с правильной длительностью — точно законченный результат.
        """

        if not path.exists() or path.stat().st_size == 0:
            return False
        data = self._probe(["-show_entries", "format=duration", str(path)])
        duration = _to_float((data.get("format") or {}).get("duration"))
        return duration is not None and abs(duration - expected_duration) <= tolerance

    def stream_windows(self, path: Path) -> dict[str, tuple[float, float]]:
        """``тип → (начало, длительность)`` первой дорожки каждого типа."""

        data = self._probe(["-show_entries", "stream=codec_type,start_time,duration", str(path)])
        windows: dict[str, tuple[float, float]] = {}
        for stream in data.get("streams") or []:
            if not isinstance(stream, dict):
                continue
            kind = str(stream.get("codec_type") or "")
            start = _to_float(stream.get("start_time"))
            duration = _to_float(stream.get("duration"))
            if kind and kind not in windows and start is not None and duration is not None:
                windows[kind] = (start, duration)
        return windows

    def signature(self, path: Path) -> tuple[tuple[str, ...], ...]:
        """Параметры дорожек, которые должны совпадать для склейки копированием."""

        data = self._probe(
            [
                "-show_entries",
                "stream=codec_type,codec_name,profile,width,height,pix_fmt,sample_rate,channels",
                str(path),
            ]
        )
        result = []
        for stream in data.get("streams") or []:
            if not isinstance(stream, dict):
                continue
            kind = str(stream.get("codec_type") or "")
            keys: tuple[str, ...]
            if kind == "video":
                keys = ("codec_name", "profile", "width", "height", "pix_fmt")
            elif kind == "audio":
                keys = ("codec_name", "profile", "sample_rate", "channels")
            else:
                continue
            result.append((kind, *(str(stream.get(key)) for key in keys)))
        return tuple(sorted(result))

    def video_codec(self, path: Path) -> str | None:
        stream = self._first_stream(path, "v:0", "stream=codec_name")
        return str(stream["codec_name"]) if stream.get("codec_name") else None

    def video_size(self, path: Path) -> tuple[int | None, int | None]:
        stream = self._first_stream(path, "v:0", "stream=width,height")
        try:
            return int(stream["width"]), int(stream["height"])
        except (KeyError, TypeError, ValueError):
            return None, None

    def probe_remote(self, url: str, headers: dict[str, str], selector: str = "v:0") -> dict[str, Any]:
        """Метаданные удалённого файла без скачивания: кодек, размер, длительность."""

        args: list[str] = []
        if url.startswith(("http://", "https://")) and headers:
            args.extend(["-headers", header_block(headers)])
        args.extend(
            [
                "-select_streams",
                selector,
                "-show_entries",
                "stream=codec_name,codec_type,width,height:format=duration",
                url,
            ]
        )
        data = self._probe(args)
        streams = data.get("streams") or []
        result: dict[str, Any] = dict(streams[0]) if streams and isinstance(streams[0], dict) else {}
        duration = _to_float((data.get("format") or {}).get("duration"))
        if duration is not None:
            result["duration"] = duration
        return result

    def remote_stream_types(self, url: str, headers: dict[str, str]) -> tuple[set[str], float | None]:
        args: list[str] = []
        if url.startswith(("http://", "https://")) and headers:
            args.extend(["-headers", header_block(headers)])
        args.extend(["-show_entries", "stream=codec_type:format=duration", url])
        data = self._probe(args)
        types = {
            str(stream.get("codec_type"))
            for stream in data.get("streams") or []
            if isinstance(stream, dict) and stream.get("codec_type")
        }
        return types, _to_float((data.get("format") or {}).get("duration"))

    def _first_stream(self, path: Path, selector: str, entries: str) -> dict[str, Any]:
        data = self._probe(["-select_streams", selector, "-show_entries", entries, str(path)])
        streams = data.get("streams") or []
        return dict(streams[0]) if streams and isinstance(streams[0], dict) else {}


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
