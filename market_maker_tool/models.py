from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(slots=True)
class AnnouncementCandidate:
    exchange: str
    external_id: str
    canonical_url: str
    title: str
    published_date: date
    publisher: str
    source_kind: str
    detail_url: str | None = None
    attachment_url: str | None = None
    content_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParsedAnnouncement:
    candidate: AnnouncementCandidate
    text: str
    raw_path: str
    text_path: str
    raw_sha256: str
    parser: str
    parse_warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Evidence:
    field_name: str
    quote: str
    page_no: int | None = None
    char_start: int | None = None
    char_end: int | None = None


@dataclass(slots=True)
class ExtractionAuditRecord:
    """Append-only audit payload for one extractor stage.

    ``raw_response`` may contain a complete model response body, while the
    event collections hold the progressively normalised representations used
    by extraction and reconciliation.  Request metadata is intentionally not
    represented: API keys, authorization headers and endpoint URLs must never
    be persisted in this record.
    """

    exchange: str
    external_id: str
    extractor: str
    stage: str
    succeeded: bool
    run_id: int | None = None
    status: str = ""
    raw_response: Any = None
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    validated_events: list[dict[str, Any]] = field(default_factory=list)
    rejected_events: list[dict[str, Any]] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    created_at: datetime | None = None


@dataclass(slots=True)
class MarketMakingEvent:
    published_date: date
    exchange: str
    market_maker: str
    security_code: str
    effective_date: date | None
    action: str
    service_type_raw: str
    service_class: str
    source_url: str
    security_name: str = ""
    publisher: str = ""
    announcement_external_id: str = ""
    extractor: str = ""
    confidence: str = "LOW"
    review_status: str = "NEEDS_REVIEW"
    evidence: list[Evidence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def event_fingerprint_parts(self) -> tuple[str, ...]:
        return (
            self.published_date.isoformat(),
            self.exchange,
            normalize_identity(self.market_maker),
            self.security_code,
            self.effective_date.isoformat() if self.effective_date else "",
            self.action,
            self.service_class,
        )

    def as_json_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["published_date"] = self.published_date.isoformat()
        result["effective_date"] = self.effective_date.isoformat() if self.effective_date else None
        return result


def normalize_identity(value: str) -> str:
    return "".join(value.split()).replace("（", "(").replace("）", ")")
