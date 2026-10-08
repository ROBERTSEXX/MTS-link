import ast
import json
from pathlib import Path

import pytest

from mtslink_downloader.application.fallback import FallbackDownloader, FallbackRecordSource
from mtslink_downloader.application.use_cases.download_batch import DownloadBatch
from mtslink_downloader.domain.errors import (
    AccessDeniedError,
    AllStrategiesFailedError,
    RecordUnavailableError,
)
from mtslink_downloader.domain.links import parse_link
from mtslink_downloader.domain.models import DownloadJob, DownloadSettings, JobStatus
from mtslink_downloader.infrastructure.sources.api import ApiRecordSource
from mtslink_downloader.infrastructure.storage.catalog import JsonCompletedCatalog
from mtslink_downloader.infrastructure.storage.report import JsonBatchReportWriter

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "mtslink_downloader"


class FakeStrategy:
    def __init__(self, name, outcome, supported=True):
        self.name = name
        self._outcome = outcome
        self._supported = supported
        self.calls = 0

    def supports(self, link):
        return self._supported

    def download(self, job, settings):
        self.calls += 1
        if isinstance(self._outcome, Exception):
            raise self._outcome
        path = settings.output_dir / f"{job.link.identifier}-{self.name}.mp4"
        path.write_bytes(b"video")
        return [path]


class SilentReporter:
    def __init__(self):
        self.events = []

    def job_started(self, job, index, total):
        self.events.append(("start", index))

    def strategy_started(self, job, strategy):
        self.events.append(("try", strategy))

    def strategy_failed(self, job, strategy, message):
        self.events.append(("fail", strategy))

    def job_finished(self, result):
        self.events.append(("finish", result.status))


def job(number: int) -> DownloadJob:
    return DownloadJob(parse_link(f"https://my.mts-link.ru/j/a/b/record-new/{number}"))


def test_fallback_tries_next_strategy_and_skips_unsupported(tmp_path):
    first = FakeStrategy("api", AccessDeniedError("нет доступа"))
    skipped = FakeStrategy("direct", RuntimeError("never"), supported=False)
    second = FakeStrategy("browser", None)
    outcome = FallbackDownloader([skipped, first, second]).download(
        job(1), DownloadSettings(output_dir=tmp_path)
    )
    assert outcome.strategy == "browser"
    assert skipped.calls == 0
    assert [(a.strategy, a.succeeded) for a in outcome.attempts] == [("api", False), ("browser", True)]


def test_fallback_survives_unexpected_exceptions(tmp_path):
    broken = FakeStrategy("api", KeyError("boom"))
    with pytest.raises(AllStrategiesFailedError) as error:
        FallbackDownloader([broken]).download(job(1), DownloadSettings(output_dir=tmp_path))
    assert "KeyError" in str(error.value)


def test_batch_continues_after_failure_writes_report_and_skips_on_rerun(tmp_path):
    good, bad = job(1), job(2)

    class Selective(FakeStrategy):
        def download(self, job_, settings):
            if job_ is bad:
                raise RecordUnavailableError("запись удалена")
            return super().download(job_, settings)

    strategy = Selective("api", None)
    reporter = SilentReporter()
    batch = DownloadBatch(
        FallbackDownloader([strategy]),
        JsonCompletedCatalog(tmp_path),
        reporter,
        JsonBatchReportWriter(tmp_path),
    )
    settings = DownloadSettings(output_dir=tmp_path)
    summary = batch.execute([good, bad], settings)
    assert [r.status for r in summary.results] == [JobStatus.DONE, JobStatus.FAILED]

    report = json.loads((tmp_path / "mtslink-report.json").read_text(encoding="utf-8"))
    assert (report["done"], report["failed"]) == (1, 1)
    failed_list = (tmp_path / "mtslink-failed.txt").read_text(encoding="utf-8")
    assert bad.link.url in failed_list and good.link.url not in failed_list

    calls_before = strategy.calls
    rerun = batch.execute([good], settings)
    assert rerun.results[0].status is JobStatus.SKIPPED
    assert strategy.calls == calls_before
    assert not (tmp_path / "mtslink-failed.txt").exists()


def test_batch_parallel_keeps_order(tmp_path):
    batch = DownloadBatch(
        FallbackDownloader([FakeStrategy("api", None)]),
        JsonCompletedCatalog(tmp_path),
        SilentReporter(),
        JsonBatchReportWriter(tmp_path),
    )
    jobs = [job(n) for n in range(1, 6)]
    summary = batch.execute(jobs, DownloadSettings(output_dir=tmp_path), parallel_jobs=3)
    assert [r.job for r in summary.results] == jobs
    assert all(r.status is JobStatus.DONE for r in summary.results)


class FakeHttp:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def get_json(self, url, headers):
        self.requests.append((url, headers))
        response = self.responses.get(url.split("?")[0])
        if isinstance(response, Exception):
            raise response
        if response is None:
            raise RecordUnavailableError("404")
        return response


def test_api_source_tries_every_endpoint_and_sends_session_cookie():
    from mtslink_downloader.infrastructure.storage.cookies import session_cookie

    link = parse_link("https://my.mts-link.ru/1/2/record-new/10/record-file/20")
    http = FakeHttp(
        {
            "https://my.mts-link.ru/api/event-sessions/10/record-files/20/flow": AccessDeniedError("403"),
            "https://gw.mts-link.ru/api/eventsessions/10/record": {"eventLogs": [], "name": "ok"},
        }
    )
    source = ApiRecordSource(http, (session_cookie("abc"),))  # type: ignore[arg-type]
    document = source.fetch(link)
    assert document.data["name"] == "ok"
    assert all(headers.get("Cookie") == "sessionId=abc" for _, headers in http.requests)


def test_api_source_reports_access_denied_with_hint():
    link = parse_link("https://my.mts-link.ru/j/a/b/record-new/10")
    http = FakeHttp({url: AccessDeniedError("403") for url in [
        "https://my.mts-link.ru/api/eventsessions/10/record",
        "https://gw.mts-link.ru/api/eventsessions/10/record",
    ]})
    with pytest.raises(AccessDeniedError, match="--session-id"):
        ApiRecordSource(http).fetch(link)  # type: ignore[arg-type]


def test_record_source_chain_prefers_access_denied_message():
    class Failing:
        def __init__(self, name, error):
            self.name, self.error = name, error

        def fetch(self, link):
            raise self.error

    chain = FallbackRecordSource(
        [Failing("api", AccessDeniedError("закрыто")), Failing("browser", RecordUnavailableError("x"))]
    )
    with pytest.raises(AccessDeniedError):
        chain.fetch(parse_link("https://my.mts-link.ru/j/a/b/record-new/1"))


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize(
    ("layer", "forbidden"),
    [
        ("domain", ("mtslink_downloader.application", "mtslink_downloader.infrastructure",
                    "mtslink_downloader.presentation", "subprocess", "playwright", "urllib.request")),
        ("application", ("mtslink_downloader.infrastructure", "mtslink_downloader.presentation",
                         "subprocess", "playwright", "urllib.request")),
    ],
)
def test_inner_layers_do_not_depend_on_outer_ones(layer, forbidden):
    for path in (PACKAGE_ROOT / layer).rglob("*.py"):
        assert not any(name.startswith(forbidden) for name in _imports(path)), path
