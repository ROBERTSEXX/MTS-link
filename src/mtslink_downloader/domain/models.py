"""Модели предметной области: запись, её источники, задания и результаты."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mtslink_downloader.domain.errors import StrategyAttempt
from mtslink_downloader.domain.links import RecordingLink

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# --------------------------------------------------------------------------
# Доступ к медиа: cookies и заголовки
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionCookie:
    """Cookie с доменом, чтобы не отправлять сессию на посторонние хосты."""

    name: str
    value: str
    domain: str

    def matches(self, host: str) -> bool:
        domain = self.domain.lstrip(".").lower()
        host = host.lower()
        return host == domain or host.endswith("." + domain)


@dataclass(frozen=True)
class MediaAccess:
    """Всё, что нужно для запроса медиафайла так же, как это делает плеер."""

    referer: str
    user_agent: str = DEFAULT_USER_AGENT
    cookies: tuple[SessionCookie, ...] = ()

    def headers_for(self, url: str) -> dict[str, str]:
        parsed_referer = urlparse(self.referer)
        headers = {
            "User-Agent": self.user_agent,
            "Referer": self.referer,
            "Origin": f"{parsed_referer.scheme}://{parsed_referer.netloc}",
        }
        host = urlparse(url).hostname or ""
        cookie = "; ".join(
            f"{item.name}={item.value}" for item in self.cookies if item.matches(host)
        )
        if cookie:
            headers["Cookie"] = cookie
        return headers


@dataclass(frozen=True)
class RecordDocument:
    """JSON-описание записи и условия доступа, при которых оно получено."""

    data: dict[str, Any]
    source: str
    access: MediaAccess


# --------------------------------------------------------------------------
# Запись и её источники
# --------------------------------------------------------------------------


@dataclass
class MediaSegment:
    """Один физический файл внутри логического потока.

    ``relative_time`` — начало сегмента относительно всей записи.
    ``trim_duration`` — длина активного хвоста первого файла (отрезает
    преролл). ``max_duration`` ограничивает файл с начала, если следующий
    snapshot заменяет его раньше конца.
    """

    source_url: str
    hls_url: str | None
    relative_time: float
    initial: bool = False
    trim_duration: float | None = None
    max_duration: float | None = None

    @property
    def any_url(self) -> str:
        return self.source_url or self.hls_url or ""

    @property
    def candidate_urls(self) -> list[str]:
        return [url for url in (self.source_url, self.hls_url) if url]


@dataclass
class VideoStream:
    """Логический видеопоток: камера спикера, экран или видео участника."""

    key: str
    title: str
    segments: list[MediaSegment]
    duration: float
    start_time: float
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    has_audio: bool = False

    @property
    def end_time(self) -> float:
        return self.start_time + self.duration


@dataclass
class AudioStream:
    """Отдельная аудиосессия, например вопрос слушателя с микрофона."""

    key: str
    title: str
    segments: list[MediaSegment]
    duration: float
    start_time: float
    participant: str | None = None
    codec: str | None = None

    @property
    def end_time(self) -> float:
        return self.start_time + self.duration


@dataclass
class PresentationUpdate:
    """Событие смены слайда или включения/выключения презентации."""

    relative_time: float
    is_active: bool
    image_url: str | None = None
    slide_name: str | None = None


@dataclass
class PresentationStream:
    """Презентация: исходный PDF и временная шкала показанных слайдов."""

    key: str
    title: str
    file_name: str
    source_url: str
    start_time: float
    duration: float
    slide_count: int
    updates: list[PresentationUpdate] = field(default_factory=list)

    @property
    def end_time(self) -> float:
        return self.start_time + self.duration


@dataclass
class CompositeSegment:
    """Участок сводного ролика: ``speaker``, ``presentation`` или ``screen``."""

    kind: str
    start_time: float
    duration: float
    image_url: str | None = None


SPEAKER_KEY = "speaker"
SCREEN_KEY = "screen-share"


@dataclass
class Recording:
    """Разобранная запись со всеми найденными источниками."""

    title: str
    duration: float
    video_streams: list[VideoStream] = field(default_factory=list)
    audio_streams: list[AudioStream] = field(default_factory=list)
    presentations: list[PresentationStream] = field(default_factory=list)

    @property
    def speaker(self) -> VideoStream | None:
        return next((item for item in self.video_streams if item.key == SPEAKER_KEY), None)

    @property
    def screen(self) -> VideoStream | None:
        return next((item for item in self.video_streams if item.key == SCREEN_KEY), None)

    @property
    def is_empty(self) -> bool:
        return not (self.video_streams or self.audio_streams)


# --------------------------------------------------------------------------
# Задания пакетной загрузки
# --------------------------------------------------------------------------


class ExportMode(Enum):
    """Что сохранить для записи."""

    AUTO = "auto"
    SPEAKER = "speaker"
    COMPOSITE = "composite"
    ALL = "all"


@dataclass(frozen=True)
class DownloadSettings:
    """Общие настройки пакета ссылок."""

    output_dir: Path
    mode: ExportMode = ExportMode.AUTO
    overwrite: bool = False
    keep_work_files: bool = False


@dataclass(frozen=True)
class DownloadJob:
    """Одна строка списка: ссылка и необязательное имя итогового файла."""

    link: RecordingLink
    name: str | None = None


class JobStatus(Enum):
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class JobResult:
    """Итог обработки одной ссылки."""

    job: DownloadJob
    status: JobStatus
    strategy: str | None = None
    outputs: tuple[Path, ...] = ()
    attempts: tuple[StrategyAttempt, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class BatchSummary:
    """Итог обработки всего списка."""

    results: tuple[JobResult, ...]

    def count(self, status: JobStatus) -> int:
        return sum(1 for item in self.results if item.status is status)

    @property
    def failed(self) -> tuple[JobResult, ...]:
        return tuple(item for item in self.results if item.status is JobStatus.FAILED)
