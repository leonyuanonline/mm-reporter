"""Human-readable and JSON views for persisted extraction audit records.

The database intentionally returns audit data as ordinary dictionaries.  This
module keeps presentation concerns out of ``storage.py`` and accepts either
flat rows (one announcement/extractor per row) or already grouped records.  It
also performs a final, defensive credential redaction before anything reaches
the terminal or JSON output.
"""

from __future__ import annotations

import json
import re
import sys
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, TextIO


_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:api_?key|access_?token|refresh_?token|authorization|secret_?key|client_?secret|secret|password)(?:$|_)",
    re.I,
)
_CREDENTIAL_TEXT = re.compile(
    r"(?i)(\b(?:api[_-]?key|authorization|access[_-]?token|refresh[_-]?token)\b\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
_BEARER_TEXT = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")

_ANNOUNCEMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "exchange": ("exchange", "announcement_exchange"),
    "external_id": (
        "external_id",
        "announcement_external_id",
        "official_announcement_id",
    ),
    "title": ("title", "announcement_title"),
    "published_date": ("published_date", "announcement_date", "target_date"),
    "canonical_url": ("canonical_url", "source_url", "announcement_url"),
    "publisher": ("publisher", "announcement_publisher"),
    "raw_path": ("raw_path", "announcement_raw_path"),
    "text_path": ("text_path", "announcement_text_path"),
    "parser": ("parser", "document_parser"),
}

_ATTEMPT_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "audit_id": ("audit_id",),
    "run_id": ("run_id",),
    "extractor": ("extractor_name", "provider_name", "audit_extractor", "extractor"),
    "stage": ("stage", "audit_stage"),
    "status": (
        "attempt_status",
        "extraction_status",
        "call_status",
        "provider_status",
        "status",
    ),
    "succeeded": ("succeeded",),
    "model": ("model", "model_name"),
    "started_at": ("attempted_at", "started_at", "created_at"),
    "finished_at": ("finished_at", "completed_at"),
    "latency_ms": ("latency_ms", "duration_ms"),
}

_RAW_EVENT_ALIASES = (
    "raw_events_json",
    "raw_events",
    "raw_output_json",
    "raw_output",
    "response_events_json",
)
_VALIDATED_EVENT_ALIASES = (
    "validated_events_json",
    "validated_events",
    "accepted_events_json",
    "accepted_events",
    "events_json",
)
_REJECTED_EVENT_ALIASES = (
    "rejected_events_json",
    "rejected_events",
    "rejections_json",
    "rejections",
)
_ATTEMPT_WARNING_ALIASES = (
    "attempt_warnings_json",
    "extractor_warnings_json",
    "warnings_json",
    "warnings",
    "warning",
    "error_message",
    "error",
)
_RAW_RESPONSE_ALIASES = ("raw_response_json", "raw_response", "response_json")
_REJECTION_REASON_ALIASES = (
    "rejection_reasons_json",
    "rejection_reasons",
    "reject_reasons_json",
)
_FINAL_EVENT_ALIASES = (
    "final_events_json",
    "final_events",
    "consensus_events_json",
    "consensus_events",
)
_FIELD_VOTE_ALIASES = (
    "field_votes_json",
    "field_votes",
    "votes_json",
    "votes",
    "consensus_json",
    "reconciliation_json",
)

_EVENT_FIELDS: tuple[tuple[str, str], ...] = (
    ("market_maker", "做市商"),
    ("security_code", "证券代码"),
    ("security_name", "证券名称"),
    ("effective_date", "生效日期"),
    ("action", "动作"),
    ("service_type_raw", "服务类型原文"),
    ("service_class", "内部分类"),
    ("confidence", "置信度"),
    ("review_status", "复核状态"),
)


