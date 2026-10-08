"""Простой HTTP-клиент на стандартной библиотеке с повторами."""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mtslink_downloader.domain.errors import (
    AccessDeniedError,
    MediaProcessingError,
    RecordUnavailableError,
)

LOG = logging.getLogger(__name__)


class HttpClient:
    """GET-запросы JSON и потоковое скачивание файлов с докачкой."""

    def __init__(self, timeout: float = 60.0, retries: int = 6, backoff: float = 2.0) -> None:
        self._timeout = timeout
        self._retries = max(1, retries)
        self._backoff = backoff

    def get_json(self, url: str, headers: dict[str, str]) -> Any:
        """Вернуть JSON или поднять понятную доменную ошибку.

        Ответ вида ``{"error": {"code": 403}}`` МТС Линк иногда отдаёт со
        статусом 200, поэтому код ошибки проверяется и внутри тела.
        """

        request_headers = {"Accept": "application/json, text/plain, */*", **headers}
        body = self._with_retries(lambda: self._read(url, request_headers), url)
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecordUnavailableError(f"Ответ {url} не является JSON") from exc
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            code = data["error"].get("code")
            message = data["error"].get("message") or "ошибка API"
            if code in (401, 403):
                raise AccessDeniedError(f"доступ запрещён ({code}: {message})")
            raise RecordUnavailableError(f"API вернул ошибку {code}: {message}")
        return data

    def download(self, url: str, destination: Path, headers: dict[str, str]) -> None:
        """Скачать файл, продолжая частично скачанный ``.part`` через Range."""

        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")

        def attempt() -> None:
            offset = partial.stat().st_size if partial.exists() else 0
            request_headers = dict(headers)
            if offset:
                request_headers["Range"] = f"bytes={offset}-"
            request = Request(url, headers=request_headers)
            with urlopen(request, timeout=self._timeout) as response:
                resumed = offset and response.status == 206
                mode = "ab" if resumed else "wb"
                with partial.open(mode) as output:
                    shutil.copyfileobj(response, output, length=1024 * 1024)

        try:
            self._with_retries(attempt, url)
        except AccessDeniedError:
            partial.unlink(missing_ok=True)
            raise
        if not partial.exists() or partial.stat().st_size == 0:
            partial.unlink(missing_ok=True)
            raise MediaProcessingError(f"Сервер вернул пустой файл: {_short(url)}")
        os.replace(partial, destination)

    def _read(self, url: str, headers: dict[str, str]) -> bytes:
        with urlopen(Request(url, headers=headers), timeout=self._timeout) as response:
            return bytes(response.read())

    def _with_retries(self, action: Any, url: str) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, self._retries + 1):
            try:
                return action()
            except HTTPError as exc:
                if exc.code in (401, 403):
                    raise AccessDeniedError(f"доступ запрещён (HTTP {exc.code})") from exc
                if exc.code == 404:
                    raise RecordUnavailableError(f"не найдено (HTTP 404): {_short(url)}") from exc
                if exc.code == 416:
                    # Range за пределами файла: файл уже скачан целиком.
                    return None
                last_error = exc
            except (URLError, TimeoutError, ConnectionError, OSError) as exc:
                last_error = exc
            if attempt < self._retries:
                # 2, 4, 8, 16, 30 с: обрывы соединения у хранилища бывают сериями.
                delay = min(30.0, self._backoff**attempt)
                LOG.debug("Повтор %s через %.1f с: %s", _short(url), delay, last_error)
                time.sleep(delay)
        raise MediaProcessingError(f"Сетевая ошибка для {_short(url)}: {last_error}")


def _short(url: str) -> str:
    """Ссылка без query-параметров: подписи и токены не попадают в логи."""

    return url.split("?", 1)[0]
