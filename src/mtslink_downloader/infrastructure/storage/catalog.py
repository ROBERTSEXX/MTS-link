"""JSON-каталог уже скачанных ссылок для повторных запусков."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JsonCompletedCatalog:
    """Хранит ``url → файлы``; ссылка пропускается, только если файлы на месте."""

    FILE_NAME = ".mtslink-completed.json"

    def __init__(self, output_dir: Path) -> None:
        self._path = output_dir / self.FILE_NAME
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def completed_outputs(self, url: str) -> list[Path] | None:
        with self._lock:
            entry = self._load().get(url)
        if not isinstance(entry, dict):
            return None
        outputs = [Path(item) for item in entry.get("outputs") or [] if isinstance(item, str)]
        if outputs and all(path.exists() for path in outputs):
            return outputs
        return None

    def mark_completed(self, url: str, strategy: str, outputs: Sequence[Path]) -> None:
        with self._lock:
            data = self._load()
            data[url] = {
                "strategy": strategy,
                "outputs": [str(path) for path in outputs],
                "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._path.with_name(self._path.name + ".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self._path)