def build_audit_payload(
    rows: Iterable[Mapping[str, Any]],
    target_date: date | str,
    announcement_id: str | None = None,
    *,
    latest_only: bool = True,
) -> dict[str, Any]:
    """Normalize flat database rows into a stable, redacted audit document."""

    source_rows = list(rows)
    if latest_only:
        source_rows = _latest_attempt_batch_per_announcement(source_rows)
    grouped: "OrderedDict[tuple[str, str], dict[str, Any]]" = OrderedDict()
    for index, source_row in enumerate(source_rows):
        row = _redact_sensitive(dict(source_row))
        nested_announcement = _as_mapping(
            _first_value(row, ("announcement", "announcement_json"))
        )
        announcement = _announcement_from_row(row, nested_announcement)
        identity = (
            str(announcement.get("exchange") or ""),
            str(
                announcement.get("external_id")
                or _first_value(row, ("announcement_id",))
                or f"row-{index + 1}"
            ),
        )
        group = grouped.setdefault(
            identity,
            {
                "announcement": announcement,
                "attempts": [],
                "field_votes": [],
                "final_events": [],
            },
        )
        _merge_nonempty(group["announcement"], announcement)

        nested_attempts = _decoded(_first_value(row, ("attempts", "attempts_json")))
        if isinstance(nested_attempts, list):
            for attempt in nested_attempts:
                if isinstance(attempt, Mapping):
                    _add_attempt(group, _normalise_attempt(attempt))
        elif isinstance(nested_attempts, Mapping):
            _add_attempt(group, _normalise_attempt(nested_attempts))
        elif _looks_like_attempt_row(row):
            _add_attempt(group, _normalise_attempt(row))

        for value in _values_for_aliases(row, _FIELD_VOTE_ALIASES):
            _extend_unique(group["field_votes"], _as_items(value))
        for value in _values_for_aliases(row, _FINAL_EVENT_ALIASES):
            _extend_unique_events(group["final_events"], _as_items(value, unwrap_events=True))

    payload = {
        "schema_version": 1,
        "target_date": _json_scalar(target_date),
        "announcement_id": announcement_id,
        "latest_only": latest_only,
        "announcement_count": len(grouped),
        "announcements": list(grouped.values()),
    }
    return _redact_sensitive(payload)


def write_audit_report(
    rows: Iterable[Mapping[str, Any]],
    target_date: date | str,
    announcement_id: str | None = None,
    *,
    as_json: bool = False,
    latest_only: bool = True,
    stream: TextIO | None = None,
) -> dict[str, Any]:
    """Write an audit report and return its normalized payload."""

    output = stream or sys.stdout
    payload = build_audit_payload(
        rows,
        target_date,
        announcement_id,
        latest_only=latest_only,
    )
    if as_json:
        json.dump(payload, output, ensure_ascii=False, indent=2, default=_json_scalar)
        output.write("\n")
    else:
        _write_human(payload, output)
    return payload


def _write_human(payload: Mapping[str, Any], stream: TextIO) -> None:
    stream.write(f"审计日期: {payload['target_date']}\n")
    if payload.get("announcement_id"):
        stream.write(f"公告ID筛选: {payload['announcement_id']}\n")
    stream.write(f"公告数: {payload['announcement_count']}\n")
    announcements = payload.get("announcements") or []
    if not announcements:
        stream.write("未找到符合条件的抽取审计记录。\n")
        return

    for number, item in enumerate(announcements, start=1):
        announcement = item.get("announcement") or {}
        stream.write("\n" + "=" * 88 + "\n")
        stream.write(
            f"[{number}] {announcement.get('exchange') or '-'} "
            f"{announcement.get('external_id') or '-'}\n"
        )
        for key, label in (
            ("title", "标题"),
            ("published_date", "公告日期"),
            ("canonical_url", "公告URL"),
            ("publisher", "发布主体"),
            ("raw_path", "原始文件"),
            ("text_path", "解析文本"),
            ("parser", "解析器"),
        ):
            if announcement.get(key) not in (None, ""):
                stream.write(f"{label}: {announcement[key]}\n")
        _write_warning_list("解析警告", announcement.get("parse_warnings"), stream, "  ")

        attempts = item.get("attempts") or []
        stream.write(f"\n抽取器调用 ({len(attempts)}):\n")
        if not attempts:
            stream.write("  （无已保存的逐抽取器快照）\n")
        for attempt_index, attempt in enumerate(attempts, start=1):
            extractor = attempt.get("extractor") or "UNKNOWN"
            status = attempt.get("status") or "UNKNOWN"
            model = f" | 模型 {attempt['model']}" if attempt.get("model") else ""
            stage = f" | 阶段 {attempt['stage']}" if attempt.get("stage") else ""
            succeeded = (
                f" | succeeded={str(bool(attempt['succeeded'])).lower()}"
                if "succeeded" in attempt
                else ""
            )
            stream.write(
                f"  [{attempt_index}] {extractor} | 状态 {status}{stage}{model}{succeeded}\n"
            )
            metadata = []
            for key, label in (
                ("audit_id", "audit_id"),
                ("run_id", "run_id"),
                ("started_at", "开始"),
                ("finished_at", "结束"),
                ("latency_ms", "耗时ms"),
            ):
                if attempt.get(key) not in (None, ""):
                    metadata.append(f"{label}={attempt[key]}")
            if metadata:
                stream.write("      " + " | ".join(metadata) + "\n")
            _write_warning_list("调用/抽取警告", attempt.get("warnings"), stream, "      ")
            if _is_consensus_attempt(attempt):
                stream.write("      共识原始明细已归并到下方“逐字段投票/对账”和“最终事件”。\n")
            else:
                if attempt.get("raw_response") not in (None, "", {}):
                    stream.write("      原始接口/规则响应:\n")
                    _write_json_block(attempt["raw_response"], stream, "        ")
                _write_event_collection("原始事件", attempt.get("raw_events"), stream, "      ")
                _write_event_collection("校验后事件", attempt.get("validated_events"), stream, "      ")
                _write_rejections(attempt.get("rejected_events"), stream, "      ")
                _write_warning_list(
                    "汇总拒绝原因", attempt.get("rejection_reasons"), stream, "      "
                )

        stream.write("\n逐字段投票/对账:\n")
        votes = item.get("field_votes") or []
        if votes:
            _write_json_block(votes, stream, "  ")
        else:
            stream.write("  （无已保存的结构化投票明细）\n")

        _write_event_collection(
            "最终事件",
            item.get("final_events"),
            stream,
            "",
            heading_prefix="\n",
        )


