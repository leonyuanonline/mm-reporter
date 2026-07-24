from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from market_maker_tool.models import (
    AnnouncementCandidate,
    ExtractionAuditRecord,
    MarketMakingEvent,
    ParsedAnnouncement,
)
from market_maker_tool.storage import Database


class StorageTests(unittest.TestCase):
    @staticmethod
    def store_announcement(
        db: Database,
        root: Path,
        *,
        external_id: str = "announcement-one",
        published_date: date = date(2026, 7, 3),
    ) -> int:
        candidate = AnnouncementCandidate(
            exchange="SSE",
            external_id=external_id,
            canonical_url=f"https://example.test/{external_id}",
            title="关于测试证券股份有限公司为测试ETF提供主做市服务的公告",
            published_date=published_date,
            publisher="上海证券交易所",
            source_kind="TEST",
        )
        parsed = ParsedAnnouncement(
            candidate=candidate,
            text="测试公告正文",
            raw_path=str(root / f"{external_id}.html"),
            text_path=str(root / f"{external_id}.txt"),
            raw_sha256="abc123",
            parser="test-parser",
        )
        return db.upsert_announcement(parsed)

    def test_semantic_event_dedup_preserves_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "app.db")
            base = dict(
                published_date=date(2026, 6, 8), exchange="SZSE", market_maker="中信建投证券股份有限公司",
                security_code="159698", effective_date=date(2026, 6, 8), action="新增",
                service_type_raw="一般流动性服务商", service_class="GENERAL", confidence="HIGH",
                review_status="AUTO_ACCEPTED", extractor="RULE", publisher="鹏华基金管理有限公司",
            )
            db.upsert_event(MarketMakingEvent(**base, source_url="https://example/one", announcement_external_id="one"))
            db.upsert_event(MarketMakingEvent(**base, source_url="https://example/two", announcement_external_id="two"))
            rows = db.events_for_date(date(2026, 6, 8))
            self.assertEqual(len(rows), 1)
            self.assertIn("https://example/one", rows[0]["source_urls"])
            self.assertIn("https://example/two", rows[0]["source_urls"])

    def test_legacy_bare_liquidity_events_are_migrated_and_sources_merged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.db"
            db = Database(path)
            base = dict(
                published_date=date(2026, 6, 8),
                exchange="SZSE",
                market_maker="中信建投证券股份有限公司",
                security_code="159698",
                effective_date=date(2026, 6, 8),
                action="新增",
                confidence="HIGH",
                review_status="AUTO_ACCEPTED",
                extractor="RULE",
                publisher="鹏华基金管理有限公司",
            )
            db.upsert_event(MarketMakingEvent(
                **base,
                service_type_raw="流动性服务商",
                service_class="UNSPECIFIED",
                source_url="https://example/legacy",
                announcement_external_id="legacy",
            ))
            db.upsert_event(MarketMakingEvent(
                **base,
                service_type_raw="一般流动性服务商",
                service_class="GENERAL",
                source_url="https://example/canonical",
                announcement_external_id="canonical",
            ))
            self.assertEqual(len(db.events_for_date(date(2026, 6, 8))), 2)

            # Reopening the database runs the idempotent data migration.
            migrated = Database(path)
            rows = migrated.events_for_date(date(2026, 6, 8))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["service_type_raw"], "一般流动性服务商")
            self.assertEqual(rows[0]["service_class"], "GENERAL")
            self.assertIn("https://example/legacy", rows[0]["source_urls"])
            self.assertIn("https://example/canonical", rows[0]["source_urls"])

            # A second initialization is a no-op and does not duplicate links.
            migrated_again = Database(path)
            self.assertEqual(len(migrated_again.events_for_date(date(2026, 6, 8))), 1)

    def test_extraction_audit_roundtrip_queries_and_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "app.db")
            run_id = db.start_run(date(2026, 7, 3), "manual")
            self.store_announcement(db, root)
            created_at = datetime(2026, 7, 13, 16, 30, 45)

            audit_id = db.write_extraction_audit(
                ExtractionAuditRecord(
                    run_id=run_id,
                    exchange="sse",
                    external_id="announcement-one",
                    extractor="DeepSeek-v4-flash",
                    stage="Validated",
                    status="success",
                    succeeded=True,
                    raw_response={
                        "id": "response-1",
                        "choices": [{"message": {"content": "{\"events\":[]}"}}],
                        "request": {
                            "api_key": "must-not-be-persisted",
                            "Authorization": "Bearer must-not-be-persisted",
                            "endpoint": "https://secret-endpoint.example/v1",
                        },
                    },
                    raw_events=[{"security_code": "588000", "action": "新增"}],
                    validated_events=[{"security_code": "588000", "action": "新增"}],
                    rejected_events=[{
                        "event": {"security_code": "not-a-code"},
                        "reasons": ["证券代码无原文证据"],
                    }],
                    rejection_reasons=["丢弃1个无效事件"],
                    warnings=[
                        "模型返回一条无效事件",
                        "Authorization: Bearer must-not-be-persisted",
                        "endpoint=https://secret-endpoint.example/v1",
                    ],
                    created_at=created_at,
                )
            )
            self.assertGreater(audit_id, 0)
            db.upsert_event(
                MarketMakingEvent(
                    published_date=date(2026, 7, 3),
                    exchange="SSE",
                    market_maker="测试证券股份有限公司",
                    security_code="588000",
                    security_name="测试ETF",
                    effective_date=date(2026, 7, 6),
                    action="新增",
                    service_type_raw="主做市服务",
                    service_class="PRIMARY",
                    source_url="https://example.test/announcement-one",
                    announcement_external_id="announcement-one",
                    extractor="CONSENSUS[RULE,DeepSeek-v4-flash]",
                    confidence="HIGH",
                    review_status="AUTO_ACCEPTED",
                )
            )

            with db.connect() as conn:
                rows = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT * FROM extraction_audits WHERE external_id=?",
                        ("announcement-one",),
                    ).fetchall()
                ]
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["audit_id"], audit_id)
            self.assertEqual(row["run_id"], run_id)
            self.assertEqual(row["exchange"], "SSE")
            self.assertEqual(row["external_id"], "announcement-one")
            self.assertEqual(row["extractor"], "DeepSeek-v4-flash")
            self.assertEqual(row["stage"], "validated")
            self.assertEqual(row["status"], "SUCCESS")
            self.assertEqual(row["succeeded"], 1)
            self.assertEqual(row["created_at"], "2026-07-13T16:30:45")
            self.assertEqual(json.loads(row["raw_events_json"])[0]["security_code"], "588000")
            self.assertEqual(json.loads(row["validated_events_json"])[0]["action"], "新增")
            self.assertEqual(
                json.loads(row["rejected_events_json"])[0]["reasons"],
                ["证券代码无原文证据"],
            )
            self.assertEqual(json.loads(row["rejection_reasons_json"]), ["丢弃1个无效事件"])
            persisted_json = "\n".join(
                str(row[name])
                for name in (
                    "raw_response_json",
                    "raw_events_json",
                    "validated_events_json",
                    "rejected_events_json",
                    "rejection_reasons_json",
                    "warnings_json",
                )
            )
            self.assertNotIn("must-not-be-persisted", persisted_json)
            self.assertNotIn("https://secret-endpoint.example", persisted_json)
            self.assertIn("[REDACTED]", persisted_json)

    def test_existing_database_is_migrated_additively(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            legacy_conn = sqlite3.connect(path)
            try:
                legacy_conn.execute("CREATE TABLE legacy_marker(value TEXT NOT NULL)")
                legacy_conn.execute("INSERT INTO legacy_marker(value) VALUES ('preserved')")
                legacy_conn.commit()
            finally:
                legacy_conn.close()

            db = Database(path)
            with db.connect() as conn:
                marker = conn.execute("SELECT value FROM legacy_marker").fetchone()[0]
                audit_table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='extraction_audits'"
                ).fetchone()
                columns = {
                    row[1]: row[2]
                    for row in conn.execute("PRAGMA table_info(extraction_audits)").fetchall()
                }
            self.assertEqual(marker, "preserved")
            self.assertIsNotNone(audit_table)
            self.assertEqual(columns["raw_response_json"], "TEXT")
            self.assertEqual(columns["raw_events_json"], "TEXT")
            self.assertEqual(columns["validated_events_json"], "TEXT")
            self.assertEqual(columns["rejected_events_json"], "TEXT")
            self.assertEqual(columns["warnings_json"], "TEXT")


if __name__ == "__main__":
    unittest.main()
