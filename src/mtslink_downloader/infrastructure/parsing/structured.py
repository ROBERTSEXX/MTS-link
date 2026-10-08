"""Подробный разбор журнала МТС Линк: камера, экран, участники, презентации.

Логика перенесена из ``download_mts_link.py`` и разделена на шаги:
сбор сведений о конференциях → выбор стартовых и добавленных сегментов →
группировка в потоки → разбор презентаций.
"""

from __future__ import annotations

from typing import Any

from mtslink_downloader.domain.errors import RecordUnavailableError
from mtslink_downloader.domain.models import (
    SCREEN_KEY,
    SPEAKER_KEY,
    AudioStream,
    MediaSegment,
    PresentationStream,
    PresentationUpdate,
    RecordDocument,
    Recording,
    VideoStream,
)
from mtslink_downloader.domain.placement import EpochTimeline, attach_epoch_pieces


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _http_url(value: Any) -> str | None:
    return value if isinstance(value, str) and value.startswith(("http://", "https://")) else None


def stream_kind(value: Any) -> str | None:
    """``speaker`` для conference, ``screen-share`` для демонстрации экрана."""

    if not isinstance(value, dict):
        return None
    stream = value.get("stream") or {}
    if not isinstance(stream, dict):
        return None
    if stream.get("screensharing"):
        return SCREEN_KEY
    if stream.get("conference"):
        return SPEAKER_KEY
    return None


def media_group_key(value: Any) -> str | None:
    """Ключ физической media-session: экран, ``conference:<id>`` или аудио."""

    if not isinstance(value, dict):
        return None
    stream = value.get("stream") or {}
    if not isinstance(stream, dict):
        return None
    if stream.get("screensharing"):
        return SCREEN_KEY
    conference = stream.get("conference")
    if isinstance(conference, dict):
        identifier = conference.get("id") or conference.get("publicKey")
        return f"conference:{identifier}" if identifier is not None else "conference:unknown"
    for media_name in ("audio", "microphone"):
        media = stream.get(media_name)
        if isinstance(media, dict):
            identifier = media.get("id") or media.get("publicKey")
            return f"audio:{identifier or media_name}"
    return None


def media_segment(value: Any, relative_time: float, initial: bool) -> MediaSegment | None:
    if not media_group_key(value):
        return None
    source_url = _http_url(value.get("url")) or ""
    hls_url = _http_url(value.get("hlsUrl"))
    if not source_url and not hls_url:
        return None
    media_id = value.get("id")
    return MediaSegment(
        source_url=source_url,
        hls_url=hls_url,
        relative_time=max(0.0, float(relative_time)),
        initial=initial,
        media_id=str(media_id) if media_id is not None else None,
        start_epoch=_epoch(value.get("time")),
    )