def _normalise_attempt(source: Mapping[str, Any]) -> dict[str, Any]:
    row = _redact_sensitive(dict(source))
    nested = _as_mapping(_first_value(row, ("attempt", "attempt_json", "extraction")))
    if nested:
        merged = dict(row)
        merged.update(nested)
        row = merged
    result: dict[str, Any] = {}
    for target, aliases in _ATTEMPT_FIELD_ALIASES.items():
        value = _first_value(row, aliases)
        if value not in (None, ""):
            result[target] = _decoded(value)
    result["warnings"] = _as_warning_list(_first_value(row, _ATTEMPT_WARNING_ALIASES))
    result["raw_response"] = _decoded(_first_value(row, _RAW_RESPONSE_ALIASES))
    result["raw_events"] = _first_collection(row, _RAW_EVENT_ALIASES)
    result["validated_events"] = _first_collection(row, _VALIDATED_EVENT_ALIASES)
    result["rejected_events"] = _first_collection(row, _REJECTED_EVENT_ALIASES)
    result["rejection_reasons"] = _as_warning_list(
        _first_value(row, _REJECTION_REASON_ALIASES)
    )
    return _redact_sensitive(result)


def _add_attempt(group: dict[str, Any], attempt: dict[str, Any]) -> None:
    """Append one attempt and derive consensus details when stored as a stage."""

    _append_unique(group["attempts"], attempt)
    if not _is_consensus_attempt(attempt):
        return
    _extend_unique_events(
        group["final_events"],
        _as_items(attempt.get("validated_events"), unwrap_events=True),
    )
    raw_response = attempt.get("raw_response")
    if isinstance(raw_response, Mapping):
        for alias in _FIELD_VOTE_ALIASES:
            if alias in raw_response:
                _extend_unique(group["field_votes"], _as_items(raw_response[alias]))
        # Consensus audit writers may use a shorter key inside raw_response.
        for alias in ("decisions", "field_decisions", "vote_details"):
            if alias in raw_response:
                _extend_unique(group["field_votes"], _as_items(raw_response[alias]))


def _is_consensus_attempt(attempt: Mapping[str, Any]) -> bool:
    stage = str(attempt.get("stage") or "").strip().casefold()
    extractor = str(attempt.get("extractor") or "").strip().casefold()
    return stage in {"consensus", "reconcile", "reconciliation", "final"} or (
        extractor.startswith("consensus")
    )


def _announcement_from_row(
    row: Mapping[str, Any], nested: Mapping[str, Any]
) -> dict[str, Any]:
    combined = dict(row)
    combined.update(nested)
    announcement: dict[str, Any] = {}
    for target, aliases in _ANNOUNCEMENT_ALIASES.items():
        value = _first_value(combined, aliases)
        if value not in (None, ""):
            announcement[target] = _decoded(value)
    announcement["parse_warnings"] = _as_warning_list(
        _first_value(combined, ("parse_warnings", "parse_warnings_json"))
    )
    return announcement


