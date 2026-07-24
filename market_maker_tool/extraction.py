"""Market-making announcement detection and structured extraction.

The module deliberately keeps the two extractors independent:

* :class:`RuleExtractor` only sees exchange metadata and source text.
* :class:`LLMExtractor` only sees the same source data, never rule output.
* :func:`reconcile_events` compares their field-level results afterwards.

Rules are not one large regular expression.  They combine announcement
structure, clause boundaries, small field regexes, entity pairing and source
evidence validation.  This makes the common SSE/SZSE templates deterministic
while leaving unusual prose to the LLM path.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from .config import LLMProviderConfig, Settings
from .models import ExtractionAuditRecord, Evidence, MarketMakingEvent, ParsedAnnouncement


SERVICE_PATTERN = re.compile(
    r"主\s*流\s*动\s*性\s*服\s*务\s*商|一\s*般\s*流\s*动\s*性\s*服\s*务\s*商|"
    r"主\s*做\s*市\s*服\s*务|一\s*般\s*做\s*市\s*服\s*务|流\s*动\s*性\s*服\s*务\s*商"
)
CANDIDATE_SERVICE_KEYWORDS = {
    "SSE": "做市服务",
    "SZSE": "流动性服务",
}
SERVICE_TRANSITION_PATTERN = re.compile(
    r"(?:调整|变更)\s*为\s*(?P<service>" + SERVICE_PATTERN.pattern + r")"
)
SERVICE_ASSIGNMENT_PATTERN = re.compile(
    r"(?:指定|选定).{0,300}?为.{0,200}?(?P<service>"
    + SERVICE_PATTERN.pattern
    + r")",
    re.S,
)
CODE_PATTERN = re.compile(
    r"(?:(?:[（(]?\s*(?:(?:基金|证券)\s*)?代\s*码\s*[：:]\s*)|(?:[（(]\s*))"
    r"(?P<code>\d{6})\s*[）)]?"
)
DATE_PATTERN = re.compile(
    r"(?P<year>20\d{2})\s*[年/-]\s*(?P<month>0?[1-9]|1[0-2])\s*[月/-]\s*"
    r"(?P<day>3[01]|[12]\d|0?[1-9])\s*(?:日|(?=$|\s|[，,。；;/]))"
)
EFFECTIVE_DATE_PATTERN = re.compile(
    r"(?:自|从|于)\s*(?P<date>20\d{2}\s*[年/-]\s*(?:0?[1-9]|1[0-2])\s*[月/-]\s*"
    r"(?:3[01]|[12]\d|0?[1-9])\s*日?)\s*(?:起|开始|生效)"
)
EXPLICIT_ACTION_START = re.compile(r"(?:本公司)?(?:新增|选定|指定|终止|调整|变更|停止|不再)")
COMPANY_SUFFIX_PATTERN = re.compile(
    r"证\s*券\s*股\s*份\s*有\s*限\s*公\s*司|"
    r"证\s*券\s*有\s*限\s*责\s*任\s*公\s*司|证\s*券\s*有\s*限\s*公\s*司|"
    r"金\s*融\s*股\s*份\s*有\s*限\s*公\s*司|金\s*融\s*有\s*限\s*责\s*任\s*公\s*司"
)
COMPANY_RUN_PATTERN = re.compile(
    r"[\u3400-\u9fffA-Za-z（）()·\s]{2,60}?"
    r"(?:证\s*券\s*股\s*份\s*有\s*限\s*公\s*司|"
    r"证\s*券\s*有\s*限\s*责\s*任\s*公\s*司|证\s*券\s*有\s*限\s*公\s*司|"
    r"金\s*融\s*股\s*份\s*有\s*限\s*公\s*司|金\s*融\s*有\s*限\s*责\s*任\s*公\s*司)"
)

_ACTION_CANONICAL = {
    "新增": "新增",
    "选定": "新增",
    "指定": "新增",
    "终止": "终止",
    "停止": "终止",
    "不再": "终止",
    "调整": "调整",
    "变更": "调整",
}
_ACTION_EVIDENCE_PATTERNS = {
    "新增": re.compile(
        r"新\s*增|选\s*定|指\s*定|"
        r"提\s*供\s*(?:(?:主|一\s*般)\s*)?(?:做\s*市\s*服\s*务|流\s*动\s*性\s*服\s*务)|"
        r"(?:担\s*任|成\s*为).{0,80}?(?:做\s*市\s*服\s*务|流\s*动\s*性\s*服\s*务\s*商)",
        re.S,
    ),
    "终止": re.compile(r"终\s*止|停\s*止|不\s*再"),
    "调整": re.compile(r"调\s*整|变\s*更"),
}
_NEGATED_ADD_EVIDENCE_PATTERN = re.compile(
    r"不\s*涉\s*及\s*(?:新\s*增|选\s*定|指\s*定)|"
    r"(?:无\s*需|无\s*须|未|不)\s*(?:新\s*增|选\s*定|指\s*定)|"
    r"(?:未|拟|曾|继\s*续|仍\s*然?|持\s*续|暂\s*停|恢\s*复)\s*提\s*供"
)
_NEGATED_TERMINATION_PATTERN = re.compile(
    r"(?:不\s*涉\s*及|无\s*需|无\s*须|未|并\s*未|不)\s*"
    r"(?:终\s*止|停\s*止)|"
    r"不\s*再\s*(?:新\s*增|选\s*定|指\s*定|调\s*整|变\s*更)"
)
_NEGATED_ADJUSTMENT_PATTERN = re.compile(
    r"(?:不\s*涉\s*及|无\s*需|无\s*须|未|并\s*未|不)\s*"
    r"(?:调\s*整|变\s*更)"
)
_CONTINUING_SERVICE_PATTERN = re.compile(
    r"(?:拟|计\s*划|曾|继\s*续|仍\s*然?|持\s*续|暂\s*停|恢\s*复)\s*"
    r"(?:为|向)?.{0,120}?提\s*供\s*(?:(?:主|一\s*般)\s*)?"
    r"(?:做\s*市\s*服\s*务|流\s*动\s*性\s*服\s*务)",
    re.S,
)
_LIST_PREFACE_CUE_PATTERN = re.compile(
    r"下\s*列|以\s*下|如\s*下|分\s*别|部\s*分\s*基\s*金|"
    r"相\s*关\s*基\s*金|旗\s*下\s*(?:部\s*分\s*)?基\s*金"
)
_SERVICE_CLASS = {
    "主做市服务": "PRIMARY",
    "主流动性服务商": "PRIMARY",
    "一般做市服务": "GENERAL",
    "一般流动性服务商": "GENERAL",
    # Kept as a defensive input alias.  Extracted/reconciled output is
    # canonicalised to ``一般流动性服务商`` before it reaches a report.
    "流动性服务商": "GENERAL",
}
_KEY_FIELDS = (
    "market_maker",
    "security_code",
    "security_name",
    "effective_date",
    "action",
    "service_type_raw",
)
_CORE_FIELDS = (
    "market_maker",
    "security_code",
    "effective_date",
    "action",
    "service_type_raw",
)
_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class _LocatedValue:
    value: Any
    quote: str
    start: int | None
    end: int | None


def is_candidate(candidate: Any, text: str = "") -> bool:
    """Return whether an announcement is a plausible in-scope candidate.

    The exchange-specific short keyword must occur independently in both the
    disclosure title and parsed body: ``做市服务`` for SSE and ``流动性服务``
    for SZSE.  Full tier terminology is deliberately left to field extraction,
    so candidate recall does not depend on a long phrase.  Requiring both sides
    prevents a prospectus whose historical-disclosure appendix mentions a
    liquidity provider from becoming a candidate.
    ETF option market-making and market-maker qualification notices are outside
    the agreed fund scope and are rejected explicitly.
    """

    title = candidate if isinstance(candidate, str) else getattr(candidate, "title", "")
    exchange = "" if isinstance(candidate, str) else str(getattr(candidate, "exchange", "")).upper()
    compact_title = re.sub(r"\s+", "", title or "")
    compact_text = re.sub(r"\s+", "", text or "")
    if not compact_title or not compact_text:
        return False
    keywords = (
        (CANDIDATE_SERVICE_KEYWORDS[exchange],)
        if exchange in CANDIDATE_SERVICE_KEYWORDS
        else tuple(CANDIDATE_SERVICE_KEYWORDS.values())
    )
    if not any(
        keyword in compact_title and keyword in compact_text
        for keyword in keywords
    ):
        return False

    haystack = f"{title}\n{text}"
    compact = re.sub(r"\s+", "", haystack)
    if re.search(r"ETF期权|交易型开放式指数期权|股票期权", compact, re.I):
        return False
    if "做市商资格" in compact and not SERVICE_PATTERN.search(compact):
        return False
    # A service phrase plus a six-digit fund code is already strong evidence;
    # otherwise require an ETF/fund term to avoid unrelated securities notices.
    return bool(
        re.search(r"ETF|交易型开放式|上市(?:开放式)?基金|证券投资基金|基金", compact, re.I)
        or CODE_PATTERN.search(compact)
    )


# More descriptive alias for callers that prefer an explicit name.
is_market_making_candidate = is_candidate


def _normalise_service_type(value: str) -> str:
    """Apply the agreed display/category rule to a service phrase.

    Whitespace inside a term is a common PDF extraction artefact.  SZSE uses
    ``主流动性服务商`` whenever the primary tier applies, so a bare
    ``流动性服务商`` is treated as and displayed as ``一般流动性服务商``.
    The original span is still retained separately in :class:`Evidence`.
    """

    compact = re.sub(r"\s+", "", value or "")
    if compact == "流动性服务商":
        return "一般流动性服务商"
    return compact


def service_class(service_type_raw: str) -> str:
    """Map a displayed service type to its internal comparison class."""

    return _SERVICE_CLASS.get(_normalise_service_type(service_type_raw), "UNSPECIFIED")


def _normalise_identity(value: str) -> str:
    return re.sub(r"[\s（）()]", "", value or "")


def _normalise_source(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    match = DATE_PATTERN.search(value.strip())
    if not match:
        return None
    try:
        return date(int(match["year"]), int(match["month"]), int(match["day"]))
    except ValueError:
        return None


def _evidence(field_name: str, located: _LocatedValue) -> Evidence:
    return Evidence(
        field_name=field_name,
        quote=located.quote,
        char_start=located.start,
        char_end=located.end,
    )


def _find_quote(source: str, quote: str, field_name: str) -> Evidence | None:
    quote = (quote or "").strip()
    if not quote:
        return None
    start = source.find(quote)
    if start < 0:
        return None
    return Evidence(field_name, quote, char_start=start, char_end=start + len(quote))


def _find_quote_ignoring_whitespace(source: str, value: str, field_name: str) -> Evidence | None:
    """Locate a model's compact value in PDF text containing layout spaces."""

    compact = re.sub(r"\s+", "", value or "")
    if not compact:
        return None
    pattern = re.compile(r"\s*".join(re.escape(character) for character in compact))
    match = pattern.search(source)
    if not match:
        return None
    return Evidence(field_name, match.group(0), char_start=match.start(), char_end=match.end())


