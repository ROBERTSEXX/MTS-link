"""Локальная «копия» МТС Линк: API журнала, страницы и медиафайлы.

Медиа генерируется ffmpeg, поэтому сквозные тесты проверяют реальную
сборку файлов, но не обращаются к настоящему сервису.
"""

from __future__ import annotations

import contextlib
import glob
import json
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

ENCODE = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def make_av(path: Path, seconds: float, size: str = "640x360", tone: int = 440) -> Path:
    _ffmpeg(
        "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency={tone}:duration={seconds}",
        *ENCODE, "-c:a", "aac", "-shortest", str(path),
    )
    return path


def make_video_only(path: Path, seconds: float, size: str = "1280x720") -> Path:
    _ffmpeg("-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25:duration={seconds}", *ENCODE, str(path))
    return path


def make_audio_only(path: Path, seconds: float, tone: int = 1000) -> Path:
    _ffmpeg("-f", "lavfi", "-i", f"sine=frequency={tone}:duration={seconds}",
            "-c:a", "aac", "-movflags", "+faststart", str(path))
    return path


def make_image(path: Path, color: str) -> Path:
    _ffmpeg("-f", "lavfi", "-i", f"color=c={color}:s=1280x720", "-frames:v", "1", str(path))
    return path


def probe(path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,width,height:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(completed.stdout)
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), {})
    return {
        "duration": float(data["format"]["duration"]),
        "types": {s["codec_type"] for s in data["streams"]},
        "width": video.get("width"),
        "height": video.get("height"),
    }


@dataclass
class Route:
    body: bytes | Path
    content_type: str = "application/octet-stream"
    require_cookie: str | None = None
    require_referer: bool = False


@dataclass
class FakeMtsLink:
    """Маршруты сервера; тесты добавляют их через ``add``."""

    base: str = ""
    routes: dict[str, Route] = field(default_factory=dict)
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def add(self, path: str, route: Route) -> str:
        self.routes[path] = route
        return self.base + path

    def add_json(self, path: str, data: Any, **kwargs: Any) -> str:
        return self.add(path, Route(json.dumps(data).encode(), "application/json", **kwargs))

    def add_html(self, path: str, html: str) -> str:
        return self.add(path, Route(html.encode(), "text/html; charset=utf-8"))

    def add_file(self, path: str, file: Path, content_type: str, **kwargs: Any) -> str:
        return self.add(path, Route(file, content_type, **kwargs))


def _handler(state: FakeMtsLink) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # тишина в выводе тестов
            pass

        def do_HEAD(self) -> None:  # noqa: N802
            self._serve(head=True)

        def do_GET(self) -> None:  # noqa: N802
            self._serve(head=False)

        def _serve(self, head: bool) -> None:
            path = self.path.split("?", 1)[0]
            state.requests.append((path, dict(self.headers)))
            route = state.routes.get(path)
            if route is None:
                self.send_error(404)
                return
            if route.require_cookie and route.require_cookie not in (self.headers.get("Cookie") or ""):
                body = json.dumps({"error": {"code": 403, "message": "Access denied"}}).encode()
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if route.require_referer and not self.headers.get("Referer"):
                self.send_error(403)
                return
            data = route.body.read_bytes() if isinstance(route.body, Path) else route.body
            start, end, status = 0, len(data) - 1, 200
            header = self.headers.get("Range")
            if header and header.startswith("bytes="):
                first, _, last = header[6:].partition("-")
                start = int(first or 0)
                end = int(last) if last else len(data) - 1
                status = 206
            chunk = data[start : end + 1]
            self.send_response(status)
            self.send_header("Content-Type", route.content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(chunk)))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            self.end_headers()
            if not head:
                # ffprobe закрывает соединение, прочитав заголовок файла.
                with contextlib.suppress(ConnectionResetError, BrokenPipeError):
                    self.wfile.write(chunk)

    return Handler


@pytest.fixture
def fake_mts() -> Iterator[FakeMtsLink]:
    state = FakeMtsLink()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    state.base = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="session")
