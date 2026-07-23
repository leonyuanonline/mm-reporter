from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path

from market_maker_tool.reporting import generate_reports


class ReportingTests(unittest.TestCase):
    def test_empty_day_generates_only_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = generate_reports(date(2026, 1, 1), [], Path(tmp))
            self.assertEqual(set(paths), {"csv"})
            self.assertEqual([path.suffix for path in Path(tmp).iterdir()], [".csv"])

    def test_csv_contains_report_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rows = [{
                "published_date": "2026-06-08",
                "exchange": "SZSE",
                "market_maker": "某证券公司",
                "security_code": "159698",
                "effective_date": "2026-06-08",
                "action": "新增",
                "service_type_raw": "流动性服务商",
                "source_urls": "https://example.test",
                "confidence": "LOW",
                "review_status": "NEEDS_REVIEW",
            }]
            paths = generate_reports(date(2026, 6, 8), rows, Path(tmp))
            with Path(paths["csv"]).open(encoding="utf-8-sig", newline="") as handle:
                exported = list(csv.DictReader(handle))
            self.assertEqual(len(exported), 1)
            self.assertEqual(exported[0]["公告发布日期"], "2026-06-08")
            self.assertEqual(exported[0]["证券代码"], "159698")


if __name__ == "__main__":
    unittest.main()
