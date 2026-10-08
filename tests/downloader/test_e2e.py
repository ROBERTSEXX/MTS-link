"""Сквозные проверки всех способов на локальной копии МТС Линк."""

from __future__ import annotations

from pathlib import Path

import pytest

from mtslink_downloader.bootstrap import AppConfig, Container
from mtslink_downloader.domain.links import parse_link
from mtslink_downloader.domain.models import (
    DownloadJob,
    DownloadSettings,
    ExportMode,
    JobStatus,
)
from mtslink_downloader.presentation.cli import main

from .conftest import (
    HAS_FFMPEG,
    chromium_path,
    mean_luma,
    mean_volume,
    playwright_available,
    probe,
    publish_seminar,
    publish_webinar,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not HAS_FFMPEG, reason="нужны ffmpeg и ffprobe"),
]

TITLE = "Тестовый вебинар"


class Reporter:
    def __init__(self):
        self.failures = []

    def job_started(self, job, index, total):
        pass

    def strategy_started(self, job, strategy):
        pass

    def strategy_failed(self, job, strategy, message):
        self.failures.append((strategy, message))

    def job_finished(self, result):
        pass


def run(
    tmp_path: Path,
    urls: list[str],
    strategies: tuple[str, ...],
    mode: ExportMode = ExportMode.AUTO,
    **config,
):
    """Запуск пакета без внешних API-хостов: сеть ограничена локальным сервером."""

    out = tmp_path / "out"
    container = Container(
        AppConfig(
            output_dir=out,
            strategies=strategies,
            api_hosts=(),
            profile_dir=None,
            browser_path=chromium_path(),
            browser_wait_seconds=6.0,
            **config,
        )
    )
    reporter = Reporter()
    app = container.build(reporter)
    jobs = [DownloadJob(parse_link(url)) for url in urls]
    summary = app.batch.execute(jobs, DownloadSettings(output_dir=out, mode=mode))
    return summary, reporter, out


def test_auto_mode_builds_composite_video(tmp_path, fake_mts, media):
    url = publish_webinar(fake_mts, media)
    summary, reporter, out = run(tmp_path, [url], ("api",))
    result = summary.results[0]
    assert result.status is JobStatus.DONE, (result.error, reporter.failures)
    assert result.strategy == "api"
    main_file = out / f"{TITLE} [1001].mp4"
    assert result.outputs == (main_file,)
    info = probe(main_file)
    assert info["types"] == {"video", "audio"}
    assert (info["width"], info["height"]) == (1280, 720)
    assert info["duration"] == pytest.approx(20.0, abs=0.7)
    # Рабочий кэш удаляется после успеха.
    assert not (out / ".mtslink-work").exists()
    # Медиа запрашивалось с Referer страницы, как это делает плеер.
    media_requests = [h for p, h in fake_mts.requests if p.startswith("/storage/")]
    assert media_requests and all(h.get("Referer") == url for h in media_requests)


def test_speaker_mode_keeps_camera_and_mixes_participant(tmp_path, fake_mts, media):
    url = publish_webinar(fake_mts, media)
    summary, reporter, out = run(tmp_path, [url], ("api",), ExportMode.SPEAKER)
    assert summary.results[0].status is JobStatus.DONE, reporter.failures
    info = probe(out / f"{TITLE} [1001].mp4")
    assert (info["width"], info["height"]) == (640, 360)
    assert info["types"] == {"video", "audio"}
    assert info["duration"] == pytest.approx(20.0, abs=0.7)