def _looks_like_attempt_row(row: Mapping[str, Any]) -> bool:
    markers = {
        "audit_id",
        "extractor_name",
        "provider_name",
        "audit_extractor",
        "attempt_status",
        "extraction_status",
        *_RAW_EVENT_ALIASES,
        *_VALIDATED_EVENT_ALIASES,
        *_REJECTED_EVENT_ALIASES,
        *_RAW_RESPONSE_ALIASES,
        *_REJECTION_REASON_ALIASES,
    }
    return bool(markers.intersection(row))


def _write_event_collection(
    label: str,
    value: Any,
    stream: TextIO,
    indent: str,
    *,
    heading_prefix: str = "",
) -> None:
    events = _as_items(value, unwrap_events=True)
    stream.write(f"{heading_prefix}{indent}{label} ({len(events)}):\n")
    if not events:
        stream.write(f"{indent}  （无）\n")
        return
    for index, event in enumerate(events, start=1):
        stream.write(f"{indent}  #{index}\n")
        if not isinstance(event, Mapping):
            stream.write(f"{indent}    {_inline_json(event)}\n")
            continue
        shown: set[str] = set()
        for key, field_label in _EVENT_FIELDS:
            if key in event and event[key] not in (None, ""):
                stream.write(f"{indent}    {field_label}: {_inline_json(event[key])}\n")
                shown.add(key)
        evidence = _decoded(event.get("evidence", event.get("evidence_json")))
        if evidence:
            stream.write(f"{indent}    evidence:\n")
            for evidence_item in _as_items(evidence):
                if isinstance(evidence_item, Mapping):
                    field_name = evidence_item.get("field_name") or "?"
                    quote = evidence_item.get("quote") or ""
                    location = _evidence_location(evidence_item)
                    stream.write(
                        f"{indent}      - {field_name}: {_inline_json(quote)}{location}\n"
                    )
                else:
                    stream.write(f"{indent}      - {_inline_json(evidence_item)}\n")
        warnings = _as_warning_list(event.get("warnings", event.get("warnings_json")))
        _write_warning_list("warnings", warnings, stream, indent + "    ")
        ignored = {
            *shown,
            "evidence",
            "evidence_json",
            "warnings",
            "warnings_json",
        }
        extras = {key: item for key, item in event.items() if key not in ignored}
        if extras:
            stream.write(f"{indent}    其他字段:\n")
            _write_json_block(extras, stream, indent + "      ")


def _write_rejections(value: Any, stream: TextIO, indent: str) -> None:
    rejected = _as_items(value)
    stream.write(f"{indent}拒绝/丢弃事件 ({len(rejected)}):\n")
    if not rejected:
        stream.write(f"{indent}  （无）\n")
        return
    for index, item in enumerate(rejected, start=1):
        stream.write(f"{indent}  #{index}\n")
        if isinstance(item, Mapping):
            event = item.get("event", item.get("raw_event"))
            reasons = _as_warning_list(
                item.get("reasons", item.get("reason", item.get("warnings")))
            )
            if event is not None:
                _write_event_collection("原始事件", [event], stream, indent + "    ")
            _write_warning_list("拒绝原因", reasons, stream, indent + "    ")
            extras = {
                key: value
                for key, value in item.items()
                if key not in {"event", "raw_event", "reasons", "reason", "warnings"}
            }
            if extras:
                _write_json_block(extras, stream, indent + "    ")
        else:
            stream.write(f"{indent}    {_inline_json(item)}\n")


def _write_warning_list(label: str, value: Any, stream: TextIO, indent: str) -> None:
    warnings = _as_warning_list(value)
    if not warnings:
        return
    stream.write(f"{indent}{label}:\n")
    for warning in warnings:
        stream.write(f"{indent}  - {warning}\n")


def _write_json_block(value: Any, stream: TextIO, indent: str) -> None:
    rendered = json.dumps(
        _redact_sensitive(value), ensure_ascii=False, indent=2, default=_json_scalar
    )
    for line in rendered.splitlines():
        stream.write(indent + line + "\n")


def _first_collection(row: Mapping[str, Any], aliases: Sequence[str]) -> list[Any]:
    value = _first_value(row, aliases)
    return _as_items(value, unwrap_events=True)


def _values_for_aliases(row: Mapping[str, Any], aliases: Sequence[str]) -> list[Any]:
    return [_decoded(row[name]) for name in aliases if name in row and row[name] is not None]


def _first_value(source: Mapping[str, Any], aliases: Sequence[str]) -> Any:
    for name in aliases:
        if name in source and source[name] is not None:
            return source[name]
    return None


