"""Чтение списка ссылок из файлов, stdin и аргументов командной строки."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from pathlib import Path

from mtslink_downloader.domain.errors import InvalidLinkError
from mtslink_downloader.domain.links import parse_link
from mtslink_downloader.domain.models import DownloadJob

_URL_RE = re.compile(r"https?://[^\s<>\"'|]+")
_TRAILING = ".,;)]}»"


def parse_lines(lines: Iterable[str]) -> tuple[list[DownloadJob], list[str]]:
    """Разобрать строки в задания. Возвращает задания и предупреждения.

    Формат строки свободный:

    * ``https://...`` — одна ссылка;
    * ``https://... Имя файла`` или ``https://... | Имя`` — ссылка и имя;
    * ``Название урока: https://...`` — имя перед ссылкой тоже подходит;
    * строки, начинающиеся с ``#`` или ``//``, и пустые строки пропускаются;
    * если в строке несколько ссылок, каждая становится отдельным заданием.

    Повторы ссылок отбрасываются с сохранением порядка.
    """

    jobs: list[DownloadJob] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for number, raw in enumerate(lines, start=1):
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith(("#", "//", ";")):
            continue
        urls = [match.group(0).rstrip(_TRAILING) for match in _URL_RE.finditer(line)]
        if not urls:
            warnings.append(f"строка {number}: ссылка не найдена — {line[:80]}")
            continue
        name: str | None = None
        if len(urls) == 1:
            rest = _URL_RE.sub(" ", line)
            rest = re.sub(r"\s+", " ", rest).strip(" \t|;,-—:")
            name = rest or None
        for url in urls:
            try:
                link = parse_link(url)
            except InvalidLinkError as exc:
                warnings.append(f"строка {number}: {exc}")
                continue
            if link.url in seen:
                continue
            seen.add(link.url)
            jobs.append(DownloadJob(link=link, name=name))
    return jobs, warnings


class LinkListReader:
    """Собирает задания из аргументов и файлов списка (``-`` — stdin)."""

    def read(
        self, urls: Iterable[str], list_files: Iterable[Path]
    ) -> tuple[list[DownloadJob], list[str]]:
        lines: list[str] = list(urls)
        for path in list_files:
            if str(path) == "-":
                lines.extend(sys.stdin.read().splitlines())
                continue
            try:
                text = path.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError:
                text = path.read_text(encoding="cp1251")
            except OSError as exc:
                raise InvalidLinkError(f"Не удалось прочитать список {path}: {exc}") from exc
            lines.extend(text.splitlines())
        return parse_lines(lines)