def test_all_mode_saves_every_source(tmp_path, fake_mts, media):
    url = publish_webinar(fake_mts, media)
    summary, reporter, out = run(tmp_path, [url], ("api",), ExportMode.ALL)
    result = summary.results[0]
    assert result.status is JobStatus.DONE, reporter.failures
    names = sorted(path.name for path in result.outputs)
    stem = f"{TITLE} [1001]"
    assert names == sorted(
        [
            f"{stem}-speaker.mp4",
            f"{stem}-screen-share.mp4",
            f"{stem}-audio-2.m4a",
            f"{stem}-presentation.pdf",
            f"{stem}.mp4",
        ]
    )
    assert probe(out / f"{stem}-screen-share.mp4")["duration"] == pytest.approx(5.0, abs=0.5)
    assert probe(out / f"{stem}-audio-2.m4a")["types"] == {"audio"}
    assert (out / f"{stem}-presentation.pdf").read_bytes().startswith(b"%PDF")


def test_seminar_with_simultaneous_mics_is_placed_by_real_time(tmp_path, fake_mts, media):
    url = publish_seminar(fake_mts, media)
    summary, reporter, out = run(tmp_path, [url], ("api",))
    assert summary.results[0].status is JobStatus.DONE, reporter.failures
    video = out / "Семинар [7007].mp4"
    info = probe(video)
    assert info["types"] == {"video", "audio"}
    assert info["duration"] == pytest.approx(20.0, abs=0.5)
    # Камера лектора только 7–17 с, остальное время чёрный кадр.
    assert mean_luma(video, 3.0) < 20
    assert mean_luma(video, 12.0) > 60
    assert mean_luma(video, 18.5) < 20
    # Звук: микрофон лектора 0–7 с, участник 3–18 с поверх камеры, затем тишина.
    assert mean_volume(video, 0.5, 2.0) > -40
    assert mean_volume(video, 12.0, 4.0) > -40
    assert mean_volume(video, 18.6, 1.2) < -60


def test_private_record_needs_session_id(tmp_path, fake_mts, media):
    url = publish_webinar(fake_mts, media, session_id="2002", private_cookie="sessionId=s3cret")

    denied, reporter, _ = run(tmp_path / "a", [url], ("api",))
    assert denied.results[0].status is JobStatus.FAILED
    assert "session-id" in reporter.failures[0][1]

    allowed, reporter, out = run(tmp_path / "b", [url], ("api",), ExportMode.SPEAKER,
                                 session_id="s3cret")
    assert allowed.results[0].status is JobStatus.DONE, reporter.failures
    assert (out / f"{TITLE} [2002].mp4").exists()


def test_flat_parser_handles_records_without_mediasession(tmp_path, fake_mts, media):
    flat1 = fake_mts.add_file("/files/flat1.mp4", media["flat1"], "video/mp4")
    flat2 = fake_mts.add_file("/files/flat2.mp4", media["flat2"], "video/mp4")
    fake_mts.add_json(
        "/api/event-sessions/3003/record-files/77/flow",
        {"name": "Старый формат", "duration": 10.0, "eventLogs": [
            {"relativeTime": 0.0, "data": {"url": flat1}},
            {"relativeTime": 5.0, "data": {"url": flat2}},
            {"relativeTime": 6.0, "data": {"url": fake_mts.base + "/files/notes.pdf"}},
        ]},
    )
    url = f"{fake_mts.base}/12/34/record-new/3003/record-file/77"
    summary, reporter, out = run(tmp_path, [url], ("api",))
    assert summary.results[0].status is JobStatus.DONE, reporter.failures
    info = probe(out / "Старый формат [3003-77].mp4")
    assert info["duration"] == pytest.approx(10.0, abs=0.6)
    assert info["types"] == {"video", "audio"}


def test_direct_and_ytdlp_strategies(tmp_path, fake_mts, media):
    direct_url = fake_mts.add_file("/cdn/full.mp4", media["full"], "video/mp4")
    summary, reporter, out = run(tmp_path / "d", [direct_url], ("direct",))
    assert summary.results[0].status is JobStatus.DONE, reporter.failures
    assert probe(summary.results[0].outputs[0])["duration"] == pytest.approx(6.0, abs=0.5)

    pytest.importorskip("yt_dlp")
    summary, reporter, _ = run(tmp_path / "y", [direct_url], ("ytdlp",))
    assert summary.results[0].status is JobStatus.DONE, reporter.failures
    assert summary.results[0].strategy == "ytdlp"