def _epoch(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 1_000_000_000 else None


class StructuredRecordParser:
    """Разбирает ``mediasession``/``conference``/``presentation`` события."""

    def parse(self, document: RecordDocument) -> Recording:
        record = document.data
        event_logs = record.get("eventLogs") or []
        if not isinstance(event_logs, list):
            raise RecordUnavailableError("Журнал записи имеет неожиданный формат.")
        total_duration = _float(record.get("duration"))
        title = str(record.get("name") or "").strip()

        video_streams, audio_streams = self._media_streams(event_logs, total_duration)
        self._attach_timing(record, event_logs, [*video_streams, *audio_streams])
        presentations = self._presentations(event_logs, total_duration)
        return Recording(
            title=title,
            duration=total_duration,
            video_streams=video_streams,
            audio_streams=audio_streams,
            presentations=presentations,
        )

    # ------------------------------------------------------------------
    # Точное время файлов
    # ------------------------------------------------------------------

    @staticmethod
    def _attach_timing(
        record: dict[str, Any], event_logs: list[Any], streams: list[VideoStream | AudioStream]
    ) -> None:
        """Разложить файлы по шкале записи по их реальному времени.

        ``mediasession.update`` сообщает время начала и длительность файла,
        ``eventsession.start`` — начало мероприятия, ``cuts`` — вырезанные
        паузы. Вместе они дают точное положение каждого файла даже тогда,
        когда файлы разных участников идут одновременно.
        """

        start_epoch = next(
            (
                _epoch(event.get("time"))
                for event in event_logs
                if isinstance(event, dict) and event.get("module") == "eventsession.start"
            ),
            None,
        )
        if start_epoch is None:
            return
        cuts = [
            (float(cut["start"]), float(cut["end"]))
            for cut in record.get("cuts") or []
            if isinstance(cut, dict) and _epoch(cut.get("start")) and _epoch(cut.get("end"))
        ]
        timeline = EpochTimeline.build(start_epoch, cuts)

        updates: dict[str, dict[str, Any]] = {}
        for event in event_logs:
            if not isinstance(event, dict) or event.get("module") != "mediasession.update":
                continue
            data = event.get("data")
            if isinstance(data, dict) and data.get("id") is not None:
                updates[str(data["id"])] = data

        for stream in streams:
            for segment in stream.segments:
                update = updates.get(segment.media_id or "")
                if update is None:
                    continue
                duration = update.get("duration")
                if isinstance(duration, (int, float)) and duration > 0:
                    segment.known_duration = float(duration)
                if segment.start_epoch is None:
                    segment.start_epoch = _epoch(update.get("time"))
                attach_epoch_pieces(segment, timeline)

    # ------------------------------------------------------------------
    # Конференции
    # ------------------------------------------------------------------

    @staticmethod
    def _conference_info(event_logs: list[Any]) -> dict[str, dict[str, Any]]:
        """Флаги hasVideo/hasAudio объединяются по всем состояниям конференции."""

        result: dict[str, dict[str, Any]] = {}

        def add(value: Any) -> None:
            if not isinstance(value, dict) or value.get("id") is None:
                return
            info = result.setdefault(
                str(value["id"]),
                {"has_video": False, "has_audio": False, "participant": None, "user_id": None},
            )
            info["has_video"] = bool(info["has_video"] or value.get("hasVideo"))
            info["has_audio"] = bool(info["has_audio"] or value.get("hasAudio"))
            if value.get("userId") is not None:
                info["user_id"] = str(value["userId"])
            user = value.get("user")
            if isinstance(user, dict):
                participant = user.get("nickname") or user.get("name")
                if participant:
                    info["participant"] = str(participant)
                if user.get("id") is not None:
                    info["user_id"] = str(user["id"])

        for event in event_logs:
            if not isinstance(event, dict):
                continue
            snapshot = event.get("snapshot") or {}
            data = snapshot.get("data") if isinstance(snapshot, dict) else None
            if isinstance(data, dict):
                for conference in data.get("conference") or []:
                    add(conference)
            if event.get("module") in {"conference.add", "conference.update", "conference.delete"}:
                add(event.get("data"))
        return result

    # ------------------------------------------------------------------
    # Сегменты
    # ------------------------------------------------------------------

    @staticmethod
    def _snapshot_sessions(event: Any) -> list[Any]:
        if not isinstance(event, dict):
            return []
        snapshot = event.get("snapshot") or {}
        data = snapshot.get("data") if isinstance(snapshot, dict) else None
        sessions = data.get("mediasession") if isinstance(data, dict) else None
        return sessions if isinstance(sessions, list) else []

    def _initial_from_snapshots(self, event_logs: list[Any]) -> dict[str, MediaSegment]:
        """Обычный журнал: самый ранний snapshot каждой группы.

        При одинаковом времени последний URL заменяет короткий преролл.
        """

        initial: dict[str, MediaSegment] = {}
        times: dict[str, float] = {}
        for event in event_logs:
            sessions = self._snapshot_sessions(event)
            if not sessions:
                continue
            snapshot_time = _float(event.get("relativeTime"))
            for session in sessions:
                candidate = media_segment(session, 0.0, initial=True)
                group = media_group_key(session)
                if not candidate or not group:
                    continue
                current = initial.get(group)
                current_source = current.any_url if current else None
                current_time = times.get(group)
                is_earlier = current_time is None or snapshot_time < current_time - 0.01
                is_replacement = (
                    current_time is not None
                    and abs(snapshot_time - current_time) <= 0.01
                    and current_source != candidate.any_url
                )
                if is_earlier or is_replacement:
                    initial[group] = candidate
                    times[group] = snapshot_time
        return initial

    def _snapshot_only(
        self, event_logs: list[Any], total_duration: float
    ) -> tuple[dict[str, MediaSegment], dict[str, list[MediaSegment]]]:
        """Журнал без ``mediasession.add``: каждый snapshot накопительный.

        Новый URL начинает новый физический интервал; предыдущий файл
        ограничивается началом следующего, последний — концом записи.
        """

        candidates_by_group: dict[str, list[MediaSegment]] = {}
        for event in event_logs:
            sessions = self._snapshot_sessions(event)
            if not sessions:
                continue
            relative_time = _float(event.get("relativeTime"))
            for session in sessions:
                group = media_group_key(session)
                candidate = media_segment(
                    session, relative_time, initial=not candidates_by_group.get(group or "")
                )
                if not candidate or not group:
                    continue
                candidates = candidates_by_group.setdefault(group, [])
                if any(item.any_url == candidate.any_url for item in candidates):
                    continue
                same_time = next(
                    (
                        index
                        for index, item in enumerate(candidates)
                        if abs(item.relative_time - candidate.relative_time) < 0.01
                    ),
                    None,
                )
                if same_time is None:
                    candidates.append(candidate)
                else:
                    candidate.initial = candidates[same_time].initial
                    candidates[same_time] = candidate

        initial: dict[str, MediaSegment] = {}
        additions: dict[str, list[MediaSegment]] = {}
        for group, candidates in candidates_by_group.items():
            candidates.sort(key=lambda item: item.relative_time)
            if not candidates:
                continue
            for index, segment in enumerate(candidates):
                next_time = (
                    candidates[index + 1].relative_time
                    if index + 1 < len(candidates)
                    else total_duration
                )
                if next_time > segment.relative_time:
                    segment.max_duration = next_time - segment.relative_time
            initial[group] = candidates[0]
            additions[group] = candidates[1:]
        return initial, additions

    def _media_streams(
        self, event_logs: list[Any], total_duration: float
    ) -> tuple[list[VideoStream], list[AudioStream]]:
        conference_info = self._conference_info(event_logs)
        has_additions = any(
            isinstance(event, dict) and event.get("module") == "mediasession.add"
            for event in event_logs
        )
        if has_additions:
            initial_by_group = self._initial_from_snapshots(event_logs)
            additions_by_group: dict[str, list[MediaSegment]] = {}
        else:
            initial_by_group, additions_by_group = self._snapshot_only(event_logs, total_duration)

        for event in event_logs:
            if not isinstance(event, dict) or event.get("module") != "mediasession.add":
                continue
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            try:
                relative_time = float(event.get("relativeTime", 0.0))
            except (TypeError, ValueError):
                continue
            candidate = media_segment(data, relative_time, initial=False)
            group = media_group_key(data)
            if candidate and group:
                additions_by_group.setdefault(group, []).append(candidate)

        return self._group_streams(
            initial_by_group, additions_by_group, conference_info, has_additions, total_duration
        )

    def _group_streams(
        self,
        initial_by_group: dict[str, MediaSegment],
        additions_by_group: dict[str, list[MediaSegment]],
        conference_info: dict[str, dict[str, Any]],
        has_additions: bool,
        total_duration: float,
    ) -> tuple[list[VideoStream], list[AudioStream]]:
        all_groups = sorted(set(initial_by_group) | set(additions_by_group))

        def first_time(group: str) -> float:
            first = initial_by_group.get(group)
            if first is None and additions_by_group.get(group):
                first = additions_by_group[group][0]
            return first.relative_time if first else float("inf")

        # Первый conference с видео — камера лектора; его переподключения
        # объединяются по user_id, видео других участников идут отдельно.
        video_groups = [
            group
            for group in all_groups
            if group.startswith("conference:")
            and conference_info.get(group.split(":", 1)[1], {}).get("has_video")
        ]
        primary = min(video_groups, key=first_time) if video_groups else None
        primary_user = (
            conference_info.get(primary.split(":", 1)[1], {}).get("user_id") if primary else None
        )

        speaker_segments: list[MediaSegment] = []
        screen_stream: VideoStream | None = None
        audio_streams: list[AudioStream] = []
        participant_videos: list[VideoStream] = []

        for group in all_groups:
            segments = self._ordered_segments(
                initial_by_group.get(group), additions_by_group.get(group, []), has_additions
            )
            if not segments:
                continue

            if group == SCREEN_KEY:
                screen_stream = VideoStream(
                    key=SCREEN_KEY,
                    title="Демонстрация экрана",
                    segments=segments,
                    duration=0.0,
                    start_time=segments[0].relative_time,
                )
                continue

            if not group.startswith("conference:"):
                number = len(audio_streams) + 1
                audio_streams.append(
                    AudioStream(
                        key=f"audio-{number}",
                        title=f"Аудиопоток {number}",
                        segments=segments,
                        duration=0.0,
                        start_time=segments[0].relative_time,
                    )
                )
                continue

            conference_id = group.split(":", 1)[1]
            info = conference_info.get(conference_id)
            # Без conference.update (старые журналы) источник считается камерой.
            if info and info.get("has_audio") and not info.get("has_video"):
                participant = info.get("participant")
                audio_streams.append(
                    AudioStream(
                        key=f"audio-{conference_id}",
                        title=f"Аудио участника: {participant}" if participant else "Аудио участника",
                        segments=segments,
                        duration=0.0,
                        start_time=segments[0].relative_time,
                        participant=participant,
                    )
                )
            elif (
                info
                and info.get("has_video")
                and primary_user
                and info.get("user_id")
                and info.get("user_id") != primary_user
            ):
                participant = info.get("participant")
                participant_videos.append(
                    VideoStream(
                        key=f"participant-video-{conference_id}",
                        title=f"Видео участника: {participant}" if participant else "Видео участника",
                        segments=segments,
                        duration=0.0,
                        start_time=segments[0].relative_time,
                        has_audio=bool(info.get("has_audio")),
                    )
                )
            else:
                speaker_segments.extend(segments)

        speaker_segments.sort(key=lambda item: item.relative_time)
        streams: list[VideoStream] = []
        if speaker_segments:
            streams.append(
                VideoStream(
                    key=SPEAKER_KEY,
                    title="Спикер / камера",
                    segments=speaker_segments,
                    duration=total_duration,
                    start_time=0.0,
                    has_audio=True,
                )
            )
        streams.extend(participant_videos)
        if screen_stream:
            streams.append(screen_stream)
        return streams, audio_streams

    @staticmethod
    def _ordered_segments(
        initial: MediaSegment | None, additions: list[MediaSegment], has_additions: bool
    ) -> list[MediaSegment]:
        ordered_additions = sorted(additions, key=lambda item: item.relative_time)
        segments: list[MediaSegment] = []
        seen: set[str] = set()
        if initial and initial.any_url:
            segments.append(initial)
            seen.add(initial.any_url)
        for candidate in ordered_additions:
            if not candidate.any_url or candidate.any_url in seen:
                continue
            segments.append(candidate)
            seen.add(candidate.any_url)
        if segments and has_additions and initial and ordered_additions:
            # Только первый snapshot-файл может содержать преролл.
            segments[0].trim_duration = max(0.0, ordered_additions[0].relative_time)
        return segments

    # ------------------------------------------------------------------
    # Презентации
    # ------------------------------------------------------------------

    def _presentations(
        self, event_logs: list[Any], total_duration: float
    ) -> list[PresentationStream]:
        groups: dict[str, dict[str, Any]] = {}
        for event in event_logs:
            if not isinstance(event, dict) or event.get("module") != "presentation.update":
                continue
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            reference = data.get("fileReference")
            if not isinstance(reference, dict):
                continue
            file_info = reference.get("file")
            if not isinstance(file_info, dict):
                continue
            source_url = _http_url(file_info.get("downloadUrl")) or _http_url(file_info.get("url"))
            if not source_url:
                continue
            try:
                relative_time = max(0.0, float(event.get("relativeTime", 0.0)))
            except (TypeError, ValueError):
                continue

            group = groups.setdefault(
                str(file_info.get("id") or source_url),
                {
                    "name": str(file_info.get("name") or "presentation.pdf"),
                    "source_url": source_url,
                    "events": [],
                    "slides": file_info.get("slides"),
                    "displayed": set(),
                },
            )
            slide = reference.get("slide")
            slide_url: str | None = None
            alt_slide_url: str | None = None
            slide_name: str | None = None
            if isinstance(slide, dict) and slide.get("name"):
                slide_name = str(slide["name"])
                group["displayed"].add(slide_name)
                download_url = _http_url(slide.get("downloadUrl"))
                file_url = _http_url(slide.get("url"))
                slide_url = download_url or file_url
                alt_slide_url = file_url if file_url != slide_url else None
            group["events"].append(
                PresentationUpdate(
                    relative_time=relative_time,
                    is_active=data.get("isActive") is not False,
                    image_url=slide_url,
                    alt_image_url=alt_slide_url,
                    slide_name=slide_name,
                )
            )
            if not group.get("slides") and isinstance(file_info.get("slides"), list):
                group["slides"] = file_info["slides"]

        # Без финального presentation.update(false) последний слайд не
        # растягивается дальше начала screen share или конца записи.
        fallback_end = total_duration if total_duration > 0 else 0.0
        for event in event_logs:
            if not isinstance(event, dict) or event.get("module") != "mediasession.add":
                continue
            if stream_kind(event.get("data")) != SCREEN_KEY:
                continue
            screen_start = _float(event.get("relativeTime"), default=-1.0)
            if screen_start < 0:
                continue
            fallback_end = screen_start if fallback_end <= 0 else min(fallback_end, screen_start)

        presentations: list[PresentationStream] = []
        for index, group in enumerate(groups.values(), start=1):
            events: list[PresentationUpdate] = sorted(
                group["events"], key=lambda item: item.relative_time
            )
            if not events:
                continue
            intervals: list[tuple[float, float]] = []
            active_start: float | None = None
            for update in events:
                if not update.is_active:
                    if active_start is not None and update.relative_time >= active_start:
                        intervals.append((active_start, update.relative_time))
                        active_start = None
                    continue
                if active_start is None:
                    active_start = update.relative_time
            if active_start is not None:
                end_time = fallback_end or events[-1].relative_time
                if end_time > active_start:
                    intervals.append((active_start, end_time))

            if intervals:
                start_time = min(start for start, _ in intervals)
                duration = sum(end - start for start, end in intervals)
            else:
                start_time = events[0].relative_time
                duration = max(0.0, events[-1].relative_time - start_time)

            slides = group.get("slides")
            slide_count = (
                sum(1 for slide in slides if isinstance(slide, dict))
                if isinstance(slides, list)
                else 0
            )
            slide_count = max(slide_count, len(group["displayed"]))
            title = "Презентация"
            if len(groups) > 1:
                title = f"{title}: {group['name']}"
            presentations.append(
                PresentationStream(
                    key="presentation" if index == 1 else f"presentation-{index}",
                    title=title,
                    file_name=group["name"],
                    source_url=group["source_url"],
                    start_time=start_time,
                    duration=max(0.0, duration),
                    slide_count=slide_count,
                    updates=events,
                )
            )
        return presentations
