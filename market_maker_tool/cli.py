from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from .audit_reporting import write_audit_report
from .config import Settings
from .logging_utils import configure_logging
from .pipeline import DailyPipeline
from .run_lock import single_instance_lock
from .storage import Database


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="交易所ETF/上市基金做市公告日报工具")
    parser.add_argument("--config", help="配置文件路径，默认使用当前目录 config.json")
    parser.add_argument("--verbose", action="store_true", help="显示调试日志")
    parser.add_argument("--no-llm", action="store_true", help="本次运行禁用大模型")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="运行日报；未指定日期时处理昨日并回看配置天数")
    run.add_argument("--date", type=iso_date, help="公告发布日期 YYYY-MM-DD")

    export = sub.add_parser("export", help="从数据库重新生成指定日期日报")
    export.add_argument("--date", type=iso_date, required=True)

    reprocess = sub.add_parser("reprocess", help="重新抽取一个已下载公告")
    reprocess.add_argument("--exchange", choices=("SSE", "SZSE"), required=True)
    reprocess.add_argument("--announcement-id", required=True)

    history = sub.add_parser("test-history", help="运行一段历史日期，用于验证，不代表正式回填")
    history.add_argument("--from", dest="date_from", type=iso_date, required=True)
    history.add_argument("--to", dest="date_to", type=iso_date, required=True)

    audit = sub.add_parser("audit", help="查看规则、各模型及最终共识的抽取审计")
    audit.add_argument("--date", type=iso_date, required=True, help="公告发布日期 YYYY-MM-DD")
    audit.add_argument("--announcement-id", help="只查看指定交易所官方公告ID")
    audit.add_argument("--json", dest="json_output", action="store_true", help="输出机器可读JSON")
    audit.add_argument("--all-runs", action="store_true", help="显示历史运行；默认每个公告只显示最新一次")

    sub.add_parser("status", help="显示最近运行记录")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.load(config_path=args.config)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        config_path = Path(args.config).resolve() if args.config else Path.cwd() / "config.json"
        print(f"配置文件错误 {config_path}: {exc}", file=sys.stderr)
        return 2
    if args.no_llm:
        settings.llm_enabled = False

    # Audit is read-only and deliberately bypasses the run lock.  This lets a
    # user inspect completed records while a scheduled collection is running,
    # and keeps --json output free from operational log lines.
    if args.command == "audit":
        try:
            rows = Database(settings.db_path).extraction_audits_for_date(
                args.date,
                announcement_id=args.announcement_id,
            )
            write_audit_report(
                rows,
                args.date,
                args.announcement_id,
                as_json=args.json_output,
                latest_only=not args.all_runs,
            )
            return 0
        except (OSError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
            print(f"读取抽取审计失败: {exc}", file=sys.stderr)
            return 1

    logger = configure_logging(settings.log_dir, args.verbose)
    lock_path = settings.data_dir / "run.lock"

    try:
        with single_instance_lock(lock_path):
            pipeline = DailyPipeline(settings, logger)
            if args.command == "run":
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
            if args.command == "export":
                paths = pipeline.export(args.date)
                for key, value in paths.items():
                    print(f"{key}: {value}")
                return 0
            if args.command == "reprocess":
                events = pipeline.reprocess(args.exchange, args.announcement_id)
                for event in events:
                    print(json.dumps(event.as_json_dict(), ensure_ascii=False))
                return 0
            if args.command == "test-history":
                if args.date_from > args.date_to:
                    raise ValueError("--from 不能晚于 --to")
                current = args.date_from
                failed = False
                while current <= args.date_to:
                    result = pipeline.run(current, mode="history-test")
                    failed = failed or result.status != "SUCCESS"
                    current += timedelta(days=1)
                return 1 if failed else 0
            if args.command == "status":
                for row in Database(settings.db_path).recent_runs():
                    print(
                        f"{row['run_id']:>4} {row['target_date']} {row['status']:<8} "
                        f"{row['started_at']} {row['error'] or ''}"
                    )
                return 0
    except KeyboardInterrupt:
        logger.error("用户中断运行")
        return 130
    except Exception as exc:
        logger.exception("运行失败: %s", exc)
        return 1
    return 0


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