def _find_service_evidence(source: str, value: str) -> Evidence | None:
    """Locate a service phrase using business semantics, not substring search.

    In particular, searching for the bare text ``流动性服务商`` must not match
    the suffix of ``主流动性服务商``.  ``SERVICE_PATTERN`` orders the explicit
    tiers before the bare alternative and therefore gives us whole semantic
    phrases even when a PDF inserts whitespace between characters.
    """

    expected = _normalise_service_type(value)
    if not expected:
        return None
    for match in SERVICE_PATTERN.finditer(source):
        if _normalise_service_type(match.group(0)) == expected:
            return Evidence(
                "service_type_raw",
                match.group(0),
                char_start=match.start(),
                char_end=match.end(),
            )
    return None


def _action_from_text(value: str) -> _LocatedValue | None:
    inferred = _action_evidence_from_text(value)
    if inferred is None:
        return None
    action, match = inferred
    return _LocatedValue(action, match.group(0), match.start(), match.end())


def _service_from_text(value: str) -> _LocatedValue | None:
    # In a grade transition the report must show the resulting grade, never
    # the first (old) service phrase in the sentence.
    transition = SERVICE_TRANSITION_PATTERN.search(value)
    if transition:
        raw = transition.group("service")
        start, end = transition.span("service")
        return _LocatedValue(_normalise_service_type(raw), raw, start, end)
    # In ``指定下列流动性服务商为…主流动性服务商`` the first phrase
    # describes the listed entities generically; the phrase after ``为`` is
    # the service tier being assigned and is the value the report must retain.
    assignment = SERVICE_ASSIGNMENT_PATTERN.search(value)
    if assignment:
        raw = assignment.group("service")
        start, end = assignment.span("service")
        return _LocatedValue(_normalise_service_type(raw), raw, start, end)
    match = SERVICE_PATTERN.search(value)
    if not match:
        return None
    raw = match.group(0)
    # PDF text layers may insert layout whitespace inside a term.  Evidence
    # retains the byte-for-byte quote; the display value also applies the SZSE
    # bare-liquidity-service business classification.
    display = _normalise_service_type(raw)
    return _LocatedValue(display, raw, match.start(), match.end())


def _is_grade_transition_action(source: str, action_match: re.Match[str]) -> bool:
    """Return whether an action word is internal to ``由旧等级调整为新等级``."""

    raw_action = action_match.group(0)
    if "调整" not in raw_action and "变更" not in raw_action:
        return False
    before = source[max(0, action_match.start() - 80) : action_match.start()]
    after = source[action_match.end() : action_match.end() + 10]
    old_service = list(SERVICE_PATTERN.finditer(before))
    if not old_service or before[old_service[-1].end() :].strip():
        return False
    before_old = before[: old_service[-1].start()].rstrip()
    return before_old.endswith("由") and bool(re.match(r"\s*为", after))


def _effective_date_from_text(value: str) -> _LocatedValue | None:
    match = EFFECTIVE_DATE_PATTERN.search(value)
    if not match:
        return None
    parsed = _parse_date(match.group("date"))
    if parsed is None:
        return None
    return _LocatedValue(parsed, match.group(0), match.start(), match.end())


def _soft_event_boundaries(source: str, left: int, right: int) -> list[int]:
    """Find comma/dunhao boundaries between independently owned fund rows.

    A soft separator is promoted to an event boundary only when the text on
    both sides contains a securities/financial company associated with the
    adjacent fund-code groups.  This keeps one-provider/multi-fund wording in
    one group while separating compact ``A/code1，B/code2`` announcements.
    """

    codes = list(CODE_PATTERN.finditer(source, left, right))
    boundaries: list[int] = []
    for index, (current, following) in enumerate(zip(codes, codes[1:])):
        separators = list(re.finditer(r"[，,、]", source[current.end() : following.start()]))
        if not separators:
            continue
        following_limit = codes[index + 2].start() if index + 2 < len(codes) else right
        tail = source[following.end() : following_limit]
        tail_boundary = re.search(r"[，,、。；;]", tail)
        if tail_boundary:
            tail = tail[: tail_boundary.start()]
        for separator in reversed(separators):
            absolute = current.end() + separator.start()
            left_group = source[left:absolute]
            next_prefix = source[absolute + 1 : following.start()]
            next_group_has_company = bool(
                COMPANY_SUFFIX_PATTERN.search(next_prefix)
                or COMPANY_SUFFIX_PATTERN.search(tail)
            )
            if COMPANY_SUFFIX_PATTERN.search(left_group) and next_group_has_company:
                boundaries.append(absolute)
                break
    return sorted(set(boundaries))


def _event_window(source: str, code_start: int, code_end: int) -> tuple[int, int]:
    """Find the smallest clause that owns a code.

    SZSE often joins multiple events with ``、新增`` instead of a semicolon,
    so explicit action starts are treated as secondary clause boundaries.
    """

    # A PDF newline is usually visual wrapping, not a semantic boundary.
    left = max(source.rfind(mark, 0, code_start) for mark in ("。", "；", ";")) + 1
    right_candidates = [pos for mark in ("。", "；", ";") if (pos := source.find(mark, code_end)) >= 0]
    right = min(right_candidates) if right_candidates else len(source)

    # Commas and dunhao are normally too weak to delimit prose, but they do
    # separate compact multi-event rows when a new company/code relation begins.
    soft_boundaries = _soft_event_boundaries(source, left, right)
    left_soft = [position for position in soft_boundaries if position < code_start]
    right_soft = [position for position in soft_boundaries if position > code_end]
    if left_soft:
        left = max(left, max(left_soft) + 1)
    if right_soft:
        right = min(right, min(right_soft))

    actions = list(EXPLICIT_ACTION_START.finditer(source, left, right))
    before_matches = [match for match in actions if match.start() <= code_start]
    after_matches = [match for match in actions if match.start() > code_start]
    before = [match.start() for match in before_matches]
    # An internal grade transition after the code belongs to the current event
    # and must not become the right boundary (which would retain the old grade).
    after = [
        match.start()
        for match in after_matches
        if not _is_grade_transition_action(source, match)
    ]
    if before:
        action_left = max(before)
        # SSE termination is written ``券商名称终止为基金...``.  In that
        # template the action is not a clause start; advancing to it would drop
        # the owning company.  Keep the sentence boundary only when a company
        # suffix is immediately adjacent to the action.  This does not affect
        # SZSE's ``、调整券商...`` multi-event delimiter.
        immediate_prefix = source[max(left, action_left - 60) : action_left]
        suffix_matches = list(COMPANY_SUFFIX_PATTERN.finditer(immediate_prefix))
        company_before_action = bool(
            suffix_matches and not immediate_prefix[suffix_matches[-1].end() :].strip()
        )
        owning_action = next(match for match in before_matches if match.start() == action_left)
        if not company_before_action and not _is_grade_transition_action(source, owning_action):
            left = action_left
            # Keep punctuation out of the company prefix but retain exact offsets.
            while left > 0 and source[left - 1] in "、，, ":
                left -= 1
    if after:
        right = min(after)
    return left, right


