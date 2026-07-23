from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .models import (
    AnnouncementCandidate,
    ExtractionAuditRecord,
    MarketMakingEvent,
    ParsedAnnouncement,
    normalize_identity,
)


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_date TEXT NOT NULL,
    mode TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    stats_json TEXT NOT NULL DEFAULT '{}',
    error TEXT
);

CREATE TABLE IF NOT EXISTS announcements (
    announcement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT NOT NULL,
    external_id TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    title TEXT NOT NULL,
    published_date TEXT NOT NULL,
    publisher TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    detail_url TEXT,
    attachment_url TEXT,
    raw_path TEXT,
    text_path TEXT,
    raw_sha256 TEXT,
    parser TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    parse_warnings_json TEXT NOT NULL DEFAULT '[]',
    retrieved_at TEXT NOT NULL,
    UNIQUE(exchange, external_id)
);

CREATE TABLE IF NOT EXISTS extraction_audits (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(run_id) ON DELETE SET NULL,
    exchange TEXT NOT NULL,
    external_id TEXT NOT NULL,
    extractor TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    succeeded INTEGER NOT NULL CHECK(succeeded IN (0, 1)),
    raw_response_json TEXT NOT NULL DEFAULT 'null',
    raw_events_json TEXT NOT NULL DEFAULT '[]',
    validated_events_json TEXT NOT NULL DEFAULT '[]',
    rejected_events_json TEXT NOT NULL DEFAULT '[]',
    rejection_reasons_json TEXT NOT NULL DEFAULT '[]',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    FOREIGN KEY(exchange, external_id)
        REFERENCES announcements(exchange, external_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_extraction_audits_announcement
    ON extraction_audits(exchange, external_id, created_at, audit_id);
CREATE INDEX IF NOT EXISTS idx_extraction_audits_run
    ON extraction_audits(run_id, audit_id);
CREATE INDEX IF NOT EXISTS idx_announcements_published_date
    ON announcements(published_date, exchange, external_id);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    published_date TEXT NOT NULL,
    exchange TEXT NOT NULL,
    market_maker TEXT NOT NULL,
    security_code TEXT NOT NULL,
    security_name TEXT NOT NULL DEFAULT '',
    effective_date TEXT,
    action TEXT NOT NULL,
    service_type_raw TEXT NOT NULL,
    service_class TEXT NOT NULL,
    publisher TEXT NOT NULL DEFAULT '',
    extractor TEXT NOT NULL,
    confidence TEXT NOT NULL,
    review_status TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_sources (
    event_id INTEGER NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    exchange TEXT NOT NULL,
    external_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    PRIMARY KEY(event_id, source_url)
);
"""

_EXTRACTION_AUDIT_SELECT = """
SELECT
    ea.audit_id,
    ea.run_id,
    ea.exchange,
    ea.external_id,
    ea.extractor,
    ea.stage,
    ea.status,
    ea.succeeded,
    ea.raw_response_json,
    ea.raw_events_json,
    ea.validated_events_json,
    ea.rejected_events_json,
    ea.rejection_reasons_json,
    ea.warnings_json,
    ea.created_at,
    a.announcement_id,
    a.title,
    a.published_date,
    a.canonical_url,
    a.publisher,
    a.raw_path,
    a.text_path,
    a.parser,
    a.parse_warnings_json
FROM extraction_audits ea
JOIN announcements a
  ON a.exchange=ea.exchange AND a.external_id=ea.external_id
"""

_SENSITIVE_AUDIT_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "authorization",
    "proxy_authorization",
    "secret",
    "password",
    "endpoint",
    "endpoint_url",
    "api_base",
    "base_url",
    "request_headers",
}
_AUDIT_CREDENTIAL_TEXT = re.compile(
    r"(?i)(\b(?:api[_-]?key|authorization)\b\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
_AUDIT_BEARER_TEXT = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_AUDIT_ENDPOINT_TEXT = re.compile(
    r"(?i)(\b(?:endpoint|api[_-]?base|base[_-]?url)\b\s*[:=]\s*)https?://[^\s,;]+"
)


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate_bare_liquidity_services(conn)

    @staticmethod
    def _migrate_bare_liquidity_services(conn: sqlite3.Connection) -> None:
        """Upgrade pre-rule-change events without creating report duplicates.

        Event fingerprints contain ``service_class``.  Merely changing an old
        row from ``UNSPECIFIED`` to ``GENERAL`` can therefore collide with an
        event already written by newer code.  In that case source links are
        merged into the canonical row before the obsolete row is removed.
        The migration is intentionally idempotent and leaves append-only
        extraction audit snapshots untouched.
        """

        rows = conn.execute(
            """SELECT event_id, published_date, exchange, market_maker,
                      security_code, effective_date, action
               FROM events
               WHERE service_type_raw='流动性服务商'"""
        ).fetchall()
        now = datetime.now().isoformat(timespec="seconds")
        for row in rows:
            parts = (
                row["published_date"],
                row["exchange"],
                normalize_identity(row["market_maker"]),
                row["security_code"],
                row["effective_date"] or "",
                row["action"],
                "GENERAL",
            )
            fingerprint = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
            canonical = conn.execute(
                "SELECT event_id FROM events WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
            old_event_id = int(row["event_id"])
            if canonical is not None and int(canonical["event_id"]) != old_event_id:
                canonical_event_id = int(canonical["event_id"])
                conn.execute(
                    """INSERT OR IGNORE INTO event_sources(event_id, exchange, external_id, source_url)
                       SELECT ?, exchange, external_id, source_url
                       FROM event_sources WHERE event_id=?""",
                    (canonical_event_id, old_event_id),
                )
                conn.execute(
                    """UPDATE events
                       SET service_type_raw='一般流动性服务商', service_class='GENERAL', updated_at=?
                       WHERE event_id=?""",
                    (now, canonical_event_id),
                )
                conn.execute("DELETE FROM events WHERE event_id=?", (old_event_id,))
            else:
                conn.execute(
                    """UPDATE events
                       SET fingerprint=?, service_type_raw='一般流动性服务商',
                           service_class='GENERAL', updated_at=?
                       WHERE event_id=?""",
                    (fingerprint, now, old_event_id),
                )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def start_run(self, target_date: date, mode: str) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs(target_date, mode, started_at, status) VALUES (?, ?, ?, 'RUNNING')",
                (target_date.isoformat(), mode, datetime.now().isoformat(timespec="seconds")),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, stats: dict, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE runs SET finished_at=?, status=?, stats_json=?, error=? WHERE run_id=?",
                (datetime.now().isoformat(timespec="seconds"), status, json.dumps(stats, ensure_ascii=False), error, run_id),
            )

    def upsert_announcement(self, parsed: ParsedAnnouncement) -> int:
        c = parsed.candidate
        now = datetime.now().isoformat(timespec="seconds")
        values = (
            c.exchange, c.external_id, c.canonical_url, c.title, c.published_date.isoformat(),
            c.publisher, c.source_kind, c.detail_url, c.attachment_url, parsed.raw_path,
            parsed.text_path, parsed.raw_sha256, parsed.parser,
            json.dumps(c.metadata, ensure_ascii=False), json.dumps(parsed.parse_warnings, ensure_ascii=False), now,
        )
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO announcements(
                    exchange, external_id, canonical_url, title, published_date, publisher, source_kind,
                    detail_url, attachment_url, raw_path, text_path, raw_sha256, parser,
                    metadata_json, parse_warnings_json, retrieved_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(exchange, external_id) DO UPDATE SET
                    canonical_url=excluded.canonical_url, title=excluded.title,
                    detail_url=excluded.detail_url, attachment_url=excluded.attachment_url,
                    raw_path=excluded.raw_path, text_path=excluded.text_path,
                    raw_sha256=excluded.raw_sha256, parser=excluded.parser,
                    metadata_json=excluded.metadata_json,
                    parse_warnings_json=excluded.parse_warnings_json,
                    retrieved_at=excluded.retrieved_at""",
                values,
            )
            row = conn.execute(
                "SELECT announcement_id FROM announcements WHERE exchange=? AND external_id=?",
                (c.exchange, c.external_id),
            ).fetchone()
            return int(row[0])

    def insert_extraction_audit(self, record: ExtractionAuditRecord) -> int:
        """Append one extractor-stage audit record and return its row id.

        The referenced announcement must already exist.  This matches the
        pipeline order (parse/store announcement, then extract) and keeps audit
        rows tied to inspectable source text.  JSON bodies are stored as TEXT
        without a size limit; known credential/request-routing keys are
        recursively redacted before serialisation.
        """

        exchange = record.exchange.strip().upper()
        external_id = record.external_id.strip()
        extractor = record.extractor.strip()
        stage = record.stage.strip().lower()
        if not exchange or not external_id or not extractor or not stage:
            raise ValueError("抽取审计的 exchange/external_id/extractor/stage 不能为空")

        status = (record.status or ("SUCCESS" if record.succeeded else "FAILED")).strip().upper()
        if not status:
            raise ValueError("抽取审计的 status 不能为空")
        created_at = record.created_at or datetime.now()
        if isinstance(created_at, datetime):
            created_at_text = created_at.isoformat(timespec="seconds")
        else:  # Defensive compatibility for callers deserialising old payloads.
            created_at_text = str(created_at)

        with self.connect() as conn:
            cursor = conn.execute(
                """INSERT INTO extraction_audits(
                    run_id, exchange, external_id, extractor, stage, status, succeeded,
                    raw_response_json, raw_events_json, validated_events_json,
                    rejected_events_json, rejection_reasons_json, warnings_json, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record.run_id,
                    exchange,
                    external_id,
                    extractor,
                    stage,
                    status,
                    int(bool(record.succeeded)),
                    _audit_json_dumps(record.raw_response),
                    _audit_json_dumps(record.raw_events),
                    _audit_json_dumps(record.validated_events),
                    _audit_json_dumps(record.rejected_events),
                    _audit_json_dumps(record.rejection_reasons),
                    _audit_json_dumps(record.warnings),
                    created_at_text,
                ),
            )
            return int(cursor.lastrowid)

    # Readable alias for call sites that treat persistence as an audit sink.
    def write_extraction_audit(self, record: ExtractionAuditRecord) -> int:
        return self.insert_extraction_audit(record)

    def extraction_audits_for_date(
        self,
        target_date: date,
        announcement_id: str | int | None = None,
    ) -> list[dict]:
        """Return flat audit rows for an announcement date.

        ``announcement_id`` accepts either the exchange's external id or the
        internal numeric ``announcements.announcement_id``.  JSON columns stay
        as JSON strings so a CLI can stream large raw model responses without
        changing their structure.
        """

        parameters: list[Any] = [target_date.isoformat()]
        filter_sql = ""
        if announcement_id is not None:
            identifier = str(announcement_id)
            filter_sql = (
                " AND (a.external_id=? OR CAST(a.announcement_id AS TEXT)=?)"
            )
            parameters.extend((identifier, identifier))
        with self.connect() as conn:
            rows = conn.execute(
                _EXTRACTION_AUDIT_SELECT
                + " WHERE a.published_date=?"
                + filter_sql
                + " ORDER BY ea.created_at, ea.audit_id",
                parameters,
            ).fetchall()
            return _audit_rows_with_final_events(conn, rows)

    def extraction_audits_for_announcement(
        self,
        exchange: str,
        external_id: str,
    ) -> list[dict]:
        """Return all audit stages for one official announcement id."""

        with self.connect() as conn:
            rows = conn.execute(
                _EXTRACTION_AUDIT_SELECT
                + " WHERE ea.exchange=? AND ea.external_id=?"
                + " ORDER BY ea.created_at, ea.audit_id",
                (exchange.strip().upper(), external_id.strip()),
            ).fetchall()
            return _audit_rows_with_final_events(conn, rows)

    def upsert_event(self, event: MarketMakingEvent) -> int:
        with self.connect() as conn:
            return self._upsert_event(conn, event)

    def _upsert_event(self, conn: sqlite3.Connection, event: MarketMakingEvent) -> int:
        fingerprint = hashlib.sha256("|".join(event.event_fingerprint_parts()).encode("utf-8")).hexdigest()
        now = datetime.now().isoformat(timespec="seconds")
        evidence_json = json.dumps([asdict(item) for item in event.evidence], ensure_ascii=False)
        warnings_json = json.dumps(event.warnings, ensure_ascii=False)
        conn.execute(
            """INSERT INTO events(
                fingerprint, published_date, exchange, market_maker, security_code, security_name,
                effective_date, action, service_type_raw, service_class, publisher, extractor,
                confidence, review_status, evidence_json, warnings_json, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                security_name=CASE WHEN excluded.security_name != '' THEN excluded.security_name ELSE events.security_name END,
                service_type_raw=excluded.service_type_raw,
                publisher=excluded.publisher,
                extractor=excluded.extractor,
                confidence=excluded.confidence,
                review_status=excluded.review_status,
                evidence_json=excluded.evidence_json,
                warnings_json=excluded.warnings_json,
                updated_at=excluded.updated_at""",
            (
                fingerprint, event.published_date.isoformat(), event.exchange, event.market_maker,
                event.security_code, event.security_name,
                event.effective_date.isoformat() if event.effective_date else None,
                event.action, event.service_type_raw, event.service_class, event.publisher,
                event.extractor, event.confidence, event.review_status, evidence_json,
                warnings_json, now, now,
            ),
        )
        row = conn.execute("SELECT event_id FROM events WHERE fingerprint=?", (fingerprint,)).fetchone()
        event_id = int(row[0])
        conn.execute(
            "INSERT OR IGNORE INTO event_sources(event_id, exchange, external_id, source_url) VALUES (?,?,?,?)",
            (event_id, event.exchange, event.announcement_external_id, event.source_url),
        )
        return event_id

    def replace_source_events(
        self,
        exchange: str,
        external_id: str,
        events: Iterable[MarketMakingEvent],
    ) -> None:
        """Replace one announcement's event links, then remove orphaned old events."""
        event_list = list(events)
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM event_sources WHERE exchange=? AND external_id=?",
                (exchange.upper(), external_id),
            )
            conn.execute(
                "DELETE FROM events WHERE NOT EXISTS "
                "(SELECT 1 FROM event_sources es WHERE es.event_id=events.event_id)"
            )
            for event in event_list:
                self._upsert_event(conn, event)

    def events_for_date(self, target_date: date) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT e.*, GROUP_CONCAT(es.source_url, '; ') AS source_urls
                FROM events e LEFT JOIN event_sources es ON es.event_id=e.event_id
                WHERE e.published_date=?
                GROUP BY e.event_id
                ORDER BY e.exchange, e.security_code, e.market_maker""",
                (target_date.isoformat(),),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_announcement(self, exchange: str, external_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM announcements WHERE exchange=? AND external_id=?",
                (exchange.upper(), external_id),
            ).fetchone()
            return dict(row) if row else None

    def recent_runs(self, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]

    def latest_successful_target_date(self, mode: str = "daily") -> date | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(target_date) FROM runs WHERE mode=? AND status='SUCCESS'",
                (mode,),
            ).fetchone()
            return date.fromisoformat(row[0]) if row and row[0] else None

    def incomplete_daily_dates(self, through: date) -> list[date]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT target_date FROM runs
                WHERE mode='daily' AND target_date<=?
                GROUP BY target_date
                HAVING SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END)=0
                ORDER BY target_date""",
                (through.isoformat(),),
            ).fetchall()
            return [date.fromisoformat(row[0]) for row in rows]


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _audit_row(row: sqlite3.Row) -> dict:
    result = dict(row)
    result["succeeded"] = bool(result["succeeded"])
    return result


def _audit_rows_with_final_events(
    conn: sqlite3.Connection,
    rows: Iterable[sqlite3.Row],
) -> list[dict]:
    result = [_audit_row(row) for row in rows]
    final_by_announcement: dict[tuple[str, str], str] = {}
    for item in result:
        identity = (item["exchange"], item["external_id"])
        if identity in final_by_announcement:
            continue
        event_rows = conn.execute(
            """SELECT
                e.event_id, e.published_date, e.exchange, e.market_maker,
                e.security_code, e.security_name, e.effective_date, e.action,
                e.service_type_raw, e.service_class, e.publisher, e.extractor,
                e.confidence, e.review_status, e.evidence_json, e.warnings_json,
                es.source_url
            FROM event_sources es
            JOIN events e ON e.event_id=es.event_id
            WHERE es.exchange=? AND es.external_id=?
            ORDER BY e.security_code, e.market_maker, e.event_id""",
            identity,
        ).fetchall()
        events: list[dict[str, Any]] = []
        for event_row in event_rows:
            event = dict(event_row)
            event["evidence"] = json.loads(event.pop("evidence_json") or "[]")
            event["warnings"] = json.loads(event.pop("warnings_json") or "[]")
            events.append(event)
        final_by_announcement[identity] = _audit_json_dumps(events)
    for item in result:
        item["final_events_json"] = final_by_announcement[
            (item["exchange"], item["external_id"])
        ]
    return result


def _audit_json_dumps(value: Any) -> str:
    sanitized = _sanitize_audit_payload(value)
    return json.dumps(
        sanitized,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_audit_json_default,
    )


def _sanitize_audit_payload(value: Any) -> Any:
    """Remove credentials and request-routing metadata from audit JSON."""

    if is_dataclass(value):
        return _sanitize_audit_payload(asdict(value))
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = key.casefold().replace("-", "_").replace(" ", "_")
            if normalized in _SENSITIVE_AUDIT_KEYS:
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = _sanitize_audit_payload(item)
        return sanitized
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_audit_payload(item) for item in value]
    if isinstance(value, str):
        sanitized = _AUDIT_CREDENTIAL_TEXT.sub(r"\1[REDACTED]", value)
        sanitized = _AUDIT_BEARER_TEXT.sub("Bearer [REDACTED]", sanitized)
        return _AUDIT_ENDPOINT_TEXT.sub(r"\1[REDACTED]", sanitized)
    return value


def _audit_json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    raise TypeError(f"不支持写入抽取审计JSON的类型: {type(value).__name__}")
