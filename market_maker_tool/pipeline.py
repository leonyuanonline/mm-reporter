from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .config import Settings
from .document_parser import parse_and_store
from .extraction import extract_events_with_audit, is_candidate
from .models import AnnouncementCandidate
from .reporting import generate_reports
from .sources import SSESource, SZSESource
from .storage import Database


@dataclass(slots=True)
class RunResult:
    target_date: date
    status: str
    stats: dict[str, Any]
    report_paths: dict[str, Path] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


class DailyPipeline:
    def __init__(self, settings: Settings, logger: logging.Logger):
        self.settings = settings
        self.logger = logger
        self.db = Database(settings.db_path)
        self.sources = [SSESource(settings), SZSESource(settings)]

    def run(self, target_date: date, mode: str = "daily") -> RunResult:
        run_id = self.db.start_run(target_date, mode)
        try:
            return self._run_started(target_date, mode, run_id)
        except Exception as exc:
            failure_stats = {"status": "FAILED", "unhandled_exception": type(exc).__name__}
            try:
                self.db.finish_run(run_id, "FAILED", failure_stats, str(exc))
            except Exception:
                self.logger.exception("无法更新失败运行状态 run_id=%s", run_id)
            raise

    def _run_started(self, target_date: date, mode: str, run_id: int) -> RunResult:
        stats: dict[str, Any] = {
            "source_candidate_counts": {},
            "candidate_count": 0,
            "fetched_count": 0,
            "parsed_count": 0,
            "event_count_before_dedup": 0,
            "event_count": 0,
            "needs_review_count": 0,
            "document_failure_count": 0,
            "extraction_failure_count": 0,
            "audit_record_count": 0,
            "source_failure_count": 0,
        }
        errors: list[str] = []
        candidates: list[tuple[Any, AnnouncementCandidate]] = []

        self.logger.info("开始处理公告发布日期 %s", target_date.isoformat())
        incomplete_providers = [
            provider
            for provider in self.settings.llm_providers
            if not provider.available
        ]
        if incomplete_providers:
            self.logger.warning(
                "以下已启用的大模型接口配置不完整，未参与校验: %s",
                "；".join(
                    f"{provider.name}(缺少{','.join(provider.missing_fields)})"
                    for provider in incomplete_providers
                ),
            )
        if not self.settings.llm_available:
            self.logger.warning("配置文件中没有可用的大模型接口，本次仅执行规则抽取")
        elif self.settings.llm_available:
            self.logger.info(
                "启用 %d 个大模型接口并行校验: %s",
                len(self.settings.available_llm_providers),
                ", ".join(provider.name for provider in self.settings.available_llm_providers),
            )

        for source in self.sources:
            source_name = type(source).__name__
            try:
                items = source.list_for_date(target_date)
                stats["source_candidate_counts"][source_name] = len(items)
                candidates.extend((source, item) for item in items)
                self.logger.info("%s 获取候选公告 %d 条", source_name, len(items))
            except Exception as exc:
                message = f"{source_name} 清单采集失败: {exc}"
                errors.append(message)
                stats["source_failure_count"] += 1
                self.logger.error(message)

        candidates = _deduplicate_candidates(candidates)
        stats["candidate_count"] = len(candidates)
        for source, candidate in candidates:
            try:
                fetched = source.fetch(candidate)
                stats["fetched_count"] += 1
                parsed = parse_and_store(
                    fetched.candidate,
                    fetched.raw_bytes,
                    fetched.content_type,
                    self.settings,
                )
                stats["parsed_count"] += 1
                self.db.upsert_announcement(parsed)
                if not is_candidate(candidate, parsed.text):
                    self.logger.debug("过滤非目标公告: %s", candidate.title)
                    continue
                extraction = extract_events_with_audit(
                    parsed,
                    self.settings,
                    run_id=run_id,
                )
                events = extraction.events
                for audit in extraction.audits:
                    self.db.write_extraction_audit(audit)
                    stats["audit_record_count"] += 1
                stats["event_count_before_dedup"] += len(events)
                if not events:
                    stats["extraction_failure_count"] += 1
                    attempts = "；".join(
                        f"{audit.extractor}[{audit.status}]"
                        f" raw={len(audit.raw_events)}"
                        f" validated={len(audit.validated_events)}"
                        f" rejected={len(audit.rejected_events)}"
                        for audit in extraction.audits
                    )
                    message = (
                        f"候选公告未抽取到事件: {candidate.title} "
                        f"{candidate.canonical_url}；抽取审计: {attempts}"
                    )
                    errors.append(message)
                    self.logger.warning(message)
                else:
                    self.db.replace_source_events(
                        candidate.exchange,
                        candidate.external_id,
                        events,
                    )
                for event in events:
                    if event.review_status == "NEEDS_REVIEW" or event.confidence == "LOW":
                        self.logger.warning(
                            "需复核: %s %s %s [%s] %s",
                            event.security_code,
                            event.market_maker,
                            event.action,
                            event.confidence,
                            "; ".join(event.warnings),
                        )
            except Exception as exc:
                stats["document_failure_count"] += 1
                message = f"公告处理失败 {candidate.external_id} {candidate.canonical_url}: {exc}"
                errors.append(message)
                self.logger.error(message)

        rows = self.db.events_for_date(target_date)
        stats["event_count"] = len(rows)
        stats["needs_review_count"] = sum(
            1 for row in rows if row.get("review_status") == "NEEDS_REVIEW" or row.get("confidence") == "LOW"
        )
        report_paths = generate_reports(target_date, rows, self.settings.report_dir)

        if stats["source_failure_count"] == len(self.sources):
            status = "FAILED"
        elif (
            stats["source_failure_count"]
            or stats["document_failure_count"]
            or stats["extraction_failure_count"]
        ):
            status = "PARTIAL"
        else:
            status = "SUCCESS"
        stats["status"] = status
        self.db.finish_run(run_id, status, stats, "\n".join(errors) if errors else None)
        self.logger.info(
            "完成 %s: status=%s, events=%d, needs_review=%d",
            target_date.isoformat(), status, stats["event_count"], stats["needs_review_count"],
        )
        return RunResult(target_date, status, stats, report_paths, errors)


def _deduplicate_candidates(
    candidates: Iterable[tuple[Any, AnnouncementCandidate]],
) -> list[tuple[Any, AnnouncementCandidate]]:
    result: list[tuple[Any, AnnouncementCandidate]] = []
    seen: set[tuple[str, str]] = set()
    for source, candidate in candidates:
        key = (candidate.exchange, candidate.external_id)
        if key not in seen:
            seen.add(key)
            result.append((source, candidate))
    return result
