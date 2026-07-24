from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from market_maker_tool.cli import build_parser, default_targets, main
from market_maker_tool.config import Settings


class CliTests(unittest.TestCase):
    def test_parser_accepts_run_date(self) -> None:
        args = build_parser().parse_args(["run", "--date", "2026-06-08"])
        self.assertEqual(args.command, "run")
        self.assertEqual(args.date, date(2026, 6, 8))

    def test_removed_commands_are_rejected(self) -> None:
        for command in ("export", "reprocess", "test-history", "audit", "status"):
            with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    build_parser().parse_args([command])

    def test_no_llm_option_is_rejected(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["--no-llm", "run"])

    def test_run_with_date_executes_manual_pipeline(self) -> None:
        calls = []

        class FakePipeline:
            def __init__(self, settings, logger):
                pass

            def run(self, target_date, mode):
                calls.append((target_date, mode))
                return SimpleNamespace(status="SUCCESS")

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings.load(root_dir=tmp)
            with (
                patch("market_maker_tool.cli.Settings.load", return_value=settings),
                patch("market_maker_tool.cli.DailyPipeline", FakePipeline),
            ):
                exit_code = main(["run", "--date", "2026-06-08"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [(date(2026, 6, 8), "manual")])

    def test_default_targets_include_retries_and_catch_up_dates(self) -> None:
        yesterday = date.today() - timedelta(days=1)
        latest = yesterday - timedelta(days=4)
        retry = yesterday - timedelta(days=10)

        targets = default_targets(3, latest, [retry])

        self.assertEqual(targets[0], retry)
        self.assertEqual(targets[-1], yesterday)
        self.assertIn(latest + timedelta(days=1), targets)


if __name__ == "__main__":
    unittest.main()
