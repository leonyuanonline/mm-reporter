from __future__ import annotations

import unittest
import json
import threading
from dataclasses import replace
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from market_maker_tool.config import LLMProviderConfig, Settings
from market_maker_tool.extraction import (
    LLMExtractionResult,
    LLMExtractor,
    RuleExtractor,
    extract_events,
    is_candidate,
    reconcile_model_results,
)
from market_maker_tool.models import AnnouncementCandidate, Evidence, ParsedAnnouncement


def parsed(exchange: str, title: str, text: str, external_id: str = "test") -> ParsedAnnouncement:
    candidate = AnnouncementCandidate(
        exchange=exchange,
        external_id=external_id,
        canonical_url="https://example.test/announcement",
        title=title,
        published_date=date(2026, 6, 8),
        publisher="测试发布主体",
        source_kind="TEST",
    )
    return ParsedAnnouncement(candidate, text, "raw", "text", "hash", "test")


class RuleExtractionTests(unittest.TestCase):
    def test_szse_multi_event_with_pdf_spacing(self) -> None:
        item = parsed(
            "SZSE",
            "云计算ETF鹏华：关于鹏华基金管理有限公司旗下部分基金新增流动性服务商的公告",
            """关于鹏华基金管理有限公司旗下部分基金新增流动性服务商的公告
为促进相关基金的市场流动性和平稳运行，自 2026 年 6 月 8 日起，
本公司新增中信建投证券股份有限公司为粮食 ETF 鹏华（代
码：159698）流动性服务商、新增东方财富证券股份有限公司为云计算 ETF 鹏
华（代码：159739）流动性服务商。
鹏华基金管理有限公司 2026 年 6 月 8 日""",
        )
        events = RuleExtractor().extract(item)
        self.assertEqual({event.security_code for event in events}, {"159698", "159739"})
        self.assertEqual({event.market_maker for event in events}, {"中信建投证券股份有限公司", "东方财富证券股份有限公司"})
        self.assertTrue(all(event.action == "新增" for event in events))
        self.assertTrue(all(event.service_type_raw == "一般流动性服务商" for event in events))
        self.assertTrue(all(event.service_class == "GENERAL" for event in events))
        self.assertTrue(all(
            any(
                evidence.field_name == "service_type_raw"
                and "流动性服务商" in "".join(evidence.quote.split())
                for evidence in event.evidence
            )
            for event in events
        ))
        self.assertTrue(all(event.effective_date == date(2026, 6, 8) for event in events))

    def test_sse_implicit_add(self) -> None:
        item = parsed(
            "SSE",
            "关于中信证券股份有限公司为易方达上证科创板芯片交易型开放式指数证券投资基金提供主做市服务的公告",
            "根据相关规定，经中信证券股份有限公司备案申请，自2026年07月13日起，中信证券股份有限公司为易方达上证科创板芯片交易型开放式指数证券投资基金（基金代码：589130）提供主做市服务。",
        )
        events = RuleExtractor().extract(item)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].action, "新增")
        self.assertEqual(events[0].service_type_raw, "主做市服务")
        self.assertEqual(events[0].effective_date, date(2026, 7, 13))

    def test_excludes_etf_options(self) -> None:
        item = parsed(
            "SZSE",
            "关于深圳证券交易所ETF期权做市商的公告",
            "某证券公司成为ETF期权主做市商。",
        )
        self.assertFalse(is_candidate(item.candidate, item.text))

    def test_service_phrase_must_appear_in_both_title_and_body(self) -> None:
        prospectus = parsed(
            "SZSE",
            "信用债ETF博时：博时深证基准做市信用债交易型开放式指数证券投资基金更新招募说明书",
            "历史公告：2025年7月3日发布《关于新增某证券公司为部分基金流动性服务商的公告》。",
        )
        self.assertFalse(is_candidate(prospectus.candidate, prospectus.text))

        title_only = parsed(
            "SZSE",
            "关于某ETF新增流动性服务商的公告",
            "本公告仅调整申购、赎回替代金额处理程序，不涉及服务商变更。",
        )
        self.assertFalse(is_candidate(title_only.candidate, title_only.text))

    def test_candidate_uses_short_exchange_specific_service_keyword(self) -> None:
        sse = parsed(
            "SSE",
            "关于某ETF做市服务安排的公告",
            "本次做市服务安排涉及某ETF（基金代码：589999）。",
        )
        szse = parsed(
            "SZSE",
            "关于某ETF流动性服务安排的公告",
            "本次流动性服务安排涉及某ETF（代码：159999）。",
        )
        self.assertTrue(is_candidate(sse.candidate, sse.text))
        self.assertTrue(is_candidate(szse.candidate, szse.text))

        wrong_for_sse = parsed(
            "SSE",
            "关于某ETF流动性服务安排的公告",
            "本次流动性服务安排涉及某ETF（基金代码：589999）。",
        )
        wrong_for_szse = parsed(
            "SZSE",
            "关于某ETF做市服务安排的公告",
            "本次做市服务安排涉及某ETF（代码：159999）。",
        )
        self.assertFalse(is_candidate(wrong_for_sse.candidate, wrong_for_sse.text))
        self.assertFalse(is_candidate(wrong_for_szse.candidate, wrong_for_szse.text))

    def test_sse_termination_company_before_action(self) -> None:
        item = parsed(
            "SSE",
            "关于广发证券股份有限公司终止为博时上证科创板综合交易型开放式指数证券投资基金提供主做市服务的公告",
            "根据相关规定，自2026年07月13日起，广发证券股份有限公司终止为博时上证科创板综合交易型开放式指数证券投资基金（基金代码：589900）提供主做市服务。",
        )
        events = RuleExtractor().extract(item)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].market_maker, "广发证券股份有限公司")
        self.assertEqual(events[0].action, "终止")
        self.assertEqual(events[0].security_code, "589900")

    def test_szse_selected_provider_with_bare_parenthesized_code(self) -> None:
        item = parsed(
            "SZSE",
            "软件ETF华夏：华夏基金管理有限公司关于华夏中证全指软件交易型开放式指数证券投资基金流动性服务商的公告",
            "自 2026 年 7 月 10 日起，华夏基金管理有限公司选定方正证券股份有限公司为软件 ETF 华夏（159068）的流动性服\n务商。",
        )
        events = RuleExtractor().extract(item)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].market_maker, "方正证券股份有限公司")
        self.assertEqual(events[0].security_code, "159068")
        self.assertEqual(events[0].action, "新增")
        self.assertEqual(events[0].service_type_raw, "一般流动性服务商")
        self.assertEqual(events[0].service_class, "GENERAL")

    def test_szse_designated_provider_listed_after_fund_code(self) -> None:
        item = parsed(
            "SZSE",
            "稀土ETF易方达：易方达基金管理有限公司关于指定旗下部分证券投资基金主流动性服务商的公告",
            """[第 1 页]
易方达基金管理有限公司关于指定旗下部分证券投资基金主流
动性服务商的公告
根据有关规定，自 2026 年 7 月 3 日起，本公司指定下列流动性服务商为相关证券投资基金的主流动性服务商：
1. 易方达中证稀土产业交易型开放式指数证券投资基金（159715）：
浙商证券股份有限公司
特此公告。
易方达基金管理有限公司
2026 年 7 月 3 日""",
        )
        events = RuleExtractor().extract(item)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].market_maker, "浙商证券股份有限公司")
        self.assertEqual(events[0].security_code, "159715")
        self.assertEqual(events[0].effective_date, date(2026, 7, 3))
        self.assertEqual(events[0].action, "新增")
        self.assertEqual(events[0].service_type_raw, "主流动性服务商")

    def test_szse_post_code_list_pairs_each_fund_with_its_provider(self) -> None:
        item = parsed(
            "SZSE",
            "某基金管理有限公司关于指定旗下基金主流动性服务商的公告",
            """自2026年7月3日起，本公司指定下列流动性服务商为相关证券投资基金的主流动性服务商：
1. 甲ETF（159715）：
浙商证券股份有限公司
2. 乙ETF（159716）：
中信证券股份有限公司
特此公告。""",
        )
        events = RuleExtractor().extract(item)
        self.assertEqual(
            {(event.security_code, event.market_maker) for event in events},
            {
                ("159715", "浙商证券股份有限公司"),
                ("159716", "中信证券股份有限公司"),
            },
        )

    def test_tier_adjustment_uses_new_service_tier(self) -> None:
        item = parsed(
            "SZSE",
            "关于某ETF调整流动性服务商类别的公告",
            "自2026年7月15日起，将中信证券股份有限公司为某ETF（代码：159999）的服务类别由一般流动性服务商调整为主流动性服务商。",
        )
        events = RuleExtractor().extract(item)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].action, "调整")
        self.assertEqual(events[0].service_type_raw, "主流动性服务商")
        self.assertEqual(events[0].service_class, "PRIMARY")

    def test_tier_adjustment_can_move_to_general(self) -> None:
        item = parsed(
            "SSE",
            "关于某证券公司调整某ETF做市服务类别的公告",
            "自2026年7月15日起，中信证券股份有限公司为某ETF（基金代码：589999）的服务类别由主做市服务变更为一般做市服务。",
        )
        events = RuleExtractor().extract(item)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].action, "调整")
        self.assertEqual(events[0].service_type_raw, "一般做市服务")
        self.assertEqual(events[0].service_class, "GENERAL")

    def test_tier_adjustment_to_bare_liquidity_provider_is_general(self) -> None:
        item = parsed(
            "SZSE",
            "关于某ETF调整流动性服务商类别的公告",
            "自2026年7月15日起，将中信证券股份有限公司为某ETF（代码：159999）的服务类别由主流动性服务商调整为流动性服务商。",
        )
        events = RuleExtractor().extract(item)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].action, "调整")
        self.assertEqual(events[0].service_type_raw, "一般流动性服务商")
        self.assertEqual(events[0].service_class, "GENERAL")
        self.assertTrue(any(
            evidence.field_name == "service_type_raw"
            and evidence.quote == "流动性服务商"
            for evidence in events[0].evidence
        ))

    def test_comma_joined_implicit_events_keep_pairing_and_shared_date(self) -> None:
        item = parsed(
            "SSE",
            "关于ETF做市服务的公告",
            "自2026年7月8日起，甲方证券股份有限公司为甲ETF（基金代码：111111）"
            "提供主做市服务，乙方证券股份有限公司为乙ETF（基金代码：222222）"
            "提供一般做市服务。",
        )
        events = RuleExtractor().extract(item)
        self.assertEqual(
            {
                (
                    event.security_code,
                    event.market_maker,
                    event.service_type_raw,
                    event.effective_date,
                )
                for event in events
            },
            {
                ("111111", "甲方证券股份有限公司", "主做市服务", date(2026, 7, 8)),
                ("222222", "乙方证券股份有限公司", "一般做市服务", date(2026, 7, 8)),
            },
        )

    def test_rule_does_not_borrow_date_from_later_event(self) -> None:
        item = parsed(
            "SSE",
            "关于ETF做市服务的公告",
            "甲方证券股份有限公司为甲ETF（基金代码：111111）提供主做市服务。"
            "自2026年7月9日起，乙方证券股份有限公司为乙ETF（基金代码：222222）"
            "提供主做市服务。",
        )
        events = RuleExtractor().extract(item)
        by_code = {event.security_code: event for event in events}
        self.assertIsNone(by_code["111111"].effective_date)
        self.assertEqual(by_code["222222"].effective_date, date(2026, 7, 9))