def test_fallback_reaches_next_strategy_and_batch_continues(tmp_path, fake_mts, media):
    good = publish_webinar(fake_mts, media, session_id="4004")
    missing = f"{fake_mts.base}/j/org/event/record-new/9999"
    direct_url = fake_mts.add_file("/cdn/full.mp4", media["full"], "video/mp4")
    summary, reporter, _ = run(
        tmp_path, [missing, good, direct_url], ("api", "direct"), ExportMode.SPEAKER
    )
    statuses = [result.status for result in summary.results]
    assert statuses == [JobStatus.FAILED, JobStatus.DONE, JobStatus.DONE]
    assert summary.results[2].strategy == "direct"


def test_cli_batch_from_list_file_and_rerun_skips(tmp_path, fake_mts, media, capsys):
    url = publish_webinar(fake_mts, media, session_id="5005")
    links = tmp_path / "links.txt"
    links.write_text(f"# список\n{url}  Моя лекция\n", encoding="utf-8")
    out = tmp_path / "cli-out"
    args = ["-i", str(links), "-o", str(out), "-s", "api", "-m", "speaker", "--no-profile"]

    assert main(args) == 0
    assert (out / "Моя лекция.mp4").exists()
    assert "скачано 1" in capsys.readouterr().out

    assert main(args) == 0
    assert "пропущено 1" in capsys.readouterr().out

    assert main([url, "--dry-run", "-s", "api", "--no-profile"]) == 0
    printed = capsys.readouterr().out
    assert "Будет создано: сводное видео" in printed
    assert "Аудио участника: Слушатель" in printed


def test_cli_without_links_is_usage_error(capsys):
    assert main([]) == 2


browser_only = pytest.mark.skipif(
    not (playwright_available() and chromium_path()), reason="нужны Playwright и Chromium"
)


@browser_only
@pytest.mark.browser
def test_browser_strategy_uses_logged_in_cookies(tmp_path, fake_mts, media):
    url = publish_webinar(fake_mts, media, session_id="6006", private_cookie="sessionId=browser")
    # Страница «залогиненного» пользователя: cookie есть в браузере, плеер
    # сам запрашивает журнал записи.
    fake_mts.add_html(
        "/j/org/event/record-new/6006",
        """<!doctype html><title>Запись</title><script>
        document.cookie = 'sessionId=browser; path=/';
        fetch('/api/eventsessions/6006/record?withoutCuts=false');
        </script>""",
    )
    summary, reporter, out = run(tmp_path, [url], ("api", "browser"), ExportMode.SPEAKER)
    result = summary.results[0]
    assert result.status is JobStatus.DONE, reporter.failures
    assert result.strategy == "browser"
    assert [strategy for strategy, _ in reporter.failures] == ["api"]
    assert probe(out / f"{TITLE} [6006].mp4")["duration"] == pytest.approx(20.0, abs=0.7)


@browser_only
@pytest.mark.browser
def test_sniff_strategy_captures_player_stream(tmp_path, fake_mts, media):
    fake_mts.add_file("/media/lesson.mp4", media["full"], "video/mp4")
    page = fake_mts.add_html(
        "/lesson/42",
        "<!doctype html><title>Урок 42</title>"
        "<video src='/media/lesson.mp4' muted autoplay></video>",
    )
    summary, reporter, out = run(tmp_path, [page], ("sniff",))
    result = summary.results[0]
    assert result.status is JobStatus.DONE, reporter.failures
    # Идентификатор «42» уже есть в названии страницы и повторно не добавляется.
    assert result.outputs[0].name == "Урок 42.mp4"
    assert probe(result.outputs[0])["duration"] == pytest.approx(6.0, abs=0.5)
