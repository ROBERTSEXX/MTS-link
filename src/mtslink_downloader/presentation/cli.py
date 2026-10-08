"""Командная строка: ``mtslink-dl`` / ``python -m mtslink_downloader``."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from mtslink_downloader.bootstrap import (
    STRATEGY_HELP,
    STRATEGY_NAMES,
    AppConfig,
    Container,
    default_profile_dir,
    parse_strategy_list,
)
from mtslink_downloader.domain.errors import MtsLinkError
from mtslink_downloader.domain.models import DownloadSettings, ExportMode, JobStatus
from mtslink_downloader.infrastructure.sources.endpoints import DEFAULT_API_HOSTS
from mtslink_downloader.infrastructure.storage.link_list import LinkListReader
from mtslink_downloader.infrastructure.storage.report import JsonBatchReportWriter
from mtslink_downloader.presentation.console import ConsoleReporter, print_inspection

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

EPILOG = """\
Способы скачивания (перебираются по порядку, пока один не сработает):
{strategies}

Формат файла со списком: одна ссылка в строке; после ссылки можно указать
имя файла; строки с # — комментарии. Пример:

  # курс по Python
  https://my.mts-link.ru/j/org/123/record-new/456   Лекция 1
  https://my.mts-link.ru/12/34/record-new/56/record-file/78 | Лекция 2

Примеры:
  mtslink-dl -i links.txt -o downloads
  mtslink-dl URL1 URL2 --session-id XXXX
  mtslink-dl -i links.txt --login            # войти в браузере один раз
  mtslink-dl -i links.txt --mode all -j 2    # все источники, 2 записи параллельно
  mtslink-dl -i downloads/mtslink-failed.txt # повторить неудачные
