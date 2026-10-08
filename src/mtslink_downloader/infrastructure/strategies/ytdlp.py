"""Последний рубеж: универсальный загрузчик yt-dlp."""

from __future__ import annotations

import importlib.util
import logging
import shutil
import subprocess
import sys
from pathlib import Path

from mtslink_downloader.domain.errors import MediaProcessingError, StrategyUnavailableError
from mtslink_downloader.domain.links import RecordingLink
from mtslink_downloader.domain.models import DEFAULT_USER_AGENT, DownloadJob, DownloadSettings
from mtslink_downloader.infrastructure.storage.naming import OutputNamer

LOG = logging.getLogger(__name__)


class YtDlpStrategy:
    """Запускает yt-dlp (бинарник из PATH или модуль ``yt_dlp``).

    yt-dlp знает сотни плееров и общий извлекатель видео со страницы, поэтому
    иногда справляется там, где специализированные способы не сработали.
    """

    name = "ytdlp"

    def __init__(self, cookies_file: Path | None = None, timeout: float = 6 * 3600) -> None:
        self._cookies_file = cookies_file
        self._timeout = timeout

    def supports(self, link: RecordingLink) -> bool:
        return True

    def _command(self) -> list[str]:
        binary = shutil.which("yt-dlp")
        if binary:
            return [binary]
        if importlib.util.find_spec("yt_dlp") is not None:
            return [sys.executable, "-m", "yt_dlp"]
        raise StrategyUnavailableError("yt-dlp не установлен: pip install yt-dlp")

    def download(self, job: DownloadJob, settings: DownloadSettings) -> list[Path]:
        stem = OutputNamer(settings.output_dir).stem(job, None)
        destination = OutputNamer.main(stem)
        if destination.exists() and not settings.overwrite:
            return [destination]
        stem.parent.mkdir(parents=True, exist_ok=True)
        command = [
            *self._command(),
            "--no-playlist",
            "--no-progress",
            "--retries", "10",
            "--fragment-retries", "10",
            "--merge-output-format", "mp4",
            "--user-agent", DEFAULT_USER_AGENT,
            "--add-header", f"Referer:{job.link.url}",
            "-o", f"{stem}.%(ext)s",
        ]
        if settings.overwrite:
            command.append("--force-overwrites")
        if self._cookies_file:
            command += ["--cookies", str(self._cookies_file)]
        command.append(job.link.url)
        LOG.info("Запуск yt-dlp")
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=self._timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise MediaProcessingError("yt-dlp не завершился вовремя") from exc
        if completed.returncode != 0:
            lines = [line for line in (completed.stderr or "").splitlines() if "ERROR" in line]
            raise MediaProcessingError((lines[-1] if lines else "yt-dlp завершился с ошибкой")[:300])

        produced = [
            path
            for path in stem.parent.glob(f"{_glob_escape(stem.name)}.*")
            if path.suffix.lower() in {".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".mov"}
        ]
        if not produced:
            raise MediaProcessingError("yt-dlp не создал медиафайл")
        return sorted(produced)


def _glob_escape(text: str) -> str:
    return "".join(f"[{char}]" if char in "*?[]" else char for char in text)
