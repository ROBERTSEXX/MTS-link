"""Корень композиции: единственное место, где выбираются реализации портов."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from mtslink_downloader.application.export_plan import ExportPlanner
from mtslink_downloader.application.fallback import (
    FallbackDownloader,
    FallbackRecordParser,
    FallbackRecordSource,
)
from mtslink_downloader.application.ports import DownloadStrategy, ProgressReporter, RecordSource
from mtslink_downloader.application.record_strategy import RecordTimelineStrategy
from mtslink_downloader.application.use_cases.download_batch import DownloadBatch
from mtslink_downloader.application.use_cases.inspect_recording import InspectRecording
from mtslink_downloader.domain.errors import InvalidLinkError
from mtslink_downloader.domain.models import SessionCookie
from mtslink_downloader.infrastructure.browser.playwright_browser import (
    BrowserOptions,
    LoginPrompt,
    PlaywrightBrowser,
)
from mtslink_downloader.infrastructure.http import HttpClient
from mtslink_downloader.infrastructure.media.assembler import SegmentPreparer, TrackBuilder
from mtslink_downloader.infrastructure.media.composite import CompositeRenderer
from mtslink_downloader.infrastructure.media.editing import Concatenator, SegmentEditor
from mtslink_downloader.infrastructure.media.enricher import FfprobeEnricher
from mtslink_downloader.infrastructure.media.exporter import FfmpegRecordingExporter
from mtslink_downloader.infrastructure.media.fetching import SegmentFetcher
from mtslink_downloader.infrastructure.media.ffmpeg import Ffmpeg
from mtslink_downloader.infrastructure.media.mixing import AudioMixer, Muxer
from mtslink_downloader.infrastructure.parsing.flat import FlatUrlRecordParser
from mtslink_downloader.infrastructure.parsing.structured import StructuredRecordParser
from mtslink_downloader.infrastructure.sources.api import ApiRecordSource
from mtslink_downloader.infrastructure.sources.browser import BrowserRecordSource
from mtslink_downloader.infrastructure.sources.endpoints import DEFAULT_API_HOSTS
from mtslink_downloader.infrastructure.storage.catalog import JsonCompletedCatalog
from mtslink_downloader.infrastructure.storage.cookies import load_cookies_file, session_cookie
from mtslink_downloader.infrastructure.storage.report import JsonBatchReportWriter
from mtslink_downloader.infrastructure.strategies.browser_capture import BrowserCaptureStrategy
from mtslink_downloader.infrastructure.strategies.direct import DirectMediaStrategy
from mtslink_downloader.infrastructure.strategies.media_saver import MediaUrlSaver
from mtslink_downloader.infrastructure.strategies.ytdlp import YtDlpStrategy

# Порядок по умолчанию: от быстрых и точных способов к универсальным.
STRATEGY_NAMES: tuple[str, ...] = ("direct", "api", "browser", "sniff", "ytdlp")
STRATEGY_HELP = {
    "direct": "прямая ссылка на .mp4/.m3u8/.mpd",
    "api": "журнал записи через API МТС Линк + сборка ffmpeg",
    "browser": "журнал записи через браузер (cookies и вход) + сборка ffmpeg",
    "sniff": "перехват потока, который запрашивает плеер на странице",
    "ytdlp": "универсальный загрузчик yt-dlp",
}


def default_profile_dir() -> Path:
    return Path.home() / ".mtslink-downloader" / "browser-profile"


@dataclass(frozen=True)
class AppConfig:
    output_dir: Path
    strategies: tuple[str, ...] = STRATEGY_NAMES
    session_id: str | None = None
    cookies_file: Path | None = None
    headed: bool = False
    login: bool = False
    profile_dir: Path | None = field(default_factory=default_profile_dir)
    browser_channel: str | None = None
    browser_path: str | None = None
    api_hosts: tuple[str, ...] = DEFAULT_API_HOSTS
    browser_wait_seconds: float = 12.0


def parse_strategy_list(value: str) -> tuple[str, ...]:
    names = tuple(dict.fromkeys(item.strip().lower() for item in value.split(",") if item.strip()))
    unknown = [name for name in names if name not in STRATEGY_NAMES]
    if unknown or not names:
        raise InvalidLinkError(
            "Неизвестные способы: " + ", ".join(unknown or ["(пусто)"])
            + ". Доступны: " + ", ".join(STRATEGY_NAMES)
        )
    return names


@dataclass
class Application:
    batch: DownloadBatch
    inspect: InspectRecording
    strategy_names: tuple[str, ...]


class Container:
    """Создаёт адаптеры один раз и связывает их со сценариями."""

    def __init__(self, config: AppConfig, login_prompt: LoginPrompt | None = None) -> None:
        self.config = config
        self.ffmpeg = Ffmpeg()
        self.http = HttpClient()
        self.cookies = self._cookies()
        self.browser = PlaywrightBrowser(
            BrowserOptions(
                headless=not config.headed,
                profile_dir=config.profile_dir,
                channel=config.browser_channel,
                executable_path=config.browser_path,
            )
        )
        self.login_prompt = login_prompt if config.login else None

        self.planner = ExportPlanner()
        self.parser = FallbackRecordParser(
            [StructuredRecordParser(), FlatUrlRecordParser(self.ffmpeg)]
        )
        self.enricher = FfprobeEnricher(self.ffmpeg)
        fetcher = SegmentFetcher(self.ffmpeg, self.http)
        editor = SegmentEditor(self.ffmpeg)
        concatenator = Concatenator(self.ffmpeg, editor)
        self.exporter = FfmpegRecordingExporter(
            preparer=SegmentPreparer(self.ffmpeg, fetcher),
            tracks=TrackBuilder(self.ffmpeg, editor, concatenator, AudioMixer(self.ffmpeg)),
            composite=CompositeRenderer(self.ffmpeg, fetcher, concatenator),
            muxer=Muxer(self.ffmpeg),
            fetcher=fetcher,
        )
        self.saver = MediaUrlSaver(self.ffmpeg, self.http)

        self.api_source = ApiRecordSource(
            self.http, self.cookies, config.api_hosts, session_id=config.session_id
        )
        self.browser_source = BrowserRecordSource(
            self.browser,
            login_prompt=self.login_prompt,
            wait_seconds=config.browser_wait_seconds,
            api_hosts=config.api_hosts,
        )

    def _cookies(self) -> tuple[SessionCookie, ...]:
        cookies: list[SessionCookie] = []
        if self.config.cookies_file:
            cookies.extend(load_cookies_file(self.config.cookies_file))
        if self.config.session_id:
            cookies.append(session_cookie(self.config.session_id))
        return tuple(cookies)

    def _record_strategy(self, name: str, source: RecordSource) -> RecordTimelineStrategy:
        return RecordTimelineStrategy(
            name=name,
            source=source,
            parser=self.parser,
            enricher=self.enricher,
            planner=self.planner,
            exporter=self.exporter,
        )

    def strategy(self, name: str) -> DownloadStrategy:
        factories: dict[str, Callable[[], DownloadStrategy]] = {
            "direct": lambda: DirectMediaStrategy(self.saver, self.cookies),
            "api": lambda: self._record_strategy("api", self.api_source),
            "browser": lambda: self._record_strategy("browser", self.browser_source),
            "sniff": lambda: BrowserCaptureStrategy(self.browser, self.ffmpeg, self.saver),
            "ytdlp": lambda: YtDlpStrategy(self.config.cookies_file),
        }
        return factories[name]()

    def record_sources(self) -> Sequence[RecordSource]:
        sources: list[RecordSource] = []
        if "api" in self.config.strategies:
            sources.append(self.api_source)
        if "browser" in self.config.strategies:
            sources.append(self.browser_source)
        return sources or [self.api_source, self.browser_source]

    def build(self, reporter: ProgressReporter) -> Application:
        downloader = FallbackDownloader([self.strategy(name) for name in self.config.strategies])
        batch = DownloadBatch(
            downloader=downloader,
            catalog=JsonCompletedCatalog(self.config.output_dir),
            reporter=reporter,
            report_writer=JsonBatchReportWriter(self.config.output_dir),
        )
        inspect = InspectRecording(
            source=FallbackRecordSource(self.record_sources()),
            parser=self.parser,
            enricher=self.enricher,
            planner=self.planner,
        )
        return Application(batch=batch, inspect=inspect, strategy_names=downloader.strategy_names)