def _identity_occurs_in_text(text: str, value: str) -> bool:
    compact = re.sub(r"\s+", "", value or "")
    if not compact:
        return False
    pattern = re.compile(r"\s*".join(re.escape(character) for character in compact))
    return bool(pattern.search(text))


def _safe_preface_ranges(
    source: str,
    ranges: Sequence[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Return only pre-event text that can safely govern an event.

    Two forms are supported: an immediate preface in the same hard sentence
    (for example a date before ``本公司新增``), and a structural list preface
    containing cues such as ``下列/以下/分别``.  Text after the target event is
    never considered, so another event cannot donate its action or date.
    """

    result: set[tuple[int, int]] = set()
    for left, _ in ranges:
        start = max(source.rfind(mark, 0, left) for mark in ("。", "；", ";")) + 1
        region = source[start:left]
        # A previous fund code means this is another row, not a direct preface.
        if start < left and len(region) <= 800 and not CODE_PATTERN.search(region):
            result.add((start, left))

        # Multiple implicit events may share a date/action before the first
        # concrete provider in one hard sentence.  The shared prefix itself
        # must contain neither a company nor a fund code.
        hard_right_candidates = [
            position
            for mark in ("。", "；", ";")
            if (position := source.find(mark, left)) >= 0
        ]
        hard_right = min(hard_right_candidates) if hard_right_candidates else len(source)
        sentence_codes = list(CODE_PATTERN.finditer(source, start, hard_right))
        if len(sentence_codes) >= 2:
            first_company = COMPANY_RUN_PATTERN.search(
                source,
                start,
                sentence_codes[0].start(),
            )
            if first_company:
                shared = source[start:first_company.start()]
                if (
                    shared
                    and len(shared) <= 800
                    and not CODE_PATTERN.search(shared)
                    and not COMPANY_SUFFIX_PATTERN.search(shared)
                    and re.search(r"[，,:：]\s*$", shared)
                ):
                    result.add((start, first_company.start()))

    codes = list(CODE_PATTERN.finditer(source))
    if codes:
        first_code = codes[0]
        start = source.rfind("。", 0, first_code.start()) + 1
        prefix = source[start:first_code.start()]
        target_left = min((left for left, _ in ranges), default=first_code.start())
        between_end = max(first_code.end(), target_left)
        stays_in_list = "。" not in source[first_code.end():between_end]
        if (
            stays_in_list
            and len(prefix) <= 1500
            and _LIST_PREFACE_CUE_PATTERN.search(prefix)
        ):
            result.add((start, first_code.start()))
    return sorted(result)


def _action_from_ranges(
    source: str,
    ranges: Sequence[tuple[int, int]],
) -> _LocatedValue | None:
    found: list[tuple[str, re.Match[str], int]] = []
    for left, right in ranges:
        inferred = _action_evidence_from_text(source[left:right])
        if inferred:
            found.append((inferred[0], inferred[1], left))
    if not found or len({action for action, _, _ in found}) != 1:
        return None
    action, match, offset = found[0]
    return _LocatedValue(
        action,
        match.group(0),
        offset + match.start(),
        offset + match.end(),
    )


def _effective_date_from_ranges(
    source: str,
    ranges: Sequence[tuple[int, int]],
) -> _LocatedValue | None:
    found: list[tuple[date, re.Match[str]]] = []
    for left, right in ranges:
        for match in EFFECTIVE_DATE_PATTERN.finditer(source, left, right):
            parsed = _parse_date(match.group("date"))
            if parsed is not None:
                found.append((parsed, match))
    if not found or len({value for value, _ in found}) != 1:
        return None
    value, match = found[0]
    return _LocatedValue(value, match.group(0), match.start(), match.end())


def _service_from_ranges(
    source: str,
    ranges: Sequence[tuple[int, int]],
) -> _LocatedValue | None:
    found: list[_LocatedValue] = []
    for left, right in ranges:
        located = _service_from_text(source[left:right])
        if located is not None:
            found.append(
                _LocatedValue(
                    located.value,
                    located.quote,
                    left + (located.start or 0),
                    left + (located.end or 0),
                )
            )
    if not found or len({item.value for item in found}) != 1:
        return None
    return found[0]


def _find_quote_in_ranges(
    source: str,
    quote: str,
    field_name: str,
    ranges: Sequence[tuple[int, int]],
    *,
    ignore_whitespace: bool = False,
) -> Evidence | None:
    for left, right in ranges:
        region = source[left:right]
        located = (
            _find_quote_ignoring_whitespace(region, quote, field_name)
            if ignore_whitespace
            else _find_quote(region, quote, field_name)
        )
        if located is not None:
            if located.char_start is not None:
                located.char_start += left
            if located.char_end is not None:
                located.char_end += left
            return located
    return None


def _find_service_evidence_in_ranges(
    source: str,
    value: str,
    ranges: Sequence[tuple[int, int]],
) -> Evidence | None:
    for left, right in ranges:
        located = _find_service_evidence(source[left:right], value)
        if located is not None:
            if located.char_start is not None:
                located.char_start += left
            if located.char_end is not None:
                located.char_end += left
            return located
    return None


def _find_event_service_evidence(
    source: str,
    value: str,
    ranges: Sequence[tuple[int, int]],
) -> Evidence | None:
    return _find_service_evidence_in_ranges(source, value, ranges) or (
        _find_service_evidence_in_ranges(
            source,
            value,
            _safe_preface_ranges(source, ranges),
        )
    )


def _action_match_is_negated(
    action: str,
    value: str,
    match: re.Match[str],
) -> bool:
    context = value[max(0, match.start() - 40) : min(len(value), match.end() + 40)]
    if action == "终止":
        return bool(_NEGATED_TERMINATION_PATTERN.search(context))
    if action == "调整":
        return bool(_NEGATED_ADJUSTMENT_PATTERN.search(context))
    if _NEGATED_ADD_EVIDENCE_PATTERN.search(context):
        return True
    # ``继续为基金提供...`` and similar continuity wording is a state
    # description, not establishment of a new service relationship.
    if "提供" in re.sub(r"\s+", "", match.group(0)):
        context = value[max(0, match.start() - 180) : match.end()]
        return bool(_CONTINUING_SERVICE_PATTERN.search(context))
    return False


def _action_match_is_service_related(
    action: str,
    value: str,
    match: re.Match[str],
) -> bool:
    context_left = max(0, match.start() - 180)
    context_right = min(len(value), match.end() + 360)
    context = value[context_left:context_right]
    services = list(SERVICE_PATTERN.finditer(context))
    if not services:
        return False
    if action == "新增" and "提供" in re.sub(r"\s+", "", match.group(0)):
        return True

    relative_action_start = match.start() - context_left
    relative_action_end = match.end() - context_left
    for service in services:
        low = min(relative_action_end, service.start())
        high = max(relative_action_start, service.end())
        bridge = context[low:high]
        if len(re.sub(r"\s+", "", bridge)) > 260:
            continue
        if action == "终止" and re.search(
            r"(?:新\s*增|选\s*定|指\s*定|调\s*整|变\s*更)", bridge
        ):
            continue
        if action == "调整" and re.search(
            r"(?:但|而|同\s*时).{0,40}?(?:继\s*续|仍\s*然?|持\s*续)",
            bridge,
            re.S,
        ):
            continue
        return True
    return False


def _action_evidence_from_text(value: str) -> tuple[str, re.Match[str]] | None:
    """Infer one action from a clause, giving explicit negative actions priority.

    ``终止为…提供主做市服务`` is a termination, not an implicit addition.
    Likewise, adjustment wording wins over a nested/continuing service phrase.
    """

    matches: dict[str, list[re.Match[str]]] = {}
    for action, pattern in _ACTION_EVIDENCE_PATTERNS.items():
        matches[action] = [
            match
            for match in pattern.finditer(value)
            if not _action_match_is_negated(action, value, match)
            and _action_match_is_service_related(action, value, match)
        ]

    # A grade transition can also say that the provider no longer holds the
    # old tier.  The business event is still an adjustment, not a termination.
    if matches["调整"] and SERVICE_TRANSITION_PATTERN.search(value):
        return "调整", matches["调整"][0]
    for action in ("终止", "调整"):
        if matches[action]:
            return action, matches[action][0]
    if matches["新增"]:
        return "新增", matches["新增"][0]
    return None


def _find_action_evidence(
    source: str,
    action: str,
    ranges: Sequence[tuple[int, int]],
) -> Evidence | None:
    """Find action evidence locally, or from an unambiguous shared preface."""

    local: list[tuple[str, re.Match[str], int]] = []
    for left, right in ranges:
        inferred = _action_evidence_from_text(source[left:right])
        if inferred:
            local.append((inferred[0], inferred[1], left))
    if local:
        if {item[0] for item in local} != {action}:
            return None
        _, match, offset = local[0]
        return Evidence(
            "action",
            match.group(0),
            char_start=offset + match.start(),
            char_end=offset + match.end(),
        )

    # Only a direct pre-event preface or a structurally marked list preface may
    # donate an action.  Never scan later/unrelated events in the whole source.
    preface_matches: list[tuple[str, re.Match[str], int]] = []
    for left, right in _safe_preface_ranges(source, ranges):
        inferred = _action_evidence_from_text(source[left:right])
        if inferred:
            preface_matches.append((inferred[0], inferred[1], left))
    if not preface_matches or {item[0] for item in preface_matches} != {action}:
        return None
    _, match, offset = preface_matches[0]
    return Evidence(
        "action",
        match.group(0),
        char_start=offset + match.start(),
        char_end=offset + match.end(),
    )


def _find_effective_date_evidence(
    source: str,
    expected: date,
    ranges: Sequence[tuple[int, int]],
) -> Evidence | None:
    """Find an event-local date, or one unambiguous shared effective date."""

    matches = [
        (match, _parse_date(match.group("date")))
        for match in EFFECTIVE_DATE_PATTERN.finditer(source)
    ]
    local = [
        (match, parsed)
        for match, parsed in matches
        if any(left <= match.start() and match.end() <= right for left, right in ranges)
    ]
    if local:
        if {parsed for _, parsed in local if parsed is not None} != {expected}:
            return None
        match = local[0][0]
    else:
        prefaces = _safe_preface_ranges(source, ranges)
        shared = [
            (match, parsed)
            for match, parsed in matches
            if any(left <= match.start() and match.end() <= right for left, right in prefaces)
        ]
        if {parsed for _, parsed in shared if parsed is not None} != {expected}:
            return None
        match = next((item for item, parsed in shared if parsed == expected), None)
        if match is None:
            return None
    return Evidence(
        "effective_date",
        match.group(0),
        char_start=match.start(),
        char_end=match.end(),
    )


def _clean_company(raw: str) -> str:
    value = raw.strip("，,。；;：:、 \t\n")
    # COMPANY_RUN_PATTERN intentionally has a permissive left side.  Trim only
    # structural prefixes and conjunctions, never suffixes or internal spaces.
    markers = (
        "新增", "选定", "终止", "调整", "变更", "停止", "不再", "关于", "本公司", "决定", "指定",
        "同意", "以及", "及", "和", "由", "经",
    )
    best = -1
    best_len = 0
    for marker in markers:
        index = value.rfind(marker)
        if index >= best:
            best = index
            best_len = len(marker)
    if best >= 0:
        value = value[best + best_len :]
    return re.sub(r"\s+", "", value.strip("，,。；;：:、 \t\n"))


def _companies_from_window(window: str, offset: int) -> list[_LocatedValue]:
    found: list[_LocatedValue] = []
    for match in COMPANY_RUN_PATTERN.finditer(window):
        name = _clean_company(match.group(0))
        if len(name) < 4 or not COMPANY_SUFFIX_PATTERN.search(name):
            continue
        relative = match.group(0).rfind(name)
        start = offset + match.start() + max(relative, 0)
        raw_quote = match.group(0)[max(relative, 0) :]
        located = _LocatedValue(name, raw_quote, start, offset + match.end())
        if _normalise_identity(name) not in {_normalise_identity(item.value) for item in found}:
            found.append(located)
    return found


def _companies_after_code_in_list(
    source: str,
    code_match: re.Match[str],
    window_right: int,
    next_code_start: int | None,
) -> list[_LocatedValue]:
    """Extract providers from SZSE's ``fund(code): provider`` list form.

    The normal templates introduce a provider before the fund code.  Some
    manager notices instead put the fund on a numbered line and list one or
    more providers after its colon, often on the next line.  Requiring that
    colon and stopping at the next numbered item keeps this reverse lookup
    local to its fund and avoids pulling a company from surrounding prose.
    """

    suffix = source[code_match.end() : window_right]
    separator = re.match(r"\s*[：:]\s*", suffix)
    if separator is None:
        return []

    start = code_match.end() + separator.end()
    end = window_right
    if next_code_start is not None:
        end = min(end, next_code_start)
    tail = source[start:end]
    boundaries = [
        match.start()
        for pattern in (
            r"(?:^|\n)\s*\d+\s*[.、．]\s*",
            r"(?:^|\n)\s*[（(][一二三四五六七八九十]+[）)]\s*",
            r"(?:^|\n)\s*特此公告",
        )
        if (match := re.search(pattern, tail)) is not None
    ]
    if boundaries:
        end = start + min(boundaries)
    if end <= start:
        return []
    return _companies_from_window(source[start:end], start)


def _security_name(window: str, code_match_start: int, offset: int) -> _LocatedValue | None:
    prefix = window[:code_match_start]
    # The security name is normally immediately before （基金代码：xxxxxx）.
    boundary = max(prefix.rfind(mark) for mark in ("为", "：", ":", "，", ",", "；", ";", "、"))
    raw_quote = prefix[boundary + 1 :].strip(" （(，,。；;：:\t\n")
    raw_quote = re.sub(r"^\d+\s*[.、．]\s*", "", raw_quote).strip()
    raw_quote = re.sub(r"^(?:将|向|给)", "", raw_quote).strip()
    display = re.sub(r"\s+", "", raw_quote)
    if not display or COMPANY_SUFFIX_PATTERN.search(display) or len(display) > 100:
        return None
    start_in_window = prefix.rfind(raw_quote)
    start = offset + start_in_window
    return _LocatedValue(display, raw_quote, start, start + len(raw_quote))


def _event_evidence(event: MarketMakingEvent, field_name: str) -> list[Evidence]:
    return [item for item in event.evidence if item.field_name == field_name]


def _deduplicate(events: Iterable[MarketMakingEvent]) -> list[MarketMakingEvent]:
    result: list[MarketMakingEvent] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for event in events:
        key = (
            _normalise_identity(event.market_maker),
            event.security_code,
            event.effective_date.isoformat() if event.effective_date else "",
            event.action,
            service_class(event.service_type_raw),
        )
        if key not in seen:
            seen.add(key)
            result.append(event)
    return result


class RuleExtractor:
    """Deterministic extractor for common SSE and SZSE fund templates."""

    def __init__(self) -> None:
        self.rejection_reasons: list[str] = []

    def extract(self, parsed: ParsedAnnouncement) -> list[MarketMakingEvent]:
        self.rejection_reasons = []
        candidate = parsed.candidate
        source = _normalise_source(parsed.text)
        if not is_candidate(candidate, source):
            self.rejection_reasons.append("公告未通过ETF/上市基金做市候选过滤")
            return []

        title = _normalise_source(candidate.title)
        title_action = _action_from_text(title)
        title_service = _service_from_text(title)

        events: list[MarketMakingEvent] = []
        code_matches = list(CODE_PATTERN.finditer(source))
        source_for_offsets = source
        if not code_matches:
            # Some parsers may omit a one-line title; use it as a last resort.
            source_for_offsets = title
            code_matches = list(CODE_PATTERN.finditer(title))
        if not code_matches:
            self.rejection_reasons.append("正文和标题中均未找到可识别的六位证券代码")

        for code_index, code_match in enumerate(code_matches):
            code = code_match.group("code")
            left, right = _event_window(source_for_offsets, code_match.start(), code_match.end())
            window = source_for_offsets[left:right]
            local_code_start = code_match.start() - left

            companies = _companies_from_window(window, left)
            # Never cross-pair a company that first appears after this code.
            # For a compact SZSE form such as
            # ``新增A为甲(代码1)、B为乙(代码2)`` the second code owns companies
            # introduced after the preceding code.  Conversely,
            # ``新增A、B为甲(代码1)`` intentionally keeps both companies.
            companies_before_code = [
                item for item in companies if (item.end or 0) <= code_match.start()
            ]
            next_code_start = (
                code_matches[code_index + 1].start()
                if code_index + 1 < len(code_matches)
                else None
            )
            companies_after_code = _companies_after_code_in_list(
                source_for_offsets,
                code_match,
                right,
                next_code_start,
            )
            # A colon-delimited post-code list is more specific than any
            # company mentioned in the clause preamble.  Otherwise preserve
            # the established company-before-code behaviour.
            companies = companies_after_code or companies_before_code
            previous_code_ends = [
                item.end()
                for item in code_matches
                if left <= item.start() < code_match.start()
            ]
            if previous_code_ends and not companies_after_code:
                previous_end = max(previous_code_ends)
                recently_introduced = [item for item in companies if (item.start or 0) >= previous_end]
                if recently_introduced:
                    companies = recently_introduced
            if not companies:
                self.rejection_reasons.append(
                    f"证券代码{code}附近未找到可与该基金配对的证券/金融公司"
                )
                continue
            event_ranges = [(left, right)]
            preface_ranges = _safe_preface_ranges(source_for_offsets, event_ranges)

            local_action = _action_from_text(window)
            action = local_action
            action_origin = "local" if action is not None else ""
            if action is None:
                action = _action_from_ranges(source_for_offsets, preface_ranges)
                action_origin = "preface" if action is not None else ""
            if action is None and len(code_matches) == 1 and title_action is not None:
                action = title_action
                action_origin = "title"

            local_service = _service_from_text(window)
            service = local_service
            service_origin = "local" if service is not None else ""
            if service is None:
                service = _service_from_ranges(source_for_offsets, preface_ranges)
                service_origin = "preface" if service is not None else ""
            if service is None and len(code_matches) == 1 and title_service is not None:
                service = title_service
                service_origin = "title"

            local_effective = _effective_date_from_text(window)
            effective = local_effective
            effective_origin = "local" if effective is not None else ""
            if effective is None:
                effective = _effective_date_from_ranges(source_for_offsets, preface_ranges)
                effective_origin = "preface" if effective is not None else ""
            security_name = _security_name(window, local_code_start, left)

            for company in companies:
                warnings: list[str] = []
                evidence = [
                    _evidence("market_maker", company),
                    Evidence(
                        "security_code",
                        code_match.group(0),
                        char_start=code_match.start(),
                        char_end=code_match.end(),
                    ),
                ]
                if security_name:
                    evidence.append(_evidence("security_name", security_name))
                if effective:
                    item = _evidence("effective_date", effective)
                    if effective_origin == "local" and item.char_start is not None:
                        item.char_start += left
                        item.char_end = (item.char_end or 0) + left
                    evidence.append(item)
                else:
                    warnings.append("未找到有明确‘自/从/于……起’证据的生效日期")
                if action:
                    item = _evidence("action", action)
                    if action_origin == "title":
                        item.char_start = item.char_end = None
                    elif action_origin == "local" and item.char_start is not None:
                        item.char_start += left
                        item.char_end = (item.char_end or 0) + left
                    evidence.append(item)
                else:
                    warnings.append("未能确定新增、终止或调整动作")
                if service:
                    item = _evidence("service_type_raw", service)
                    if service_origin == "title":
                        item.char_start = item.char_end = None
                    elif service_origin == "local" and item.char_start is not None:
                        item.char_start += left
                        item.char_end = (item.char_end or 0) + left
                    evidence.append(item)
                else:
                    warnings.append("未找到受支持的服务类型原文")

                if parsed.parse_warnings:
                    warnings.extend(f"文本解析警告：{item}" for item in parsed.parse_warnings)
                complete = bool(action and service and effective)
                confidence = "HIGH" if complete and not parsed.parse_warnings else "MEDIUM"
                review_status = "AUTO_ACCEPTED" if confidence == "HIGH" else "NEEDS_REVIEW"
                events.append(
                    MarketMakingEvent(
                        published_date=candidate.published_date,
                        exchange=candidate.exchange,
                        market_maker=company.value,
                        security_code=code,
                        security_name=security_name.value if security_name else "",
                        effective_date=effective.value if effective else None,
                        action=action.value if action else "",
                        service_type_raw=service.value if service else "",
                        service_class=service_class(service.value if service else ""),
                        source_url=candidate.canonical_url,
                        publisher=candidate.publisher,
                        announcement_external_id=candidate.external_id,
                        extractor="RULE",
                        confidence=confidence,
                        review_status=review_status,
                        evidence=evidence,
                        warnings=warnings,
                    )
                )
        result = _deduplicate(events)
        if not result and not self.rejection_reasons:
            self.rejection_reasons.append("规则执行完成，但未构造出业务事件")
        return result


class LLMExtractor:
    """Independent OpenAI-compatible JSON extractor using only the stdlib."""

    def __init__(self, settings: Settings, provider: LLMProviderConfig | None = None):
        self.settings = settings
        self.provider = provider or next(iter(settings.available_llm_providers), None)
        self.attempted = False
        self.succeeded = False
        self.last_warning = ""
        self.raw_response: Mapping[str, Any] | None = None
        self.raw_events: list[dict[str, Any]] = []
        self.rejected_events: list[dict[str, Any]] = []

    def extract(self, parsed: ParsedAnnouncement) -> list[MarketMakingEvent]:
        self.attempted = False
        self.succeeded = False
        self.last_warning = ""
        self.raw_response = None
        self.raw_events = []
        self.rejected_events = []
        if self.provider is None or not self.provider.available:
            return []
        if not is_candidate(parsed.candidate, parsed.text):
            return []

        self.attempted = True
        candidate = parsed.candidate
        source = _normalise_source(parsed.text)
        prompt = (
            "你是中国交易所ETF/上市基金做市公告抽取器。只抽取本公告当次明确宣布的事件；"
            "历史公告引用和招募说明书回顾不算事件。不得补充原文没有的信息，无事件时返回events空数组。"
            "每个‘基金×做市商×动作’输出一条，多基金或多券商必须正确配对。"
            "字段在原文中的先后顺序不代表业务关系；无论基金代码、日期、动作、券商和服务类型按何种"
            "顺序出现，都必须按完整语义抽取。列表中的每一家券商都要逐条输出，输出前核对原文券商"
            "数量与事件数量，不能遗漏首项、中间项或末项。"
            "action只能是新增、终止、调整：选定/指定/提供/担任/成为归为新增，"
            "终止/停止/不再归为终止，调整/变更归为调整。应根据完整语义判断动作；"
            "上交所‘为…提供主/一般做市服务’可表示新增，‘备案申请’本身不表示动作。"
            "effective_date无明确生效日期时为null；不得把裸日期、公告发布日期或落款日期"
            "当成生效日期。"
            "service_type_raw只能输出主做市服务、一般做市服务、主流动性服务商、一般流动性服务商。"
            "深交所原文仅写‘流动性服务商’时输出‘一般流动性服务商’；"
            "原文明示‘主流动性服务商’时输出主类。"
            "只返回JSON对象：{\"events\":[{\"market_maker\":\"\",\"security_code\":\"\","
            "\"security_name\":\"\",\"effective_date\":\"YYYY-MM-DD或null\",\"action\":\"\","
            "\"service_type_raw\":\"\"}]}。\n\n"
            f"交易所：{candidate.exchange}\n公告发布日期：{candidate.published_date.isoformat()}\n"
            f"标题：{candidate.title}\n正文：\n{source[:100000]}"
        )
        payload = {
            "model": self.provider.model,
            "messages": [
                {
                    "role": "system",
                    "content": "只输出一个合法JSON对象，不要解释、Markdown或代码块。",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        if self.provider.thinking is not None:
            payload["thinking"] = {"type": self.provider.thinking}
        endpoint = self.provider.api_base.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.provider.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.provider.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(str(item.get("text", "")) if isinstance(item, Mapping) else str(item) for item in content)
            data = _load_json_object(str(content))
            self.raw_response = data
            events = self._events_from_json(parsed, data)
            self.succeeded = True
            return events
        except urllib.error.HTTPError as exc:
            self.last_warning = f"大模型接口[{self.provider.name}]抽取失败：HTTP {exc.code}"
            return []
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            # Do not include endpoint URLs, response bodies or credentials in
            # warnings because warnings are persisted to SQLite and logs.
            self.last_warning = (
                f"大模型接口[{self.provider.name}]抽取失败：{type(exc).__name__}"
            )
            return []

    def _events_from_json(self, parsed: ParsedAnnouncement, data: Mapping[str, Any]) -> list[MarketMakingEvent]:
        candidate = parsed.candidate
        source = _normalise_source(parsed.text)
        raw_events = data.get("events", [])
        if not isinstance(raw_events, list):
            raise ValueError("LLM JSON中的events不是数组")
        self.raw_response = data
        self.raw_events = [dict(item) if isinstance(item, Mapping) else {"value": item} for item in raw_events]
        self.rejected_events = []
        events: list[MarketMakingEvent] = []
        for raw_index, raw in enumerate(raw_events):
            if not isinstance(raw, Mapping):
                self._reject_raw_event(raw_index, raw, "事件不是JSON对象")
                continue
            maker = str(raw.get("market_maker") or "").strip()
            code = str(raw.get("security_code") or "").strip()
            name = str(raw.get("security_name") or "").strip()
            action = _ACTION_CANONICAL.get(str(raw.get("action") or "").strip(), str(raw.get("action") or "").strip())
            service = _normalise_service_type(str(raw.get("service_type_raw") or ""))
            effective_text = str(raw.get("effective_date") or "").strip()
            effective = _parse_date(effective_text)
            relation_quote = str(raw.get("relation_evidence") or "").strip()

            # Only objective format and source-presence checks run before
            # consensus. Semantic relationships are decided by independent
            # extractor agreement rather than local clause heuristics.
            if not re.fullmatch(r"\d{6}", code):
                self._reject_raw_event(raw_index, raw, "security_code不是六位数字")
                continue
            if code not in source:
                self._reject_raw_event(raw_index, raw, "security_code无法在正文中定位")
                continue
            if not maker:
                self._reject_raw_event(raw_index, raw, "market_maker为空")
                continue
            if _find_quote_ignoring_whitespace(source, maker, "market_maker") is None:
                self._reject_raw_event(raw_index, raw, "market_maker无法在正文中定位")
                continue
            if action not in {"新增", "终止", "调整"}:
                self._reject_raw_event(raw_index, raw, "action不是新增、终止或调整")
                continue
            if service not in _SERVICE_CLASS:
                self._reject_raw_event(raw_index, raw, "service_type_raw不是受支持的服务分类")
                continue
            if effective_text and effective is None:
                self._reject_raw_event(raw_index, raw, "effective_date格式非法")
                continue

            # Quotes are retained for audit only. They never control event
            # admission or whether a field receives a consensus vote.
            evidence: list[Evidence] = (
                [Evidence("relation", relation_quote)] if relation_quote else []
            )
            raw_evidence = raw.get("evidence", [])
            if isinstance(raw_evidence, Mapping):
                raw_evidence = [
                    {"field_name": field_name, "quote": quote}
                    for field_name, quote in raw_evidence.items()
                ]
            if isinstance(raw_evidence, list):
                for item in raw_evidence:
                    if not isinstance(item, Mapping):
                        continue
                    field_name = str(item.get("field_name") or "").strip()
                    quote = str(item.get("quote") or "").strip()
                    if quote:
                        evidence.append(Evidence(field_name, quote))

            events.append(
                MarketMakingEvent(
                    published_date=candidate.published_date,
                    exchange=candidate.exchange,
                    market_maker=maker,
                    security_code=code,
                    security_name=name,
                    effective_date=effective,
                    action=action,
                    service_type_raw=service,
                    service_class=service_class(service),
                    source_url=candidate.canonical_url,
                    publisher=candidate.publisher,
                    announcement_external_id=candidate.external_id,
                    extractor=f"LLM:{self.provider.name}",
                    confidence="HIGH",
                    review_status="AUTO_ACCEPTED",
                    evidence=evidence,
                )
            )
        return _deduplicate(events)

    def _reject_raw_event(
        self,
        index: int,
        raw: Any,
        reason: str,
    ) -> None:
        self.rejected_events.append(
            {
                "index": index,
                "event": dict(raw) if isinstance(raw, Mapping) else {"value": raw},
                "reasons": [reason],
            }
        )

def _load_json_object(content: str) -> Mapping[str, Any]:
    value = content.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value)
    try:
        result = json.loads(value)
    except json.JSONDecodeError:
        start, end = value.find("{"), value.rfind("}")
        if start < 0 or end <= start:
            raise
        result = json.loads(value[start : end + 1])
    if not isinstance(result, Mapping):
        raise ValueError("LLM输出不是JSON对象")
    return result


@dataclass(slots=True)
class LLMExtractionResult:
    """Outcome of one configured endpoint, including successful empty votes."""

    provider_name: str
    succeeded: bool
    events: list[MarketMakingEvent]
    warning: str = ""
    raw_response: Mapping[str, Any] | None = None
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    rejected_events: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ExtractionOutcome:
    """Final consensus events plus append-only per-extractor audit snapshots."""

    events: list[MarketMakingEvent]
    audits: list[ExtractionAuditRecord] = field(default_factory=list)


def _field_vote_key(field_name: str, value: Any) -> str | None:
    if value in (None, ""):
        return None
    if field_name == "market_maker":
        return _normalise_identity(str(value))
    if field_name == "service_type_raw":
        return _normalise_service_type(str(value))
    if field_name == "security_name":
        return re.sub(r"\s+", "", str(value))
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _format_vote_value(value: Any) -> str:
    if value in (None, ""):
        return "<缺失>"
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _vote_summary(
    field_name: str,
    observations: Mapping[str, MarketMakingEvent],
    source_order: Sequence[str],
) -> str:
    return "；".join(
        f"{source}={_format_vote_value(getattr(observations[source], field_name))}"
        for source in source_order
        if source in observations
    )


def _event_match_score(left: MarketMakingEvent, right: MarketMakingEvent) -> int | None:
    """Score whether two independently extracted observations are one event.

    Exact maker+code is the strongest anchor.  A disagreement in one identity
    field can still be aligned for voting only when the remaining business
    fields make the match unambiguous.  Observations with both identity fields
    different are never merged.
    """

    maker_equal = _normalise_identity(left.market_maker) == _normalise_identity(right.market_maker)
    code_equal = left.security_code == right.security_code
    if not maker_equal and not code_equal:
        return None

    action_equal = bool(left.action and left.action == right.action)
    date_equal = bool(left.effective_date and left.effective_date == right.effective_date)
    raw_service_equal = bool(
        left.service_type_raw
        and _field_vote_key("service_type_raw", left.service_type_raw)
        == _field_vote_key("service_type_raw", right.service_type_raw)
    )
    left_service_class = service_class(left.service_type_raw)
    right_service_class = service_class(right.service_type_raw)
    service_class_equal = bool(
        left_service_class == right_service_class
        and left_service_class != "UNSPECIFIED"
    )
    name_equal = bool(
        left.security_name
        and _field_vote_key("security_name", left.security_name)
        == _field_vote_key("security_name", right.security_name)
    )

    if maker_equal and code_equal:
        score = 10
    elif code_equal:
        # The code anchors the fund, but the other attributes must support
        # treating a different maker as a field disagreement rather than a
        # second legitimate provider for that fund.
        corroborating = sum((action_equal, date_equal, raw_service_equal or service_class_equal))
        if corroborating < 2:
            return None
        score = 5
    else:  # maker_equal and code differs
        # One maker can serve many funds in the same notice.  A matching fund
        # name is required before allowing the code itself to be voted on.
        if not name_equal:
            return None
        score = 5

    score += 3 if action_equal else 0
    score += 2 if date_equal else 0
    score += 2 if raw_service_equal else (1 if service_class_equal else 0)
    score += 1 if name_equal else 0
    return score if score >= 8 else None


def _cluster_event_observations(
    source_order: Sequence[str],
    source_events: Mapping[str, Sequence[MarketMakingEvent]],
) -> list[dict[str, MarketMakingEvent]]:
    """Align each source's observations one-to-one without dropping events."""

    clusters: list[dict[str, MarketMakingEvent]] = []
    for source in source_order:
        events = list(source_events[source])
        if not clusters:
            clusters.extend({source: event} for event in events)
            continue

        # A source may contribute at most one observation to a cluster.  Only
        # clusters that predate this source participate; unmatched observations
        # are appended afterwards and remain available to later model sources.
        available_clusters = set(range(len(clusters)))
        remaining_events = set(range(len(events)))
        while remaining_events and available_clusters:
            proposals: list[tuple[int, int, int]] = []
            for event_index in sorted(remaining_events):
                scored: list[tuple[int, int]] = []
                for cluster_index in sorted(available_clusters):
                    scores = [
                        score
                        for existing in clusters[cluster_index].values()
                        if (score := _event_match_score(events[event_index], existing)) is not None
                    ]
                    if scores:
                        scored.append((max(scores), cluster_index))
                if not scored:
                    continue
                best_score = max(score for score, _ in scored)
                best_clusters = [
                    cluster_index
                    for score, cluster_index in scored
                    if score == best_score
                ]
                # Equal best matches are genuinely ambiguous.  Leave the
                # observation unmatched instead of cross-pairing two makers.
                if len(best_clusters) == 1:
                    proposals.append((best_score, event_index, best_clusters[0]))
            if not proposals:
                break
            # Assign the strongest unique proposal, then recompute.  This gives
            # correct action/service pairs priority when a maker+code appears
            # more than once in one announcement.
            _, event_index, cluster_index = max(
                proposals,
                key=lambda item: (item[0], -item[1], -item[2]),
            )
            clusters[cluster_index][source] = events[event_index]
            remaining_events.remove(event_index)
            available_clusters.remove(cluster_index)

        for event_index in sorted(remaining_events):
            clusters.append({source: events[event_index]})
    return clusters


def reconcile_model_results(
    rule_events: Sequence[MarketMakingEvent],
    model_results: Sequence[LLMExtractionResult],
    *,
    audit_detail: dict[str, Any] | None = None,
) -> list[MarketMakingEvent]:
    """Build a conservative field-level consensus across rule and all models.

    Failed endpoints abstain but prevent HIGH confidence.  An endpoint that
    completed successfully with no event is a real event-existence vote.  A
    strict field majority is selected; ties keep the rule value (or the first
    configured model value) and are marked LOW for review.
    """

    successful = [result for result in model_results if result.succeeded]
    source_order = ["RULE", *(result.provider_name for result in successful)]
    source_events: dict[str, Sequence[MarketMakingEvent]] = {"RULE": rule_events}
    source_events.update({result.provider_name: result.events for result in successful})

    clusters = _cluster_event_observations(source_order, source_events)

    failure_warnings = [
        result.warning or f"大模型接口[{result.provider_name}]抽取未成功完成"
        for result in model_results
        if not result.succeeded
    ]
    reconciled: list[MarketMakingEvent] = []
    successful_voter_count = 1 + len(successful)  # deterministic rules are one source
    if audit_detail is not None:
        audit_detail.clear()
        audit_detail.update(
            {
                "source_order": source_order,
                "successful_sources": [result.provider_name for result in successful],
                "failed_sources": [
                    {
                        "source": result.provider_name,
                        "warning": result.warning,
                    }
                    for result in model_results
                    if not result.succeeded
                ],
                "field_votes": [],
                "event_clusters": [],
            }
        )

    for cluster_index, observations in enumerate(clusters):
        observed_sources = [source for source in source_order if source in observations]
        base_source = "RULE" if "RULE" in observations else observed_sources[0]
        base = observations[base_source]
        warnings = list(dict.fromkeys(
            warning
            for source in observed_sources
            for warning in observations[source].warnings
        ))
        warnings.extend(item for item in failure_warnings if item not in warnings)

        # 2=HIGH, 1=MEDIUM, 0=LOW.  Every downgrade is retained for the final
        # event and therefore visible as a yellow review row when not HIGH.
        rank = 2
        if failure_warnings:
            rank = min(rank, 1)
        if any(observations[source].confidence == "LOW" for source in observed_sources):
            rank = 0
        elif any(observations[source].confidence != "HIGH" for source in observed_sources):
            rank = min(rank, 1)

        support_count = len(observed_sources)
        missing_event_sources = [
            source for source in source_order if source not in observations
        ]
        cluster_audit: dict[str, Any] | None = None
        if audit_detail is not None:
            cluster_audit = {
                "cluster_index": cluster_index,
                "observed_sources": observed_sources,
                "missing_event_sources": missing_event_sources,
                "observations": {
                    source: observations[source].as_json_dict()
                    for source in observed_sources
                },
                "field_decisions": [],
            }
        if support_count <= successful_voter_count / 2:
            rank = 0
            warnings.append(
                "事件存在性未获严格多数支持："
                f"支持={','.join(observed_sources) or '无'}；"
                f"未返回={','.join(missing_event_sources) or '无'}"
            )
        elif missing_event_sources:
            rank = min(rank, 1)
            warnings.append(
                "部分抽取器未返回该事件：" + "、".join(missing_event_sources)
            )

        chosen: dict[str, Any] = {
            field_name: getattr(base, field_name) for field_name in _KEY_FIELDS
        }
        chosen["service_type_raw"] = _normalise_service_type(
            str(chosen["service_type_raw"] or "")
        )
        for field_name in _KEY_FIELDS:
            votes: list[tuple[str, Any, str]] = []
            vote_observations: list[dict[str, Any]] = []
            for source in observed_sources:
                observation = observations[source]
                value = getattr(observation, field_name)
                key = _field_vote_key(field_name, value)
                voted = key is not None
                vote_observations.append(
                    {
                        "source": source,
                        "value": value,
                        "vote_key": key,
                        "voted": voted,
                    }
                )
                if voted:
                    votes.append((source, value, key))

            field_audit: dict[str, Any] = {
                "cluster_index": cluster_index,
                "event_anchor": {
                    "market_maker": base.market_maker,
                    "security_code": base.security_code,
                },
                "field_name": field_name,
                "observations": vote_observations,
                "selected_value": chosen[field_name],
                "selected_source": base_source,
                "decision": "no_vote",
                "strict_majority": False,
            }

            if not votes:
                if field_name in _CORE_FIELDS:
                    rank = min(rank, 1)
                    warnings.append(f"所有抽取器均缺少核心字段：{field_name}")
                if audit_detail is not None:
                    audit_detail["field_votes"].append(field_audit)
                    assert cluster_audit is not None
                    cluster_audit["field_decisions"].append(field_audit)
                continue

            groups: dict[str, list[tuple[str, Any]]] = {}
            for source, value, key in votes:
                groups.setdefault(key, []).append((source, value))
            largest = max(len(items) for items in groups.values())
            winners = [key for key, items in groups.items() if len(items) == largest]
            has_strict_majority = len(winners) == 1 and largest > len(votes) / 2

            if has_strict_majority:
                winner = winners[0]
                winning_votes = groups[winner]
                selected = next(
                    (value for source, value in winning_votes if source == "RULE"),
                    winning_votes[0][1],
                )
                if field_name == "service_type_raw":
                    selected = _normalise_service_type(str(selected))
                chosen[field_name] = selected
                selected_source = next(
                    (source for source, value in winning_votes if value == selected),
                    winning_votes[0][0],
                )
                field_audit.update(
                    {
                        "selected_value": selected,
                        "selected_source": selected_source,
                        "decision": "strict_majority",
                        "strict_majority": True,
                    }
                )
                if len(groups) > 1:
                    rank = min(rank, 1)
                    warnings.append(
                        f"字段{field_name}按严格多数选择{_format_vote_value(selected)}："
                        + _vote_summary(field_name, observations, observed_sources)
                    )
                missing_field_count = support_count - len(votes)
                if missing_field_count and field_name in _CORE_FIELDS:
                    # A single populated core-field vote cannot be treated as
                    # reliable merely because all other sources omitted it.
                    rank = 0 if len(votes) == 1 and support_count > 1 else min(rank, 1)
                    voting_sources = {source for source, _, _ in votes}
                    missing = [source for source in observed_sources if source not in voting_sources]
                    warnings.append(
                        f"部分抽取器缺少字段{field_name}：" + "、".join(missing)
                    )
            else:
                fallback_value = getattr(base, field_name)
                fallback_source = base_source
                if fallback_value in (None, ""):
                    fallback_source, fallback_value, _ = votes[0]
                if field_name == "service_type_raw":
                    fallback_value = _normalise_service_type(str(fallback_value or ""))
                chosen[field_name] = fallback_value
                field_audit.update(
                    {
                        "selected_value": fallback_value,
                        "selected_source": fallback_source,
                        "decision": "no_strict_majority_keep_base",
                    }
                )
                if field_name in _CORE_FIELDS:
                    rank = 0
                else:
                    rank = min(rank, 1)
                warnings.append(
                    f"字段{field_name}无严格多数，保留{fallback_source}值"
                    f"{_format_vote_value(fallback_value)}："
                    + _vote_summary(field_name, observations, observed_sources)
                )
            if audit_detail is not None:
                audit_detail["field_votes"].append(field_audit)
                assert cluster_audit is not None
                cluster_audit["field_decisions"].append(field_audit)

        evidence: list[Evidence] = []
        seen_evidence: set[tuple[str, str, int | None, int | None]] = set()
        for source in observed_sources:
            for item in observations[source].evidence:
                key = (item.field_name, item.quote, item.char_start, item.char_end)
                if key not in seen_evidence:
                    seen_evidence.add(key)
                    evidence.append(item)

        confidence = ("LOW", "MEDIUM", "HIGH")[rank]
        extractor = "CONSENSUS[" + ",".join(observed_sources) + "]"
        service_raw = _normalise_service_type(str(chosen["service_type_raw"] or ""))
        final_event = replace(
            base,
            market_maker=str(chosen["market_maker"] or ""),
            security_code=str(chosen["security_code"] or ""),
            security_name=str(chosen["security_name"] or ""),
            effective_date=chosen["effective_date"],
            action=str(chosen["action"] or ""),
            service_type_raw=service_raw,
            service_class=service_class(service_raw),
            extractor=extractor,
            confidence=confidence,
            review_status="AUTO_ACCEPTED" if confidence == "HIGH" else "NEEDS_REVIEW",
            evidence=evidence,
            warnings=list(dict.fromkeys(warnings)),
        )
        reconciled.append(final_event)
        if audit_detail is not None:
            assert cluster_audit is not None
            cluster_audit["final_event"] = final_event.as_json_dict()
            audit_detail["event_clusters"].append(cluster_audit)
    result = _deduplicate(reconciled)
    if audit_detail is not None:
        audit_detail["final_events"] = [event.as_json_dict() for event in result]
    return result


def reconcile_events(
    rule_events: Sequence[MarketMakingEvent],
    llm_events: Sequence[MarketMakingEvent],
    *,
    llm_attempted: bool = True,
) -> list[MarketMakingEvent]:
    """Backward-compatible two-source wrapper around multi-model consensus."""

    if not llm_attempted:
        return _deduplicate(rule_events)
    return reconcile_model_results(
        rule_events,
        [LLMExtractionResult("LLM", True, list(llm_events))],
    )


def extract_events_with_audit(
    parsed: ParsedAnnouncement,
    settings: Settings | None = None,
    *,
    run_id: int | None = None,
) -> ExtractionOutcome:
    """Run all extractors and return both final events and audit snapshots."""

    candidate = parsed.candidate
    rule_extractor = RuleExtractor()
    rule_events = rule_extractor.extract(parsed)
    rule_payload = [event.as_json_dict() for event in rule_events]
    audits = [
        ExtractionAuditRecord(
            run_id=run_id,
            exchange=candidate.exchange,
            external_id=candidate.external_id,
            extractor="RULE",
            stage="validated",
            succeeded=True,
            status="SUCCESS" if rule_events else "EMPTY",
            raw_response={"diagnostics": rule_extractor.rejection_reasons},
            raw_events=rule_payload,
            validated_events=rule_payload,
            rejection_reasons=list(rule_extractor.rejection_reasons),
            warnings=list(dict.fromkeys(
                warning for event in rule_events for warning in event.warnings
            )),
        )
    ]
    if settings is None or not settings.llm_available:
        audits.append(
            ExtractionAuditRecord(
                run_id=run_id,
                exchange=candidate.exchange,
                external_id=candidate.external_id,
                extractor="CONSENSUS",
                stage="consensus",
                succeeded=True,
                status="SUCCESS" if rule_events else "EMPTY",
                raw_response={
                    "source_order": ["RULE"],
                    "field_votes": [],
                    "note": "没有可用大模型，最终结果直接采用规则抽取",
                },
                raw_events=rule_payload,
                validated_events=rule_payload,
                rejection_reasons=[] if rule_events else ["规则未抽取到事件且没有可用大模型"],
            )
        )
        return ExtractionOutcome(rule_events, audits)

    providers = settings.available_llm_providers
    results_by_name: dict[str, LLMExtractionResult] = {}
    workers = min(len(providers), settings.llm_max_parallel_requests)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="llm-extract") as pool:
        future_to_provider = {
            pool.submit(_extract_one_provider, parsed, settings, provider): provider
            for provider in providers
        }
        for future in as_completed(future_to_provider):
            provider = future_to_provider[future]
            try:
                results_by_name[provider.name] = future.result()
            except Exception as exc:  # defensive: individual providers never stop the batch
                results_by_name[provider.name] = LLMExtractionResult(
                    provider.name,
                    False,
                    [],
                    f"大模型接口[{provider.name}]抽取失败：{type(exc).__name__}",
                )

    # Restore configuration order so tie-breaking and warnings are deterministic
    # even though requests completed concurrently.
    model_results = [results_by_name[provider.name] for provider in providers]
    for result in model_results:
        if not result.succeeded:
            _LOGGER.warning(result.warning)
        model_warnings = list(dict.fromkeys(
            ([result.warning] if result.warning else [])
            + [warning for event in result.events for warning in event.warnings]
        ))
        rejection_reasons = [
            str(reason)
            for rejected in result.rejected_events
            for reason in rejected.get("reasons", [])
        ]
        if result.warning:
            rejection_reasons.append(result.warning)
        audits.append(
            ExtractionAuditRecord(
                run_id=run_id,
                exchange=candidate.exchange,
                external_id=candidate.external_id,
                extractor=result.provider_name,
                stage="validated",
                succeeded=result.succeeded,
                status=(
                    "FAILED"
                    if not result.succeeded
                    else "SUCCESS" if result.events else "EMPTY"
                ),
                raw_response=result.raw_response,
                raw_events=result.raw_events,
                validated_events=[event.as_json_dict() for event in result.events],
                rejected_events=result.rejected_events,
                rejection_reasons=list(dict.fromkeys(rejection_reasons)),
                warnings=model_warnings,
            )
        )

    consensus_detail: dict[str, Any] = {}
    events = reconcile_model_results(
        rule_events,
        model_results,
        audit_detail=consensus_detail,
    )
    audits.append(
        ExtractionAuditRecord(
            run_id=run_id,
            exchange=candidate.exchange,
            external_id=candidate.external_id,
            extractor="CONSENSUS",
            stage="consensus",
            succeeded=True,
            status="SUCCESS" if events else "EMPTY",
            raw_response=consensus_detail,
            raw_events=[
                event.as_json_dict()
                for event in [*rule_events, *(item for result in model_results for item in result.events)]
            ],
            validated_events=[event.as_json_dict() for event in events],
            rejection_reasons=[] if events else ["规则和所有成功模型均未形成可进入共识的事件"],
            warnings=list(dict.fromkeys(
                warning for event in events for warning in event.warnings
            )),
        )
    )
    return ExtractionOutcome(events, audits)


def extract_events(parsed: ParsedAnnouncement, settings: Settings | None = None) -> list[MarketMakingEvent]:
    """Backward-compatible convenience wrapper returning final events only."""

    return extract_events_with_audit(parsed, settings).events


def _extract_one_provider(
    parsed: ParsedAnnouncement,
    settings: Settings,
    provider: LLMProviderConfig,
) -> LLMExtractionResult:
    extractor = LLMExtractor(settings, provider)
    events = extractor.extract(parsed)
    return LLMExtractionResult(
        provider_name=provider.name,
        succeeded=extractor.succeeded,
        events=events,
        warning=extractor.last_warning,
        raw_response=extractor.raw_response,
        raw_events=extractor.raw_events,
        rejected_events=extractor.rejected_events,
    )


__all__ = [
    "LLMExtractor",
    "LLMExtractionResult",
    "ExtractionOutcome",
    "RuleExtractor",
    "extract_events",
    "extract_events_with_audit",
    "is_candidate",
    "is_market_making_candidate",
    "reconcile_events",
    "reconcile_model_results",
    "service_class",
]
