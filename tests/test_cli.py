from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from market_maker_tool.audit_reporting import build_audit_payload, write_audit_report
from market_maker_tool.cli import build_parser, main
from market_maker_tool.config import Settings
from market_maker_tool.models import (
    AnnouncementCandidate,
    ExtractionAuditRecord,
    ParsedAnnouncement,
)
from market_maker_tool.storage import Database


class AuditReportingTests(unittest.TestCase):
    def sample_rows(self):
        final_event = {
            "market_maker": "中信建投证券股份有限公司",
            "security_code": "159698",
            "effective_date": "2026-06-08",
            "action": "新增",
            "service_type_raw": "流动性服务商",
            "confidence": "HIGH",
            "review_status": "AUTO_ACCEPTED",
            "evidence": [{"field_name": "security_code", "quote": "代码：159698"}],
            "warnings": [],
        }
        common = {
            "exchange": "SZSE",
            "external_id": "announcement-1",
            "title": "新增流动性服务商公告",
            "published_date": "2026-06-08",
            "canonical_url": "https://example.test/announcement-1",
            "raw_path": "data/raw/a.pdf",
            "text_path": "data/text/a.txt",
            "parse_warnings_json": "[]",
            "final_events_json": json.dumps([final_event], ensure_ascii=False),
            "field_votes_json": json.dumps(
                [{
                    "field": "security_code",
                    "selected": "159698",
                    "votes": {"RULE": "159698", "model-a": "159698"},
                }],
                ensure_ascii=False,
            ),
        }
        return [
            {
                **common,
                "extractor_name": "RULE",
                "attempt_status": "SUCCESS",
                "raw_events_json": json.dumps([final_event], ensure_ascii=False),
                "validated_events_json": json.dumps([final_event], ensure_ascii=False),
                "rejected_events_json": "[]",
                "warnings_json": "[]",
            },
            {
                **common,
                "extractor_name": "model-a",
                "attempt_status": "SUCCESS",
                "stage": "validated",
                "succeeded": True,
                "model": "example-model",
                "raw_response_json": json.dumps(
                    {"request": {"Authorization": "Bearer must-not-leak"}},
                    ensure_ascii=False,
                ),
                "raw_events_json": json.dumps(
                    [{**final_event, "api_key": "must-not-leak"}], ensure_ascii=False
                ),
                "validated_events_json": json.dumps([final_event], ensure_ascii=False),
                "rejected_events_json": json.dumps(
                    [{"raw_event": {"security_code": "bad"}, "reasons": ["代码非法"]}],
                    ensure_ascii=False,
                ),
                "warnings_json": json.dumps(["一个测试警告"], ensure_ascii=False),
                "rejection_reasons_json": json.dumps(["丢弃1个无效事件"], ensure_ascii=False),
            },
        ]

    def test_flat_rows_are_grouped_and_credentials_are_redacted(self) -> None:
        payload = build_audit_payload(self.sample_rows(), date(2026, 6, 8))
        self.assertEqual(payload["announcement_count"], 1)
        announcement = payload["announcements"][0]
        self.assertEqual(len(announcement["attempts"]), 2)
        self.assertEqual(len(announcement["final_events"]), 1)
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("must-not-leak", rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_human_report_shows_attempts_events_votes_and_rejections(self) -> None:
        output = io.StringIO()
        write_audit_report(self.sample_rows(), "2026-06-08", stream=output)
        rendered = output.getvalue()
        self.assertIn("抽取器调用 (2)", rendered)
        self.assertIn("原始事件", rendered)
        self.assertIn("校验后事件", rendered)
        self.assertIn("拒绝原因", rendered)
        self.assertIn("汇总拒绝原因", rendered)
        self.assertIn("阶段 validated", rendered)
        self.assertIn("逐字段投票/对账", rendered)
        self.assertIn("最终事件 (1)", rendered)
        self.assertNotIn("must-not-leak", rendered)

    def test_latest_run_is_default_but_history_can_be_requested(self) -> None:
        current = self.sample_rows()
        for row in current:
            row["run_id"] = 2
        old = dict(current[0])
        old["run_id"] = 1
        old["validated_events_json"] = "[]"
        latest = build_audit_payload([old, *current], date(2026, 6, 8))
        history = build_audit_payload(
            [old, *current],
            date(2026, 6, 8),
            latest_only=False,
        )
        self.assertEqual(len(latest["announcements"][0]["attempts"]), 2)
        self.assertEqual(len(history["announcements"][0]["attempts"]), 3)


class AuditCliTests(unittest.TestCase):
    def test_parser_accepts_audit_options(self) -> None:
        args = build_parser().parse_args([
            "audit",
            "--date",
            "2026-06-08",
            "--announcement-id",
            "announcement-1",
            "--json",
            "--all-runs",
        ])
        self.assertEqual(args.command, "audit")
        self.assertEqual(args.date, date(2026, 6, 8))
        self.assertEqual(args.announcement_id, "announcement-1")
        self.assertTrue(args.json_output)
        self.assertTrue(args.all_runs)

    def test_audit_json_command_calls_database_filter(self) -> None:
        calls = []

        class FakeDatabase:
            def __init__(self, path):
                self.path = path

            def extraction_audits_for_date(self, target_date, announcement_id=None):
                calls.append((target_date, announcement_id))
                return []

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings.load(root_dir=tmp)
            output = io.StringIO()
            with (
                patch("market_maker_tool.cli.Settings.load", return_value=settings),
                patch("market_maker_tool.cli.Database", FakeDatabase),
                patch("sys.stdout", output),
            ):
                exit_code = main([
                    "audit",
                    "--date",
                    "2026-06-08",
                    "--announcement-id",
                    "announcement-1",
                    "--json",
                ])

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [(date(2026, 6, 8), "announcement-1")])
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["announcement_count"], 0)
        self.assertEqual(payload["announcement_id"], "announcement-1")

    def test_audit_command_reads_real_sqlite_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings.load(root_dir=root)
            candidate = AnnouncementCandidate(
                exchange="SSE",
                external_id="real-audit",
                canonical_url="https://example.test/real-audit",
                title="关于某ETF提供主做市服务的公告",
                published_date=date(2026, 6, 8),
                publisher="上海证券交易所",
                source_kind="TEST",
            )
            parsed = ParsedAnnouncement(
                candidate=candidate,
                text="某ETF审计正文",
                raw_path=str(root / "raw.html"),
                text_path=str(root / "text.txt"),
                raw_sha256="hash",
                parser="test",
            )
            db = Database(settings.db_path)
            db.upsert_announcement(parsed)
            db.write_extraction_audit(
                ExtractionAuditRecord(
                    exchange="SSE",
                    external_id="real-audit",
                    extractor="RULE",
                    stage="validated",
                    succeeded=True,
                    status="SUCCESS",
                    raw_events=[{"security_code": "588000"}],
                    validated_events=[{"security_code": "588000", "action": "新增"}],
                )
            )
            output = io.StringIO()
            with (
                patch("market_maker_tool.cli.Settings.load", return_value=settings),
                patch("sys.stdout", output),
            ):
                exit_code = main(["audit", "--date", "2026-06-08", "--json"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["announcement_count"], 1)
        attempt = payload["announcements"][0]["attempts"][0]
        self.assertEqual(attempt["extractor"], "RULE")
        self.assertEqual(attempt["stage"], "validated")
        self.assertEqual(attempt["validated_events"][0]["action"], "新增")


if __name__ == "__main__":
    unittest.main()
