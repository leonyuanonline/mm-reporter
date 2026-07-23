from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping
from dataclasses import is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


REPORT_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("公告发布日期", ("published_date", "公告发布日期")),
    ("交易所", ("exchange", "交易所")),
    ("做市商", ("market_maker", "做市商")),
    ("证券代码", ("security_code", "证券代码")),
    ("生效日期", ("effective_date", "生效日期")),
    ("动作", ("action", "动作")),
    ("服务类型原文", ("service_type_raw", "服务类型原文")),
    ("来源URL", ("source_urls", "source_url", "来源URL")),
)

REPORT_HEADERS: tuple[str, ...] = tuple(item[0] for item in REPORT_COLUMNS)


def generate_reports(
    target_date: date | datetime | str,
    rows: Iterable[Mapping[str, Any] | Any],
    report_dir: str | Path,
) -> dict[str, Path]:
    """Generate the daily CSV report with the eight public report columns."""

    report_date = _iso_date(target_date)
    output_dir = Path(report_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_rows = [_make_report_row(row) for row in rows]
    csv_path = output_dir / f"market_making_report_{report_date}.csv"
    _write_csv(csv_path, report_rows)
    return {"csv": csv_path}


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    # BOM makes Chinese text open correctly in Excel.
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_HEADERS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _make_report_row(source: Mapping[str, Any] | Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for header, aliases in REPORT_COLUMNS:
        value = _get_value(source, aliases)
        if header in {"公告发布日期", "生效日期"}:
            result[header] = _date_or_text(value)
        else:
            result[header] = "" if value is None else str(value)
    return result


def _get_value(source: Mapping[str, Any] | Any, aliases: tuple[str, ...]) -> Any:
    if isinstance(source, Mapping):
        for name in aliases:
            if name in source and source[name] is not None:
                return source[name]
        return None
    if is_dataclass(source) or source is not None:
        for name in aliases:
            if hasattr(source, name):
                value = getattr(source, name)
                if value is not None:
                    return value
    return None


def _iso_date(value: date | datetime | str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(
            "target_date must be a date or an ISO date string (YYYY-MM-DD)"
        ) from exc


def _date_or_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)