"""


def build_parser() -> argparse.ArgumentParser:
    strategies = "\n".join(f"  {name:<8} {STRATEGY_HELP[name]}" for name in STRATEGY_NAMES)
    parser = argparse.ArgumentParser(
        prog="mtslink-dl",
        description="Пакетное скачивание записей МТС Линк всеми доступными способами.",
        epilog=EPILOG.format(strategies=strategies),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("urls", nargs="*", help="ссылки на записи (можно несколько)")
    parser.add_argument(
        "-i", "--input", action="append", type=Path, default=[], metavar="FILE",
        help="файл со списком ссылок (можно повторять; '-' — читать из stdin)",
    )
    parser.add_argument(
        "-o", "--output-dir", type=Path, default=Path("downloads"),
        help="папка для результатов (по умолчанию downloads)",
    )
    parser.add_argument(
        "-m", "--mode", choices=[mode.value for mode in ExportMode], default=ExportMode.AUTO.value,
        help="auto — одно лучшее видео; speaker — только камера; composite — сводное видео; "
        "all — все источники отдельно + сводное (по умолчанию auto)",
    )
    parser.add_argument(
        "-s", "--strategies", default=",".join(STRATEGY_NAMES),
        help="способы и их порядок через запятую (по умолчанию: %(default)s)",
    )
    auth = parser.add_argument_group("доступ к закрытым записям")
    auth.add_argument("--session-id", help="значение cookie sessionId из браузера")
    auth.add_argument("--cookies", type=Path, help="cookies.txt (формат Netscape)")
    auth.add_argument(
        "--login", action="store_true",
        help="если запись закрыта — открыть окно браузера для входа (один раз на весь список)",
    )
    browser = parser.add_argument_group("браузер")
    browser.add_argument("--headed", action="store_true", help="всегда показывать окно браузера")
    browser.add_argument(
        "--profile", type=Path, default=default_profile_dir(),
        help="папка профиля браузера, где сохраняется вход (по умолчанию %(default)s)",
    )
    browser.add_argument("--no-profile", action="store_true", help="чистый браузер без профиля")
    browser.add_argument(
        "--browser-channel", help="установленный браузер вместо Chromium Playwright: chrome, msedge"
    )
    browser.add_argument("--browser-path", help="путь к исполняемому файлу Chromium/Chrome")
    parser.add_argument(
        "--api-host", action="append", default=[], metavar="URL",
        help="дополнительный адрес API (например, для корпоративного домена)",
    )
    parser.add_argument("-j", "--jobs", type=int, default=1, help="сколько записей качать одновременно")
    parser.add_argument("--overwrite", action="store_true", help="перекачать и перезаписать готовое")
    parser.add_argument(
        "--keep-temp", action="store_true", help="не удалять кэш сегментов (.mtslink-work)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="только показать, что найдено в записях, без скачивания"
    )
    parser.add_argument("-v", "--verbose", action="count", default=0, help="подробный журнал (-vv)")
    return parser


def _configure_logging(verbosity: int, parallel: bool) -> None:
    level = logging.WARNING if verbosity == 0 else logging.INFO if verbosity == 1 else logging.DEBUG
    fmt = "%(threadName)s %(levelname)s: %(message)s" if parallel else "%(levelname)s: %(message)s"
    logging.basicConfig(level=logging.WARNING, format=fmt)
    logging.getLogger("mtslink_downloader").setLevel(level)


def _ask_enter(message: str) -> None:
    if not sys.stdin.isatty():
        raise MtsLinkError("Вход в браузере требует интерактивного терминала.")
    input(f"\n>>> {message}: ")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose, args.jobs > 1)

    try:
        strategies = parse_strategy_list(args.strategies)
        jobs, warnings = LinkListReader().read(args.urls, args.input)
    except MtsLinkError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return EXIT_USAGE
    for warning in warnings:
        print(f"Предупреждение: {warning}", file=sys.stderr)
    if not jobs:
        parser.print_usage(sys.stderr)
        print("Ошибка: не передано ни одной ссылки (аргументами или через -i FILE).", file=sys.stderr)
        return EXIT_USAGE

    config = AppConfig(
        output_dir=args.output_dir,
        strategies=strategies,
        session_id=args.session_id,
        cookies_file=args.cookies,
        headed=args.headed,
        login=args.login,
        profile_dir=None if args.no_profile else args.profile,
        browser_channel=args.browser_channel,
        browser_path=args.browser_path,
        api_hosts=tuple(dict.fromkeys((*args.api_host, *DEFAULT_API_HOSTS))),
    )
    mode = ExportMode(args.mode)
    try:
        container = Container(config, login_prompt=_ask_enter)
        reporter = ConsoleReporter()
        app = container.build(reporter)
    except MtsLinkError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return EXIT_USAGE

    jobs, event_warnings = app.events.expand(jobs)
    for warning in event_warnings:
        print(f"Предупреждение: {warning}", file=sys.stderr)
    if not jobs:
        print("Ошибка: записей для скачивания не найдено.", file=sys.stderr)
        return EXIT_FAILED

    if args.dry_run:
        failed = 0
        for job in jobs:
            try:
                print_inspection(job, app.inspect.execute(job, mode))
            except MtsLinkError as exc:
                failed += 1
                print(f"\n{job.link.url}\n  ✗ {exc}")
        return EXIT_FAILED if failed else EXIT_OK

    print(
        f"Ссылок: {len(jobs)}. Папка: {args.output_dir.resolve()}. "
        f"Способы: {' → '.join(app.strategy_names)}."
    )
    settings = DownloadSettings(
        output_dir=args.output_dir,
        mode=mode,
        overwrite=args.overwrite,
        keep_work_files=args.keep_temp,
    )
    try:
        summary = app.batch.execute(jobs, settings, parallel_jobs=args.jobs)
    except KeyboardInterrupt:
        print("\nПрервано. Повторный запуск продолжит с места остановки.", file=sys.stderr)
        return 130
    except MtsLinkError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return EXIT_FAILED

    report = args.output_dir / JsonBatchReportWriter.REPORT_NAME
    hint = f"Отчёт: {report}"
    if summary.count(JobStatus.FAILED):
        failed_list = args.output_dir / JsonBatchReportWriter.FAILED_NAME
        hint += f"\nНеудачные ссылки: {failed_list} (повтор: mtslink-dl -i \"{failed_list}\")"
    reporter.summary(summary, hint)
    return EXIT_FAILED if summary.count(JobStatus.FAILED) else EXIT_OK
