from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.build_report_index import build_index


class ReportIndexTests(unittest.TestCase):
    def test_reports_are_sorted_and_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp)
            (report_dir / "market_making_report_2026-07-20.csv").write_text(
                "\ufeff日期,代码\n2026-07-20,510001\n2026-07-20,510002\n",
                encoding="utf-8",
            )
            (report_dir / "market_making_report_2026-07-21.csv").write_text(
                "\ufeff日期,代码\n",
                encoding="utf-8",
            )
            (report_dir / "other.csv").write_text("ignored\n", encoding="utf-8")

            result = build_index(report_dir)

            self.assertEqual(result["latest"], "2026-07-21")
            self.assertEqual(
                result["reports"],
                [
                    {
                        "date": "2026-07-21",
                        "file": "market_making_report_2026-07-21.csv",
                        "records": 0,
                    },
                    {
                        "date": "2026-07-20",
                        "file": "market_making_report_2026-07-20.csv",
                        "records": 2,
                    },
                ],
            )


if __name__ == "__main__":
    unittest.main()
