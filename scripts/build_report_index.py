from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path


REPORT_PATTERN = re.compile(r"^market_making_report_(\d{4}-\d{2}-\d{2})\.csv$")


def build_index(report_dir: Path) -> dict[str, object]:
    reports: list[dict[str, object]] = []
    for path in report_dir.glob("market_making_report_*.csv"):
        match = REPORT_PATTERN.fullmatch(path.name)
        if not match:
            continue
        with path.open(encoding="utf-8-sig", newline="") as handle:
            record_count = sum(1 for _ in csv.DictReader(handle))
        reports.append({
            "date": match.group(1),
            "file": path.name,
            "records": record_count,
        })

    reports.sort(key=lambda item: str(item["date"]), reverse=True)
    return {
        "latest": reports[0]["date"] if reports else None,
        "reports": reports,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the static report manifest.")
    parser.add_argument("--report-dir", type=Path, default=Path("reports"))
    args = parser.parse_args()
    args.report_dir.mkdir(parents=True, exist_ok=True)
    index_path = args.report_dir / "index.json"
    index_path.write_text(
        json.dumps(build_index(args.report_dir), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(index_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
