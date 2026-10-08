"""Безопасные и уникальные имена итоговых файлов."""

from __future__ import annotations

import re
from pathlib import Path

from mtslink_downloader.domain.models import DownloadJob

_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(name: str, fallback: str, limit: int = 150) -> str:
    cleaned = _FORBIDDEN.sub("_", name).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or fallback)[:limit].rstrip(" .") or fallback


class OutputNamer:
    """Строит пути результатов внутри папки пакета.

    Имя из списка ссылок используется как есть. Иначе берётся название
    записи с идентификатором в квадратных скобках: у разных вебинаров часто
    одинаковые названия, а идентификатор исключает перезапись чужого файла.
    """

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir

    def stem(self, job: DownloadJob, title: str | None) -> Path:
        identifier = job.link.identifier
        if job.name:
            supplied = Path(job.name)
            if supplied.is_absolute():
                return supplied.with_suffix("") if supplied.suffix else supplied
            name = supplied.stem if supplied.suffix else supplied.name
            return self._output_dir / safe_name(name, identifier)
        base = safe_name(title or "", f"mts-link-{identifier}")
        if identifier and identifier not in base:
            base = f"{base} [{identifier}]"
        return self._output_dir / base

    @staticmethod
    def main(stem: Path) -> Path:
        return stem.with_name(stem.name + ".mp4")

    @staticmethod
    def video(stem: Path, key: str) -> Path:
        return stem.with_name(f"{stem.name}-{key}.mp4")

    @staticmethod
    def audio(stem: Path, key: str) -> Path:
        return stem.with_name(f"{stem.name}-{key}.m4a")

    @staticmethod
    def presentation(stem: Path, key: str, suffix: str = ".pdf") -> Path:
        return stem.with_name(f"{stem.name}-{key}{suffix}")

    def work_dir(self, job: DownloadJob) -> Path:
        """Рабочая папка задания: кэш сегментов для продолжения после сбоя."""

        return self._output_dir / ".mtslink-work" / safe_name(job.link.identifier, "job")
