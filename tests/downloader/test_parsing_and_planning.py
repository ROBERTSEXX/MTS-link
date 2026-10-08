from mtslink_downloader.application.export_plan import ExportPlanner, MainKind
from mtslink_downloader.domain.models import (
    ExportMode,
    MediaAccess,
    PresentationStream,
    PresentationUpdate,
    RecordDocument,
    VideoStream,
)
from mtslink_downloader.domain.timeline import build_composite_timeline
from mtslink_downloader.infrastructure.parsing.structured import StructuredRecordParser


def parse(record):
    document = RecordDocument(data=record, source="test", access=MediaAccess(referer="https://x/"))
    return StructuredRecordParser().parse(document)


def test_main_segments_trim_initial_preroll_and_split_screen_share():
    recording = parse(
        {
            "name": "Лекция",
            "duration": 120.0,
            "eventLogs": [
                {
                    "relativeTime": 10.0,
                    "snapshot": {"data": {"mediasession": [{
                        "url": "https://storage/initial.mp4",
                        "hlsUrl": "https://delivery/initial/playlist.m3u8",
                        "stream": {"conference": {"id": 1}},
                    }]}},
                },
                {"module": "mediasession.add", "relativeTime": 10.0, "data": {
                    "url": "https://storage/main.mp4", "stream": {"conference": {"id": 1}}}},
                {"module": "mediasession.add", "relativeTime": 100.0, "data": {
                    "url": "https://storage/screen.mp4", "stream": {"screensharing": {"id": 2}}}},
                {"module": "mediasession.add", "relativeTime": 100.0, "data": {
                    "url": "https://storage/final.mp4", "stream": {"conference": {"id": 3}}}},
            ],
        }
    )
    assert recording.title == "Лекция"
    speaker = recording.speaker
    assert speaker is not None
    assert [s.source_url for s in speaker.segments] == [
        "https://storage/initial.mp4",
        "https://storage/main.mp4",
        "https://storage/final.mp4",
    ]
    assert speaker.segments[0].trim_duration == 10.0
    assert speaker.segments[0].hls_url == "https://delivery/initial/playlist.m3u8"
    assert recording.screen is not None
    assert recording.screen.start_time == 100.0


def test_later_t0_snapshot_replaces_short_preroll():
    recording = parse(
        {
            "duration": 120.0,
            "eventLogs": [
                {"relativeTime": 0.0, "snapshot": {"data": {"mediasession": [
                    {"url": "https://storage/preroll.mp4", "stream": {"conference": {"id": 1}}}]}}},
                {"relativeTime": 0.0, "snapshot": {"data": {"mediasession": [
                    {"url": "https://storage/full-start.mp4", "stream": {"conference": {"id": 1}}}]}}},
                {"module": "mediasession.add", "relativeTime": 100.0, "data": {
                    "url": "https://storage/next.mp4", "stream": {"conference": {"id": 1}}}},
            ],
        }
    )
    segments = recording.video_streams[0].segments
    assert [s.source_url for s in segments] == [
        "https://storage/full-start.mp4",
        "https://storage/next.mp4",
    ]
    assert segments[0].trim_duration == 100.0


def test_snapshot_only_record_covers_whole_duration_without_overlaps():
    def snapshot(time, speaker, screen):
        return {"relativeTime": time, "snapshot": {"data": {"mediasession": [
            {"url": speaker, "stream": {"conference": {"id": 10}}},
            {"url": screen, "stream": {"screensharing": {"id": 20}}},
        ]}}}

    recording = parse(
        {
            "duration": 100.0,
            "eventLogs": [
                snapshot(0.0, "https://s/short-start.mp4", "https://s/screen-1.mp4"),
                snapshot(0.0, "https://s/speaker-1.mp4", "https://s/screen-1.mp4"),
                snapshot(40.0, "https://s/speaker-2.mp4", "https://s/screen-2.mp4"),
            ],
        }
    )
    speaker, screen = recording.speaker, recording.screen
    assert speaker is not None and screen is not None
    assert [(s.source_url, s.relative_time, s.max_duration) for s in speaker.segments] == [
        ("https://s/speaker-1.mp4", 0.0, 40.0),
        ("https://s/speaker-2.mp4", 40.0, 60.0),
    ]
    assert [(s.relative_time, s.max_duration) for s in screen.segments] == [(0.0, 40.0), (40.0, 60.0)]


