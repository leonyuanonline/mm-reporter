from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from .config import Settings
from .logging_utils import configure_logging
from .pipeline import DailyPipeline
from .run_lock import single_instance_lock


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="交易所ETF/上市基金做市公告日报工具")
    parser.add_argument("--config", help="配置文件路径，默认使用当前目录 config.json")
    parser.add_argument("--verbose", action="store_true", help="显示调试日志")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="运行日报；未指定日期时处理昨日并回看配置天数")
    run.add_argument("--date", type=iso_date, help="公告发布日期 YYYY-MM-DD")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.load(config_path=args.config)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        config_path = Path(args.config).resolve() if args.config else Path.cwd() / "config.json"
        print(f"配置文件错误 {config_path}: {exc}", file=sys.stderr)
        return 2
    logger = configure_logging(settings.log_dir, args.verbose)
    lock_path = settings.data_dir / "run.lock"

    try:
        with single_instance_lock(lock_path):
            pipeline = DailyPipeline(settings, logger)
            if args.date:
                targets = [args.date]
                mode = "manual"
            else:
                targets = default_targets(
                    settings.lookback_days,
                    pipeline.db.latest_successful_target_date("daily"),
                    pipeline.db.incomplete_daily_dates(date.today() - timedelta(days=1)),
                )
                mode = "daily"
            results = [pipeline.run(target, mode=mode) for target in targets]
            return 1 if any(result.status != "SUCCESS" for result in results) else 0
    except KeyboardInterrupt:
        logger.error("用户中断运行")
        return 130
    except Exception as exc:
        logger.exception("运行失败: %s", exc)
        return 1


def iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必须是 YYYY-MM-DD") from exc


def default_targets(
    lookback_days: int,
    latest_daily_date: date | None = None,
    retry_dates: list[date] | None = None,
) -> list[date]:
    yesterday = date.today() - timedelta(days=1)
    days = max(1, lookback_days)
    oldest = yesterday - timedelta(days=days - 1)
    if latest_daily_date is not None and latest_daily_date < oldest:
        oldest = latest_daily_date + timedelta(days=1)
    regular = [] if oldest > yesterday else [
        oldest + timedelta(days=offset) for offset in range((yesterday - oldest).days + 1)
    ]
    # Oldest first; incomplete prior runs stay in the queue until a SUCCESS run exists.
    return sorted({*(retry_dates or []), *regular})


if __name__ == "__main__":
    raise SystemExit(main())