class LLMExtractionTests(unittest.TestCase):
    def test_bare_liquidity_provider_is_normalised_but_evidence_stays_original(self) -> None:
        item = parsed(
            "SZSE",
            "关于某ETF新增流动性服务商的公告",
            "自2026年6月8日起，本公司新增中信建投证券股份有限公司为某ETF（159698）流动性服务商。",
        )
        data = {
            "events": [{
                "market_maker": "中信建投证券股份有限公司",
                "security_code": "159698",
                "security_name": "某ETF",
                "effective_date": "2026-06-08",
                "action": "新增",
                # Defensive compatibility when a model ignores the prompt and
                # returns the source's bare wording.
                "service_type_raw": "流动性服务商",
                "evidence": [
                    {"field_name": "market_maker", "quote": "中信建投证券股份有限公司"},
                    {"field_name": "security_code", "quote": "159698"},
                    {"field_name": "security_name", "quote": "某ETF"},
                    {"field_name": "effective_date", "quote": "自2026年6月8日起"},
                    {"field_name": "action", "quote": "新增"},
                    {"field_name": "service_type_raw", "quote": "流动性服务商"},
                ],
            }]
        }
        with TemporaryDirectory() as tmp:
            settings = Settings.load(root_dir=tmp)
            provider = LLMProviderConfig("model-a", "https://example.test/v1", "key", "model")
            events = LLMExtractor(settings, provider)._events_from_json(item, data)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].service_type_raw, "一般流动性服务商")
        self.assertEqual(events[0].service_class, "GENERAL")
        service_evidence = [
            evidence.quote
            for evidence in events[0].evidence
            if evidence.field_name == "service_type_raw"
        ]
        self.assertIn("流动性服务商", service_evidence)
        self.assertFalse(any("service_type_raw" in warning for warning in events[0].warnings))

    def test_bare_service_evidence_does_not_match_primary_suffix(self) -> None:
        item = parsed(
            "SZSE",
            "关于某ETF主流动性服务商的公告",
            "自2026年6月8日起，本公司新增中信建投证券股份有限公司为某ETF（159698）主流动性服务商。",
        )
        data = {
            "events": [{
                "market_maker": "中信建投证券股份有限公司",
                "security_code": "159698",
                "security_name": "某ETF",
                "effective_date": "2026-06-08",
                "action": "新增",
                "service_type_raw": "流动性服务商",
                "evidence": [
                    {"field_name": "service_type_raw", "quote": "流动性服务商"},
                ],
            }]
        }
        with TemporaryDirectory() as tmp:
            settings = Settings.load(root_dir=tmp)
            provider = LLMProviderConfig("model-a", "https://example.test/v1", "key", "model")
            extractor = LLMExtractor(settings, provider)
            events = extractor._events_from_json(item, data)
        self.assertEqual(events, [])
        self.assertEqual(len(extractor.rejected_events), 1)
        self.assertIn(
            "service_type_raw无法在同一事件子句或公共列表前言中定位",
            extractor.rejected_events[0]["reasons"],
        )

    def test_designated_action_is_grounded_as_add(self) -> None:
        item = parsed(
            "SZSE",
            "关于指定旗下基金主流动性服务商的公告",
            "自2026年7月3日起，本公司指定浙商证券股份有限公司为某ETF（159715）的主流动性服务商。",
        )
        data = {
            "events": [{
                "market_maker": "浙商证券股份有限公司",
                "security_code": "159715",
                "security_name": "某ETF",
                "effective_date": "2026-07-03",
                "action": "指定",
                "service_type_raw": "主流动性服务商",
                "evidence": [
                    {"field_name": "action", "quote": "指定浙商证券股份有限公司"},
                ],
            }]
        }
        with TemporaryDirectory() as tmp:
            settings = Settings.load(root_dir=tmp)
            provider = LLMProviderConfig("model-a", "https://example.test/v1", "key", "model")
            events = LLMExtractor(settings, provider)._events_from_json(item, data)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].action, "新增")
        self.assertTrue(any(item.field_name == "action" for item in events[0].evidence))
        self.assertFalse(any("action" in warning for warning in events[0].warnings))

    def test_invalid_model_quotes_are_replaced_with_semantic_evidence(self) -> None:
        item = parsed(
            "SSE",
            "关于招商证券股份有限公司为某ETF提供主做市服务的公告",
            "根据相关规定，经招商证券股份有限公司备案申请，自2026年07月08日起，"
            "招商证券股份有限公司为某ETF（基金代码：512410）提供主做市服务。",
        )
        data = {
            "events": [{
                "market_maker": "招商证券股份有限公司",
                "security_code": "512410",
                "security_name": "某ETF",
                "effective_date": "2026-07-08",
                "action": "新增",
                "service_type_raw": "主做市服务",
                "evidence": [
                    {"field_name": "market_maker", "quote": "招商证券股份有限公司"},
                    {"field_name": "security_code", "quote": "基金代码：512410"},
                    {"field_name": "security_name", "quote": "某ETF"},
                    # These two model quotes exist in the source but do not
                    # prove the corresponding canonical field values.
                    {"field_name": "action", "quote": "备案申请"},
                    {"field_name": "effective_date", "quote": "2026年07月08日"},
                    {"field_name": "service_type_raw", "quote": "主做市服务"},
                ],
            }]
        }
        with TemporaryDirectory() as tmp:
            settings = Settings.load(root_dir=tmp)
            provider = LLMProviderConfig("model-a", "https://example.test/v1", "key", "model")
            events = LLMExtractor(settings, provider)._events_from_json(item, data)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].confidence, "HIGH")
        self.assertEqual(events[0].warnings, [])
        evidence = {item.field_name: item.quote for item in events[0].evidence}
        self.assertEqual(evidence["action"], "提供主做市服务")
        self.assertEqual(evidence["effective_date"], "自2026年07月08日起")

    def test_security_name_evidence_fallback_ignores_pdf_whitespace(self) -> None:
        item = parsed(
            "SZSE",
            "关于某基金流动性服务商的公告",
            "自 2026 年 7 月 7 日起，华夏基金管理有限公司选定国信证券股份有限公司为"
            "稀有金属 ETF 华夏（159053）的流动性服务商。",
        )
        data = {
            "events": [{
                "market_maker": "国信证券股份有限公司",
                "security_code": "159053",
                "security_name": "稀有金属ETF华夏",
                "effective_date": "2026-07-07",
                "action": "新增",
                "service_type_raw": "一般流动性服务商",
                "evidence": [],
            }]
        }
        with TemporaryDirectory() as tmp:
            settings = Settings.load(root_dir=tmp)
            provider = LLMProviderConfig("model-a", "https://example.test/v1", "key", "model")
            events = LLMExtractor(settings, provider)._events_from_json(item, data)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].confidence, "HIGH")
        self.assertFalse(any("security_name" in warning for warning in events[0].warnings))
        self.assertTrue(any(
            evidence.field_name == "security_name"
            and evidence.quote == "稀有金属 ETF 华夏"
            for evidence in events[0].evidence
        ))

    def test_termination_phrase_cannot_support_add_action(self) -> None:
        item = parsed(
            "SSE",
            "关于东海证券股份有限公司终止为某ETF提供主做市服务的公告",
            "自2026年7月8日起，东海证券股份有限公司终止为某ETF（基金代码：515970）"
            "提供主做市服务。",
        )
        data = {
            "events": [{
                "market_maker": "东海证券股份有限公司",
                "security_code": "515970",
                "security_name": "某ETF",
                "effective_date": "2026-07-08",
                "action": "新增",
                "service_type_raw": "主做市服务",
                "evidence": [
                    {"field_name": "action", "quote": "提供主做市服务"},
                ],
            }]
        }
        with TemporaryDirectory() as tmp:
            settings = Settings.load(root_dir=tmp)
            provider = LLMProviderConfig("model-a", "https://example.test/v1", "key", "model")
            extractor = LLMExtractor(settings, provider)
            events = extractor._events_from_json(item, data)

        self.assertEqual(events, [])
        self.assertEqual(len(extractor.rejected_events), 1)
        self.assertIn(
            "action=新增缺少同一事件子句中的受支持原文动作",
            extractor.rejected_events[0]["reasons"],
        )

    def test_action_evidence_cannot_be_borrowed_from_another_event(self) -> None:
        item = parsed(
            "SSE",
            "关于ETF做市服务的公告",
            "自2026年7月8日起，甲证券股份有限公司终止为甲ETF（基金代码：515970）"
            "提供主做市服务；自2026年7月8日起，乙证券股份有限公司为乙ETF"
            "（基金代码：588999）提供主做市服务。",
        )
        data = {
            "events": [{
                "market_maker": "乙证券股份有限公司",
                "security_code": "588999",
                "security_name": "乙ETF",
                "effective_date": "2026-07-08",
                "action": "终止",
                "service_type_raw": "主做市服务",
                "evidence": [
                    {"field_name": "action", "quote": "终止"},
                ],
            }]
        }
        with TemporaryDirectory() as tmp:
            settings = Settings.load(root_dir=tmp)
            provider = LLMProviderConfig("model-a", "https://example.test/v1", "key", "model")
            extractor = LLMExtractor(settings, provider)
            events = extractor._events_from_json(item, data)

        self.assertEqual(events, [])
        self.assertIn(
            "action=终止缺少同一事件子句中的受支持原文动作",
            extractor.rejected_events[0]["reasons"],
        )

    def test_effective_date_cannot_be_borrowed_from_another_event(self) -> None:
        item = parsed(
            "SSE",
            "关于ETF做市服务的公告",
            "自2026年7月8日起，甲证券股份有限公司为甲ETF（基金代码：515970）"
            "提供主做市服务；自2026年7月9日起，乙证券股份有限公司为乙ETF"
            "（基金代码：588999）提供主做市服务。",
        )
        data = {
            "events": [{
                "market_maker": "乙证券股份有限公司",
                "security_code": "588999",
                "security_name": "乙ETF",
                "effective_date": "2026-07-08",
                "action": "新增",
                "service_type_raw": "主做市服务",
                "evidence": [
                    {"field_name": "effective_date", "quote": "自2026年7月8日起"},
                    {"field_name": "action", "quote": "提供主做市服务"},
                ],
            }]
        }
        with TemporaryDirectory() as tmp:
            settings = Settings.load(root_dir=tmp)
            provider = LLMProviderConfig("model-a", "https://example.test/v1", "key", "model")
            events = LLMExtractor(settings, provider)._events_from_json(item, data)

        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0].effective_date)
        self.assertTrue(any("生效日期缺少可验证" in warning for warning in events[0].warnings))
        self.assertFalse(any(
            item.field_name == "effective_date"
            for item in events[0].evidence
        ))

    def test_unambiguous_shared_action_and_date_can_support_list_row(self) -> None:
        item = parsed(
            "SZSE",
            "关于旗下基金新增流动性服务商的公告",
            "自2026年7月8日起，本公司新增下列流动性服务商：\n"
            "1.甲ETF（159714）：甲证券股份有限公司；\n"
            "2.乙ETF（159715）：浙商证券股份有限公司。",
        )
        data = {
            "events": [{
                "market_maker": "浙商证券股份有限公司",
                "security_code": "159715",
                "security_name": "乙ETF",
                "effective_date": "2026-07-08",
                "action": "新增",
                "service_type_raw": "流动性服务商",
                "evidence": [],
            }]
        }
        with TemporaryDirectory() as tmp:
            settings = Settings.load(root_dir=tmp)
            provider = LLMProviderConfig("model-a", "https://example.test/v1", "key", "model")
            events = LLMExtractor(settings, provider)._events_from_json(item, data)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].confidence, "HIGH")
        self.assertEqual(events[0].effective_date, date(2026, 7, 8))
        self.assertTrue(any(
            item.field_name == "action" and "新增" in item.quote
            for item in events[0].evidence
        ))
        self.assertTrue(any(
            item.field_name == "effective_date" and item.quote == "自2026年7月8日起"
            for item in events[0].evidence
        ))

    def test_later_event_cannot_donate_action_to_earlier_event(self) -> None:
        item = parsed(
            "SSE",
            "关于ETF做市服务的公告",
            "甲证券股份有限公司系甲ETF（基金代码：111111）的主做市服务商。"
            "自2026年7月9日起，新增乙证券股份有限公司为乙ETF（基金代码：222222）"
            "提供主做市服务。",
        )
        data = {
            "events": [{
                "market_maker": "甲证券股份有限公司",
                "security_code": "111111",
                "security_name": "甲ETF",
                "effective_date": None,
                "action": "新增",
                "service_type_raw": "主做市服务",
                "evidence": [],
            }]
        }
        with TemporaryDirectory() as tmp:
            settings = Settings.load(root_dir=tmp)
            provider = LLMProviderConfig("model-a", "https://example.test/v1", "key", "model")
            extractor = LLMExtractor(settings, provider)
            events = extractor._events_from_json(item, data)

        self.assertEqual(events, [])
        self.assertIn(
            "action=新增缺少同一事件子句中的受支持原文动作",
            extractor.rejected_events[0]["reasons"],
        )

    def test_later_event_cannot_donate_its_only_date(self) -> None:
        item = parsed(
            "SSE",
            "关于ETF做市服务的公告",
            "甲证券股份有限公司为甲ETF（基金代码：111111）提供主做市服务。"
            "自2026年7月9日起，乙证券股份有限公司为乙ETF（基金代码：222222）"
            "提供主做市服务。",
        )
        data = {
            "events": [{
                "market_maker": "甲证券股份有限公司",
                "security_code": "111111",
                "security_name": "甲ETF",
                "effective_date": "2026-07-09",
                "action": "新增",
                "service_type_raw": "主做市服务",
                "evidence": [],
            }]
        }
        with TemporaryDirectory() as tmp:
            settings = Settings.load(root_dir=tmp)
            provider = LLMProviderConfig("model-a", "https://example.test/v1", "key", "model")
            events = LLMExtractor(settings, provider)._events_from_json(item, data)

        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0].effective_date)
        self.assertTrue(any("生效日期缺少可验证" in warning for warning in events[0].warnings))

    def test_comma_joined_implicit_events_reject_crossed_maker_and_code(self) -> None:
        item = parsed(
            "SSE",
            "关于ETF做市服务的公告",
            "自2026年7月8日起，甲证券股份有限公司为甲ETF（基金代码：111111）"
            "提供主做市服务，乙证券股份有限公司为乙ETF（基金代码：222222）"
            "提供一般做市服务。",
        )
        data = {
            "events": [{
                "market_maker": "乙证券股份有限公司",
                "security_code": "111111",
                "security_name": "甲ETF",
                "effective_date": "2026-07-08",
                "action": "新增",
                "service_type_raw": "主做市服务",
                "evidence": [],
            }]
        }
        with TemporaryDirectory() as tmp:
            settings = Settings.load(root_dir=tmp)
            provider = LLMProviderConfig("model-a", "https://example.test/v1", "key", "model")
            extractor = LLMExtractor(settings, provider)
            events = extractor._events_from_json(item, data)

        self.assertEqual(events, [])
        self.assertIn(
            "market_maker与security_code无法在同一事件子句中定位",
            extractor.rejected_events[0]["reasons"],
        )

    def test_one_maker_can_still_own_multiple_comma_joined_funds(self) -> None:
        item = parsed(
            "SSE",
            "关于ETF做市服务的公告",
            "自2026年7月8日起，甲证券股份有限公司为甲ETF（基金代码：111111）、"
            "乙ETF（基金代码：222222）提供主做市服务。",
        )
        data = {
            "events": [{
                "market_maker": "甲证券股份有限公司",
                "security_code": "222222",
                "security_name": "乙ETF",
                "effective_date": "2026-07-08",
                "action": "新增",
                "service_type_raw": "主做市服务",
                "evidence": [],
            }]
        }
        with TemporaryDirectory() as tmp:
            settings = Settings.load(root_dir=tmp)
            provider = LLMProviderConfig("model-a", "https://example.test/v1", "key", "model")
            events = LLMExtractor(settings, provider)._events_from_json(item, data)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].market_maker, "甲证券股份有限公司")
        self.assertEqual(events[0].security_code, "222222")

    def test_service_tier_cannot_be_borrowed_from_another_event(self) -> None:
        item = parsed(
            "SSE",
            "关于ETF做市服务的公告",
            "自2026年7月8日起，甲证券股份有限公司为甲ETF（基金代码：111111）"
            "提供主做市服务，乙证券股份有限公司为乙ETF（基金代码：222222）"
            "提供一般做市服务。",
        )
        data = {
            "events": [{
                "market_maker": "乙证券股份有限公司",
                "security_code": "222222",
                "security_name": "乙ETF",
                "effective_date": "2026-07-08",
                "action": "新增",
                "service_type_raw": "主做市服务",
                "evidence": [],
            }]
        }
        with TemporaryDirectory() as tmp:
            settings = Settings.load(root_dir=tmp)
            provider = LLMProviderConfig("model-a", "https://example.test/v1", "key", "model")
            extractor = LLMExtractor(settings, provider)
            events = extractor._events_from_json(item, data)

        self.assertEqual(events, [])
        self.assertIn(
            "service_type_raw无法在同一事件子句或公共列表前言中定位",
            extractor.rejected_events[0]["reasons"],
        )

    def test_continuing_service_does_not_prove_add_action(self) -> None:
        item = parsed(
            "SSE",
            "关于ETF做市服务的公告",
            "自2026年7月8日起，甲证券股份有限公司继续为甲ETF（基金代码：111111）"
            "提供主做市服务。",
        )
        data = {
            "events": [{
                "market_maker": "甲证券股份有限公司",
                "security_code": "111111",
                "security_name": "甲ETF",
                "effective_date": "2026-07-08",
                "action": "新增",
                "service_type_raw": "主做市服务",
                "evidence": [],
            }]
        }
        with TemporaryDirectory() as tmp:
            settings = Settings.load(root_dir=tmp)
            provider = LLMProviderConfig("model-a", "https://example.test/v1", "key", "model")
            extractor = LLMExtractor(settings, provider)
            events = extractor._events_from_json(item, data)

        self.assertEqual(events, [])
        self.assertIn(
            "action=新增缺少同一事件子句中的受支持原文动作",
            extractor.rejected_events[0]["reasons"],
        )

    def test_negated_termination_does_not_override_explicit_add(self) -> None:
        item = parsed(
            "SSE",
            "关于ETF做市服务的公告",
            "本次不涉及终止，自2026年7月8日起，本公司新增甲证券股份有限公司为"
            "甲ETF（基金代码：111111）提供主做市服务。",
        )
        data = {
            "events": [{
                "market_maker": "甲证券股份有限公司",
                "security_code": "111111",
                "security_name": "甲ETF",
                "effective_date": "2026-07-08",
                "action": "新增",
                "service_type_raw": "主做市服务",
                "evidence": [],
            }]
        }
        with TemporaryDirectory() as tmp:
            settings = Settings.load(root_dir=tmp)
            provider = LLMProviderConfig("model-a", "https://example.test/v1", "key", "model")
            events = LLMExtractor(settings, provider)._events_from_json(item, data)

        self.assertEqual(len(events), 1)
        action_evidence = next(
            item.quote for item in events[0].evidence if item.field_name == "action"
        )
        self.assertEqual(action_evidence, "新增")

    def test_openai_compatible_json_call(self) -> None:
        item = parsed(
            "SSE",
            "关于中信证券股份有限公司为某ETF提供主做市服务的公告",
            "自2026年7月13日起，中信证券股份有限公司为某ETF（基金代码：589130）提供主做市服务。",
        )
        model_payload = {
            "events": [{
                "market_maker": "中信证券股份有限公司",
                "security_code": "589130",
                "security_name": "某ETF",
                "effective_date": "2026-07-13",
                "action": "新增",
                "service_type_raw": "主做市服务",
                "evidence": [
                    {"field_name": "market_maker", "quote": "中信证券股份有限公司"},
                    {"field_name": "security_code", "quote": "基金代码：589130"},
                    {"field_name": "effective_date", "quote": "自2026年7月13日起"},
                    {"field_name": "action", "quote": "提供主做市服务"},
                    {"field_name": "service_type_raw", "quote": "主做市服务"},
                ],
            }]
        }
        response = {"choices": [{"message": {"content": json.dumps(model_payload, ensure_ascii=False)}}]}

        request_payload = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                nonlocal request_payload
                request_payload = json.loads(
                    self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8")
                )
                body = json.dumps(response, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with TemporaryDirectory() as tmp:
                settings = Settings.load(root_dir=tmp)
                provider = LLMProviderConfig(
                    name="test-model",
                    api_base=f"http://127.0.0.1:{server.server_port}/v1",
                    api_key="test-key",
                    model="test-model",
                )
                settings.llm_providers = [provider]
                extractor = LLMExtractor(settings)
                events = extractor.extract(item)
                self.assertTrue(extractor.succeeded)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].security_code, "589130")
                self.assertEqual(events[0].service_type_raw, "主做市服务")
                prompt = request_payload["messages"][1]["content"]
                self.assertIn(
                    "深交所原文仅写‘流动性服务商’时输出‘一般流动性服务商’",
                    prompt,
                )
                self.assertIn(
                    "‘备案申请’不能作为动作证据",
                    prompt,
                )
                self.assertIn("裸日期、公告发布日期或落款日期不能作为生效证据", prompt)
        finally:
            server.shutdown()
            server.server_close()

    def test_all_configured_models_are_called_and_unanimous_result_is_high(self) -> None:
        item = parsed(
            "SSE",
            "关于中信证券股份有限公司为某ETF提供主做市服务的公告",
            "自2026年7月13日起，中信证券股份有限公司为某ETF（基金代码：589130）提供主做市服务。",
        )
        model_payload = {
            "events": [{
                "market_maker": "中信证券股份有限公司",
                "security_code": "589130",
                "security_name": "某ETF",
                "effective_date": "2026-07-13",
                "action": "新增",
                "service_type_raw": "主做市服务",
                "evidence": [
                    {"field_name": "action", "quote": "提供主做市服务"},
                ],
            }]
        }
        response = {"choices": [{"message": {"content": json.dumps(model_payload, ensure_ascii=False)}}]}
        request_count = 0
        active_requests = 0
        max_active_requests = 0
        lock = threading.Lock()
        both_requests_arrived = threading.Barrier(2)

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                nonlocal request_count, active_requests, max_active_requests
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                with lock:
                    request_count += 1
                    active_requests += 1
                    max_active_requests = max(max_active_requests, active_requests)
                try:
                    both_requests_arrived.wait(timeout=2)
                except threading.BrokenBarrierError:
                    pass
                body = json.dumps(response, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                with lock:
                    active_requests -= 1

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with TemporaryDirectory() as tmp:
                settings = Settings.load(root_dir=tmp)
                api_base = f"http://127.0.0.1:{server.server_port}/v1"
                settings.llm_providers = [
                    LLMProviderConfig("model-a", api_base, "key-a", "model-a"),
                    LLMProviderConfig("model-b", api_base, "key-b", "model-b"),
                ]
                events = extract_events(item, settings)
                self.assertEqual(request_count, 2)
                self.assertGreaterEqual(max_active_requests, 2)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].confidence, "HIGH")
                self.assertEqual(events[0].review_status, "AUTO_ACCEPTED")
                self.assertEqual(events[0].extractor, "CONSENSUS[RULE,model-a,model-b]")
        finally:
            server.shutdown()
            server.server_close()

    def test_mismatched_effective_date_evidence_is_rejected(self) -> None:
        item = parsed(
            "SSE",
            "关于中信证券股份有限公司为某ETF提供主做市服务的公告",
            "自2026年7月13日起，中信证券股份有限公司为某ETF（基金代码：589130）提供主做市服务。",
        )
        data = {
            "events": [{
                "market_maker": "中信证券股份有限公司",
                "security_code": "589130",
                "security_name": "某ETF",
                "effective_date": "2026-07-14",
                "action": "新增",
                "service_type_raw": "主做市服务",
                "evidence": [
                    {"field_name": "market_maker", "quote": "中信证券股份有限公司"},
                    {"field_name": "security_code", "quote": "基金代码：589130"},
                    {"field_name": "security_name", "quote": "某ETF"},
                    {"field_name": "effective_date", "quote": "自2026年7月13日起"},
                    {"field_name": "action", "quote": "提供主做市服务"},
                    {"field_name": "service_type_raw", "quote": "主做市服务"},
                ],
            }]
        }
        with TemporaryDirectory() as tmp:
            settings = Settings.load(root_dir=tmp)
            provider = LLMProviderConfig("model-a", "https://example.test/v1", "key", "model")
            events = LLMExtractor(settings, provider)._events_from_json(item, data)
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0].effective_date)
        self.assertEqual(events[0].confidence, "MEDIUM")
        self.assertFalse(any(item.field_name == "effective_date" for item in events[0].evidence))
        self.assertTrue(any("生效日期缺少可验证" in warning for warning in events[0].warnings))

    def test_footer_date_cannot_ground_effective_date(self) -> None:
        item = parsed(
            "SSE",
            "关于中信证券股份有限公司为某ETF提供主做市服务的公告",
            "自2026年7月13日起，中信证券股份有限公司为某ETF（基金代码：589130）提供主做市服务。\n上海证券交易所 2026年7月14日",
        )
        data = {
            "events": [{
                "market_maker": "中信证券股份有限公司",
                "security_code": "589130",
                "security_name": "某ETF",
                "effective_date": "2026-07-14",
                "action": "新增",
                "service_type_raw": "主做市服务",
                "evidence": [
                    {"field_name": "effective_date", "quote": "2026年7月14日"},
                    {"field_name": "action", "quote": "提供主做市服务"},
                ],
            }]
        }
        with TemporaryDirectory() as tmp:
            settings = Settings.load(root_dir=tmp)
            provider = LLMProviderConfig("model-a", "https://example.test/v1", "key", "model")
            events = LLMExtractor(settings, provider)._events_from_json(item, data)
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0].effective_date)
        self.assertFalse(any(item.field_name == "effective_date" for item in events[0].evidence))