def test_audio_only_participant_and_presentation_are_separate_sources():
    recording = parse(
        {
            "duration": 120.0,
            "eventLogs": [
                {"relativeTime": 1.0, "snapshot": {"data": {"mediasession": [
                    {"url": "https://s/speaker.mp4", "stream": {"conference": {"id": 10}}}]}}},
                {"module": "conference.update", "relativeTime": 50.0, "data": {
                    "id": 20, "hasVideo": False, "hasAudio": True, "user": {"nickname": "Слушатель"}}},
                {"module": "mediasession.add", "relativeTime": 5.0, "data": {
                    "url": "https://s/speaker-next.mp4", "stream": {"conference": {"id": 10}}}},
                {"module": "mediasession.add", "relativeTime": 50.0, "data": {
                    "url": "https://s/question.mp4", "stream": {"conference": {"id": 20}}}},
                {"module": "presentation.update", "relativeTime": 5.0, "data": {
                    "isActive": True,
                    "fileReference": {
                        "file": {"id": 7, "name": "deck.pdf", "downloadUrl": "https://s/deck.pdf",
                                 "slides": [{"name": "1"}, {"name": "2"}]},
                        "slide": {"name": "1", "url": "https://s/1.jpg"},
                    }}},
                {"module": "presentation.update", "relativeTime": 45.0, "data": {
                    "isActive": False,
                    "fileReference": {"file": {"id": 7, "downloadUrl": "https://s/deck.pdf"},
                                      "slide": {"name": "2"}}}},
            ],
        }
    )
    assert len(recording.audio_streams) == 1
    audio = recording.audio_streams[0]
    assert audio.title == "Аудио участника: Слушатель"
    assert audio.start_time == 50.0
    assert audio.key == "audio-20"
    presentation = recording.presentations[0]
    assert (presentation.start_time, presentation.duration, presentation.slide_count) == (5.0, 40.0, 2)
    assert presentation.updates[0].image_url == "https://s/1.jpg"


def test_composite_timeline_priorities():
    presentation = PresentationStream(
        key="presentation", title="", file_name="deck.pdf", source_url="https://s/deck.pdf",
        start_time=3.0, duration=5.0, slide_count=1,
        updates=[
            PresentationUpdate(relative_time=3.0, is_active=True, image_url="https://s/slide.jpg"),
            PresentationUpdate(relative_time=8.0, is_active=False),
        ],
    )
    screen = VideoStream(key="screen-share", title="", segments=[], duration=5.0, start_time=10.0)
    timeline = build_composite_timeline(20.0, [presentation], screen)
    assert [(t.kind, t.start_time, t.duration) for t in timeline] == [
        ("speaker", 0.0, 3.0),
        ("presentation", 3.0, 5.0),
        ("speaker", 8.0, 2.0),
        ("screen", 10.0, 5.0),
        ("speaker", 15.0, 5.0),
    ]


def _recording_with(materials: bool, audio: bool):
    events = [
        {"relativeTime": 0.0, "snapshot": {"data": {"mediasession": [
            {"url": "https://s/speaker.mp4", "stream": {"conference": {"id": 1}}}]}}},
    ]
    if materials:
        events.append({"module": "mediasession.add", "relativeTime": 5.0, "data": {
            "url": "https://s/screen.mp4", "stream": {"screensharing": {"id": 2}}}})
    if audio:
        events.append({"module": "conference.update", "relativeTime": 1.0,
                       "data": {"id": 3, "hasAudio": True, "hasVideo": False}})
        events.append({"module": "mediasession.add", "relativeTime": 7.0, "data": {
            "url": "https://s/q.mp4", "stream": {"conference": {"id": 3}}}})
    return parse({"duration": 10.0, "eventLogs": events})


def test_planner_modes():
    planner = ExportPlanner()
    plain = _recording_with(materials=False, audio=False)
    assert planner.plan(plain, ExportMode.AUTO).main_kind is MainKind.VIDEO

    with_materials = _recording_with(materials=True, audio=True)
    auto = planner.plan(with_materials, ExportMode.AUTO)
    assert auto.main_kind is MainKind.COMPOSITE
    assert len(auto.mixed_audio) == 1
    assert not auto.separate_videos

    speaker_only = planner.plan(with_materials, ExportMode.SPEAKER)
    assert speaker_only.main_kind is MainKind.VIDEO
    assert speaker_only.main_video is with_materials.speaker

    everything = planner.plan(with_materials, ExportMode.ALL)
    assert everything.main_kind is MainKind.COMPOSITE
    assert {s.key for s in everything.separate_videos} == {"speaker", "screen-share"}
    assert len(everything.separate_audios) == 1

    all_plain = planner.plan(plain, ExportMode.ALL)
    assert all_plain.main_kind is MainKind.VIDEO
    assert all_plain.separate_videos == ()