def _decoded(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return value


def _as_mapping(value: Any) -> dict[str, Any]:
    decoded = _decoded(value)
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _as_items(value: Any, *, unwrap_events: bool = False) -> list[Any]:
    decoded = _decoded(value)
    if decoded in (None, ""):
        return []
    if unwrap_events and isinstance(decoded, Mapping) and isinstance(decoded.get("events"), list):
        decoded = decoded["events"]
    if isinstance(decoded, list):
        return [_redact_sensitive(item) for item in decoded]
    if isinstance(decoded, tuple):
        return [_redact_sensitive(item) for item in decoded]
    return [_redact_sensitive(decoded)]


def _as_warning_list(value: Any) -> list[str]:
    decoded = _decoded(value)
    if decoded in (None, "", []):
        return []
    if isinstance(decoded, Mapping):
        return [f"{key}: {_inline_json(item)}" for key, item in decoded.items()]
    if isinstance(decoded, (list, tuple)):
        return [str(item) for item in decoded if item not in (None, "")]
    return [str(decoded)]


def _latest_attempt_batch_per_announcement(
    rows: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Keep the newest run for each announcement while retaining every stage."""

    latest: dict[tuple[str, str], int] = {}
    for index, row in enumerate(rows):
        identity = (
            str(_first_value(row, ("exchange", "announcement_exchange")) or ""),
            str(
                _first_value(
                    row,
                    ("external_id", "announcement_external_id", "announcement_id"),
                )
                or f"row-{index}"
            ),
        )
        try:
            run_id = int(row.get("run_id")) if row.get("run_id") is not None else None
        except (TypeError, ValueError):
            run_id = None
        if run_id is not None:
            latest[identity] = max(latest.get(identity, run_id), run_id)

    selected: list[Mapping[str, Any]] = []
    for index, row in enumerate(rows):
        identity = (
            str(_first_value(row, ("exchange", "announcement_exchange")) or ""),
            str(
                _first_value(
                    row,
                    ("external_id", "announcement_external_id", "announcement_id"),
                )
                or f"row-{index}"
            ),
        )
        expected = latest.get(identity)
        if expected is None:
            selected.append(row)
            continue
        try:
            run_id = int(row.get("run_id")) if row.get("run_id") is not None else None
        except (TypeError, ValueError):
            run_id = None
        if run_id == expected:
            selected.append(row)
    return selected


def _append_unique(target: list[Any], value: Any) -> None:
    fingerprint = _stable_json(value)
    if all(_stable_json(item) != fingerprint for item in target):
        target.append(value)


def _extend_unique(target: list[Any], values: Iterable[Any]) -> None:
    for value in values:
        _append_unique(target, value)


def _extend_unique_events(target: list[Any], values: Iterable[Any]) -> None:
    """Merge final events by business identity, ignoring audit-only metadata."""

    known = {_event_identity(item) for item in target}
    for value in values:
        identity = _event_identity(value)
        if identity not in known:
            target.append(value)
            known.add(identity)


def _event_identity(value: Any) -> str:
    if not isinstance(value, Mapping):
        return _stable_json(value)
    business_key = {
        key: value.get(key)
        for key in (
            "published_date",
            "exchange",
            "market_maker",
            "security_code",
            "effective_date",
            "action",
            "service_type_raw",
            "source_url",
        )
    }
    return _stable_json(business_key)


def _merge_nonempty(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        if value not in (None, "", []):
            target.setdefault(key, value)


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_scalar)


def _inline_json(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, default=_json_scalar)


def _evidence_location(item: Mapping[str, Any]) -> str:
    page = item.get("page_no")
    start = item.get("char_start")
    end = item.get("char_end")
    parts = []
    if page is not None:
        parts.append(f"page={page}")
    if start is not None or end is not None:
        parts.append(f"chars={start}:{end}")
    return " [" + ", ".join(parts) + "]" if parts else ""


def _json_scalar(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            normalized_key = key_text.replace("-", "_").replace(" ", "_")
            if _SENSITIVE_KEY.search(normalized_key):
                result[key_text] = "[REDACTED]"
            else:
                result[key_text] = _redact_sensitive(item)
        return result
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, str):
        sanitized = _CREDENTIAL_TEXT.sub(r"\1[REDACTED]", value)
        return _BEARER_TEXT.sub("Bearer [REDACTED]", sanitized)
    return value


__all__ = ["build_audit_payload", "write_audit_report"]