class MultiModelConsensusTests(unittest.TestCase):
    def setUp(self) -> None:
        item = parsed(
            "SSE",
            "关于中信证券股份有限公司为某ETF提供主做市服务的公告",
            "自2026年7月13日起，中信证券股份有限公司为某ETF（基金代码：589130）提供主做市服务。",
        )
        self.rule = RuleExtractor().extract(item)[0]

    def model_event(self, name: str, **changes):
        event = replace(self.rule, extractor=f"LLM:{name}", **changes)
        action_quote = {
            "新增": "新增",
            "终止": "终止",
            "调整": "调整",
        }.get(event.action, event.action)
        evidence = [
            Evidence("market_maker", event.market_maker),
            Evidence("security_code", f"基金代码：{event.security_code}"),
            Evidence("action", action_quote),
            Evidence("service_type_raw", event.service_type_raw),
        ]
        if event.security_name:
            evidence.append(Evidence("security_name", event.security_name))
        if event.effective_date:
            evidence.append(
                Evidence(
                    "effective_date",
                    f"自{event.effective_date.year}年{event.effective_date.month}月{event.effective_date.day}日起",
                )
            )
        return replace(event, evidence=evidence)

    def test_two_models_can_form_majority_against_rule(self) -> None:
        result = reconcile_model_results(
            [self.rule],
            [
                LLMExtractionResult("model-a", True, [self.model_event("model-a", action="终止")]),
                LLMExtractionResult("model-b", True, [self.model_event("model-b", action="终止")]),
            ],
        )
        self.assertEqual(result[0].action, "终止")
        self.assertEqual(result[0].confidence, "MEDIUM")
        self.assertEqual(result[0].review_status, "NEEDS_REVIEW")
        self.assertTrue(any("字段action按严格多数" in warning for warning in result[0].warnings))

    def test_bare_and_normalised_service_values_share_one_vote(self) -> None:
        rule = replace(
            self.rule,
            service_type_raw="一般流动性服务商",
            service_class="GENERAL",
        )
        model = self.model_event("model-a")
        model = replace(
            model,
            service_type_raw="流动性服务商",
            service_class="UNSPECIFIED",
            evidence=[
                replace(evidence, quote="流动性服务商")
                if evidence.field_name == "service_type_raw"
                else evidence
                for evidence in model.evidence
            ],
        )
        audit: dict = {}
        result = reconcile_model_results(
            [rule],
            [LLMExtractionResult("model-a", True, [model])],
            audit_detail=audit,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].service_type_raw, "一般流动性服务商")
        self.assertEqual(result[0].service_class, "GENERAL")
        self.assertEqual(result[0].confidence, "HIGH")
        service_vote = next(
            item for item in audit["field_votes"]
            if item["field_name"] == "service_type_raw"
        )
        self.assertEqual(service_vote["decision"], "strict_majority")
        self.assertEqual(service_vote["selected_value"], "一般流动性服务商")
        self.assertEqual(
            {item["vote_key"] for item in service_vote["observations"]},
            {"一般流动性服务商"},
        )

    def test_tie_keeps_rule_value_and_is_low(self) -> None:
        audit: dict = {}
        result = reconcile_model_results(
            [self.rule],
            [LLMExtractionResult("model-a", True, [self.model_event("model-a", action="终止")])],
            audit_detail=audit,
        )
        self.assertEqual(result[0].action, "新增")
        self.assertEqual(result[0].confidence, "LOW")
        self.assertTrue(any("字段action无严格多数" in warning for warning in result[0].warnings))
        action_vote = next(
            item for item in audit["field_votes"] if item["field_name"] == "action"
        )
        self.assertEqual(action_vote["decision"], "no_strict_majority_keep_base")
        self.assertEqual(action_vote["selected_source"], "RULE")
        self.assertEqual(action_vote["selected_value"], "新增")
        self.assertEqual(
            {item["source"]: item["value"] for item in action_vote["observations"]},
            {"RULE": "新增", "model-a": "终止"},
        )

    def test_failed_model_abstains_but_caps_confidence(self) -> None:
        result = reconcile_model_results(
            [self.rule],
            [
                LLMExtractionResult("model-a", True, [self.model_event("model-a")]),
                LLMExtractionResult("model-b", False, [], "大模型接口[model-b]抽取失败：HTTP 500"),
            ],
        )
        self.assertEqual(result[0].confidence, "MEDIUM")
        self.assertIn("大模型接口[model-b]抽取失败：HTTP 500", result[0].warnings)

    def test_successful_empty_result_is_event_disagreement(self) -> None:
        result = reconcile_model_results(
            [self.rule],
            [
                LLMExtractionResult("model-a", True, [self.model_event("model-a")]),
                LLMExtractionResult("model-b", True, []),
            ],
        )
        self.assertEqual(result[0].confidence, "MEDIUM")
        self.assertTrue(any("部分抽取器未返回该事件" in warning for warning in result[0].warnings))

    def test_two_models_can_form_majority_for_market_maker(self) -> None:
        other_maker = "国泰海通证券股份有限公司"
        result = reconcile_model_results(
            [self.rule],
            [
                LLMExtractionResult(
                    "model-a", True, [self.model_event("model-a", market_maker=other_maker)]
                ),
                LLMExtractionResult(
                    "model-b", True, [self.model_event("model-b", market_maker=other_maker)]
                ),
            ],
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].market_maker, other_maker)
        self.assertEqual(result[0].confidence, "MEDIUM")

    def test_same_maker_code_with_two_actions_is_not_dropped(self) -> None:
        rule_end = replace(self.rule, action="终止")
        result = reconcile_model_results(
            [self.rule, rule_end],
            [LLMExtractionResult(
                "model-a",
                True,
                [self.model_event("model-a"), self.model_event("model-a", action="终止")],
            )],
        )
        self.assertEqual(len(result), 2)
        self.assertEqual({event.action for event in result}, {"新增", "终止"})

    def test_same_code_multiple_makers_match_when_model_order_changes(self) -> None:
        other_maker = "国泰海通证券股份有限公司"
        other_rule = replace(self.rule, market_maker=other_maker)
        result = reconcile_model_results(
            [self.rule, other_rule],
            [LLMExtractionResult(
                "model-a",
                True,
                [
                    self.model_event("model-a", market_maker=other_maker),
                    self.model_event("model-a"),
                ],
            )],
        )
        self.assertEqual(len(result), 2)
        self.assertEqual(
            {event.market_maker for event in result},
            {"中信证券股份有限公司", other_maker},
        )
        self.assertTrue(all(event.confidence == "HIGH" for event in result))

    def test_semantically_wrong_model_evidence_abstains(self) -> None:
        bad_model = replace(
            self.model_event("model-a", action="终止"),
            evidence=self.rule.evidence,
        )
        result = reconcile_model_results(
            [self.rule],
            [LLMExtractionResult("model-a", True, [bad_model])],
        )
        self.assertEqual(result[0].action, "新增")
        self.assertEqual(result[0].confidence, "LOW")
        self.assertTrue(any("缺少字段或有效证据action" in warning for warning in result[0].warnings))


if __name__ == "__main__":
    unittest.main()