def media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg/ffprobe не установлены")
    root = tmp_path_factory.mktemp("media")
    return {
        # 2 с преролла + 10 с полезной записи
        "speaker_a": make_av(root / "speaker-a.mp4", 12, tone=440),
        "speaker_b": make_av(root / "speaker-b.mp4", 10, tone=660),
        "screen": make_video_only(root / "screen.mp4", 5),
        "listener": make_audio_only(root / "listener.mp4", 3),
        "slide1": make_image(root / "slide1.jpg", "blue"),
        "slide2": make_image(root / "slide2.jpg", "green"),
        "full": make_av(root / "full.mp4", 6, size="320x240"),
        "flat1": make_av(root / "flat1.mp4", 5, size="320x240", tone=500),
        "flat2": make_av(root / "flat2.mp4", 5, size="320x240", tone=700),
    }


def publish_webinar(
    server: FakeMtsLink,
    media: dict[str, Path],
    session_id: str = "1001",
    private_cookie: str | None = None,
) -> str:
    """Опубликовать запись: камера (2 файла), экран, презентация, участник.

    Возвращает ссылку на страницу записи вида ``/j/<org>/<event>/record-new/<id>``.
    """

    secure = {"require_cookie": private_cookie} if private_cookie else {}

    def media_url(name: str, file: Path, content_type: str = "video/mp4") -> str:
        return server.add_file(f"/storage/{session_id}/{name}", file, content_type,
                               require_referer=True, **secure)

    speaker_a = media_url("speaker-a.mp4", media["speaker_a"])
    speaker_b = media_url("speaker-b.mp4", media["speaker_b"])
    screen = media_url("screen.mp4", media["screen"])
    listener = media_url("listener.mp4", media["listener"])
    slide1 = media_url("slide1.jpg", media["slide1"], "image/jpeg")
    slide2 = media_url("slide2.jpg", media["slide2"], "image/jpeg")
    deck = server.add(f"/storage/{session_id}/deck.pdf",
                      Route(b"%PDF-1.4\n% test deck\n", "application/pdf", require_referer=True, **secure))

    def slide_event(time: float, active: bool, url: str | None, name: str) -> dict[str, Any]:
        return {
            "module": "presentation.update",
            "relativeTime": time,
            "data": {
                "isActive": active,
                "fileReference": {
                    "file": {"id": 5, "name": "deck.pdf", "downloadUrl": deck,
                             "slides": [{"name": "1"}, {"name": "2"}]},
                    "slide": {"name": name, "url": url} if url else {"name": name},
                },
            },
        }

    record = {
        "name": "Тестовый вебинар",
        "duration": 20.0,
        "eventLogs": [
            {"relativeTime": 0.0, "snapshot": {"data": {
                "mediasession": [{"url": speaker_a, "stream": {"conference": {"id": 1}}}],
                "conference": [{"id": 1, "hasVideo": True, "hasAudio": True,
                                "user": {"id": 7, "nickname": "Лектор"}}],
            }}},
            {"module": "conference.update", "relativeTime": 14.0,
             "data": {"id": 2, "hasVideo": False, "hasAudio": True,
                      "user": {"id": 8, "nickname": "Слушатель"}}},
            slide_event(3.0, True, slide1, "1"),
            slide_event(6.0, True, slide2, "2"),
            slide_event(9.0, False, None, "2"),
            {"module": "mediasession.add", "relativeTime": 10.0,
             "data": {"url": speaker_b, "stream": {"conference": {"id": 1}}}},
            {"module": "mediasession.add", "relativeTime": 12.0,
             "data": {"url": screen, "stream": {"screensharing": {"id": 3}}}},
            {"module": "mediasession.add", "relativeTime": 15.0,
             "data": {"url": listener, "stream": {"conference": {"id": 2}}}},
        ],
    }
    server.add_json(f"/api/eventsessions/{session_id}/record", record, **secure)
    return f"{server.base}/j/org/event/record-new/{session_id}"


def chromium_path() -> str | None:
    candidates = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))
    return candidates[-1] if candidates else None


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


RunBatch = Callable[..., Any]
