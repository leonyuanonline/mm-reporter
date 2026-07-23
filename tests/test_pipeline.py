from __future__ import annotations

import logging
import tempfile
import unittest
from datetime import date
from pathlib import Path

from market_maker_tool.config import Settings
from market_maker_tool.models import AnnouncementCandidate
from market_maker_tool.pipeline import DailyPipeline
from market_maker_tool.sources import FetchedDocument


class EmptyExtractionSource:
    def list_for_date(self, target_date: date):
        return [AnnouncementCandidate(
            exchange="SSE",
            external_id="empty-extraction",
            canonical_url="https://example.test/empty",
            title="关于某ETF提供主做市服务的公告",
            published_date=target_date,
            publisher="上海证券交易所",
            source_kind="TEST",
            content_type="text/html",
        )]

    def fetch(self, candidate: AnnouncementCandidate):
        raw = "<html><body><div class='allZoom'>本公告未提供可解析的证券代码，提供主做市服务。</div></body></html>".encode()
        return FetchedDocument(candidate, raw, "text/html")


class FilteredProspectusSource:
    def list_for_date(self, target_date: date):
        return [AnnouncementCandidate(
            exchange="SZSE",
            external_id="filtered-prospectus",
            canonical_url="https://example.test/prospectus",
            title="信用债ETF博时：博时深证基准做市信用债交易型开放式指数证券投资基金更新招募说明书",
            published_date=target_date,
            publisher="测试基金管理有限公司",
            source_kind="TEST",
            content_type="text/html",
        )]

    def fetch(self, candidate: AnnouncementCandidate):
        raw = (
            "<html><body><div class='allZoom'>"
            "历史公告：2025年7月3日发布《关于新增某证券公司为部分基金流动性服务商的公告》。"
            "</div></body></html>"
        ).encode("utf-8")
        return FetchedDocument(candidate, raw, "text/html")


class PipelineTests(unittest.TestCase):
    def test_candidate_without_event_makes_run_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings.load(root_dir=tmp)
            settings.llm_enabled = False
            logger = logging.getLogger("pipeline-test")
            logger.handlers.clear()
            logger.addHandler(logging.NullHandler())
            pipeline = DailyPipeline(settings, logger)
            pipeline.sources = [EmptyExtractionSource()]
            result = pipeline.run(date(2026, 1, 2), mode="test")
            self.assertEqual(result.status, "PARTIAL")
            self.assertEqual(result.stats["extraction_failure_count"], 1)
            self.assertEqual(result.stats["audit_record_count"], 2)
            self.assertEqual(set(result.report_paths), {"csv"})
            self.assertTrue(result.report_paths["csv"].exists())
            audits = pipeline.db.extraction_audits_for_date(date(2026, 1, 2))
            self.assertEqual({row["extractor"] for row in audits}, {"RULE", "CONSENSUS"})
            rule = next(row for row in audits if row["extractor"] == "RULE")
            self.assertEqual(rule["status"], "EMPTY")
            self.assertIn("六位证券代码", rule["rejection_reasons_json"])

    def test_prospectus_with_historical_service_text_is_filtered_not_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings.load(root_dir=tmp)
            settings.llm_enabled = False
            logger = logging.getLogger("pipeline-filter-test")
            logger.handlers.clear()
            logger.addHandler(logging.NullHandler())
            pipeline = DailyPipeline(settings, logger)
            pipeline.sources = [FilteredProspectusSource()]

            result = pipeline.run(date(2026, 7, 7), mode="test")

            self.assertEqual(result.status, "SUCCESS")
            self.assertEqual(result.stats["candidate_count"], 1)
            self.assertEqual(result.stats["fetched_count"], 1)
            self.assertEqual(result.stats["parsed_count"], 1)
            self.assertEqual(result.stats["extraction_failure_count"], 0)
            self.assertEqual(result.stats["event_count"], 0)
            self.assertEqual(result.stats["audit_record_count"], 0)


if __name__ == "__main__":
    unittest.main()
