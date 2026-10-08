from pathlib import Path

import pytest

from mtslink_downloader.domain.errors import InvalidLinkError
from mtslink_downloader.domain.links import LinkKind, parse_link
from mtslink_downloader.domain.models import MediaAccess, SessionCookie
from mtslink_downloader.infrastructure.sources.endpoints import record_endpoints
from mtslink_downloader.infrastructure.storage.link_list import LinkListReader, parse_lines


@pytest.mark.parametrize(
    ("url", "session", "record"),
    [
        ("https://my.mts-link.ru/j/Flant/iot_1007/record-new/19856070522", "19856070522", None),
        ("https://my.mts-link.ru/12345678/987654321/record-new/123456789", "123456789", None),
        (
            "https://my.mts-link.ru/12345678/987654321/record-new/123456789/record-file/1234567890",
            "123456789",
            "1234567890",
        ),
        ("https://events.webinar.ru/1/2/record-new/333/record-file/444/", "333", "444"),
        ("https://webinar.company.ru/j/x/y/record-new/555?utm=1", "555", None),
    ],
)
def test_recording_links(url, session, record):
    link = parse_link(url)
    assert link.kind is LinkKind.RECORDING
    assert link.event_session_id == session
    assert link.record_file_id == record


def test_direct_and_page_links():
    assert parse_link("https://cdn.example/v/file.MP4?sig=1").kind is LinkKind.DIRECT_MEDIA
    assert parse_link("https://cdn.example/v/master.m3u8").kind is LinkKind.DIRECT_MEDIA
    assert parse_link("https://example.com/lesson/1").kind is LinkKind.PAGE


def test_invalid_link():
    with pytest.raises(InvalidLinkError):
        parse_link("my.mts-link.ru/j/a/b/record-new/1")


def test_record_endpoints_cover_all_known_apis():
    link = parse_link("https://my.mts-link.ru/1/2/record-new/10/record-file/20")
    endpoints = record_endpoints(link)
    assert endpoints[0] == (
        "https://my.mts-link.ru/api/event-sessions/10/record-files/20/flow?withoutCuts=false"
    )
    assert "https://gw.mts-link.ru/api/eventsessions/10/record?withoutCuts=false" in endpoints
    assert "https://my.mts-link.ru/api/eventsessions/10/record?withoutCuts=false" in endpoints
    assert len(endpoints) == len(set(endpoints))


def test_parse_lines_supports_names_comments_and_duplicates():
    jobs, warnings = parse_lines(
        [
            "﻿# курс",
            "",
            "https://my.mts-link.ru/j/a/b/record-new/1   Лекция 1",
            "Лекция 2: https://my.mts-link.ru/j/a/b/record-new/2",
            "https://my.mts-link.ru/j/a/b/record-new/3 | Лекция 3",
            "https://my.mts-link.ru/j/a/b/record-new/1",
            "две ссылки https://x.ru/a.mp4, https://x.ru/b.mp4",
            "просто текст без ссылки",
            "// комментарий",
        ]
    )
    assert [(job.link.url, job.name) for job in jobs] == [
        ("https://my.mts-link.ru/j/a/b/record-new/1", "Лекция 1"),
        ("https://my.mts-link.ru/j/a/b/record-new/2", "Лекция 2"),
        ("https://my.mts-link.ru/j/a/b/record-new/3", "Лекция 3"),
        ("https://x.ru/a.mp4", None),
        ("https://x.ru/b.mp4", None),
    ]
    assert len(warnings) == 1


def test_reader_combines_args_and_files(tmp_path: Path):
    list_file = tmp_path / "links.txt"
    list_file.write_text("https://my.mts-link.ru/j/a/b/record-new/2\n", encoding="cp1251")
    jobs, _ = LinkListReader().read(["https://my.mts-link.ru/j/a/b/record-new/1"], [list_file])
    assert [job.link.event_session_id for job in jobs] == ["1", "2"]


def test_cookies_are_sent_only_to_matching_hosts():
    access = MediaAccess(
        referer="https://my.mts-link.ru/j/a/b/record-new/1",
        cookies=(SessionCookie("sessionId", "secret", ".mts-link.ru"),),
    )
    assert access.headers_for("https://gw.mts-link.ru/api/x")["Cookie"] == "sessionId=secret"
    other = access.headers_for("https://events-storage.webinar.ru/file.mp4")
    assert "Cookie" not in other
    assert other["Referer"] == "https://my.mts-link.ru/j/a/b/record-new/1"
    assert other["Origin"] == "https://my.mts-link.ru"
