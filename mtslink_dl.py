#!/usr/bin/env python3
"""Запуск пакетного загрузчика без установки: ``python mtslink_dl.py -i links.txt``."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from mtslink_downloader.presentation.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
