from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from lxml import html

from .config import Settings
from .models import AnnouncementCandidate


SSE_SEARCH_URL = "https://query.sse.com.cn/search/getESSearchDoc.do"
SSE_GENERAL_URL = "https://www.sse.com.cn/disclosure/announcement/general/"
SSE_SITE_ROOT = "https://www.sse.com.cn"

SZSE_LIST_URL = "https://www.szse.cn/api/disc/announcement/annList"
SZSE_DETAIL_ROOT = "https://www.szse.cn/api/disc/announcement/bulletin_detail/"
SZSE_CANONICAL_ROOT = (
    "https://www.szse.cn/disclosure/listed/bulletinDetail/index.html?"
)
SZSE_DISC_ROOT = "https://disc.static.szse.cn"
SZSE_FUND_URL = "https://www.szse.cn/disclosure/fund/notice/index.html"
SZSE_CHANNELS = ("fundinfoNotice_disc", "etfNotice_disc")
SZSE_KEYWORDS = ("流动性服务", "做市服务")


class SourceError(RuntimeError):
    """Base exception for a disclosure source failure."""


class HTTPRequestError(SourceError):
    """Raised when an HTTP request remains unsuccessful after retries."""


class SourceDataError(SourceError):
    """Raised when an exchange returns incomplete or malformed data."""


@dataclass(slots=True, frozen=True)
class HTTPResponse:
    body: bytes
    status: int
    url: str
    content_type: str
    headers: Mapping[str, str]


@dataclass(slots=True, frozen=True)
class FetchedDocument:
    candidate: AnnouncementCandidate
    raw_bytes: bytes
    content_type: str

    @property
    def raw(self) -> bytes:
        """Compatibility alias for consumers that call the payload ``raw``."""

        return self.raw_bytes


class HTTPClient:
    """Small urllib client with process-local rate limiting and retries."""

    _RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

    def __init__(
        self,
        settings: Settings,
        *,
        max_retries: int = 3,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self.timeout_seconds = settings.timeout_seconds
        self.user_agent = settings.user_agent
        self.max_retries = max_retries
        self._minimum_interval = (
            1.0 / settings.requests_per_second
            if settings.requests_per_second > 0
            else 0.0
        )
        self._rate_lock = threading.Lock()
        self._last_request_at = 0.0

    def _wait_for_rate_limit(self) -> None:
        with self._rate_lock:
            now = time.monotonic()
            wait_seconds = self._minimum_interval - (now - self._last_request_at)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            self._last_request_at = time.monotonic()

    @staticmethod
    def _retry_after(headers: Mapping[str, str] | Any) -> float | None:
        value = headers.get("Retry-After") if headers is not None else None
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HTTPResponse:
        request_headers = {
            "Accept": "*/*",
            "User-Agent": self.user_agent,
        }
        if headers:
            request_headers.update(headers)

        last_error: BaseException | None = None
        for attempt in range(self.max_retries + 1):
            self._wait_for_rate_limit()
            request = Request(
                url,
                data=data,
                headers=request_headers,
                method=method.upper(),
            )
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    body = response.read()
                    status = int(response.getcode() or 200)
                    response_headers = {key: value for key, value in response.headers.items()}
                    content_type = response.headers.get_content_type()
                    final_url = response.geturl()
                if not 200 <= status < 300:
                    raise HTTPRequestError(f"HTTP {status} for {url}")
                return HTTPResponse(
                    body=body,
                    status=status,
                    url=final_url,
                    content_type=content_type,
                    headers=response_headers,
                )
            except HTTPError as exc:
                last_error = exc
                retryable = exc.code in self._RETRYABLE_STATUS
                if not retryable or attempt >= self.max_retries:
                    try:
                        detail = exc.read(500).decode("utf-8", errors="replace")
                    except Exception:
                        detail = ""
                    suffix = f": {detail}" if detail else ""
                    raise HTTPRequestError(
                        f"HTTP {exc.code} for {url}{suffix}"
                    ) from exc
                delay = self._retry_after(exc.headers)
            except (URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    raise HTTPRequestError(f"request failed for {url}: {exc}") from exc
                delay = None

            time.sleep(delay if delay is not None else 0.5 * (2**attempt))

        raise HTTPRequestError(f"request failed for {url}: {last_error}")

    def get(
        self, url: str, *, headers: Mapping[str, str] | None = None
    ) -> HTTPResponse:
        return self.request(url, headers=headers)

    def post_form(
        self,
        url: str,
        fields: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> HTTPResponse:
        request_headers = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
        if headers:
            request_headers.update(headers)
        return self.request(
            url,
            method="POST",
            data=urlencode(fields).encode("utf-8"),
            headers=request_headers,
        )

    def post_json(
        self,
        url: str,
        value: Any,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> HTTPResponse:
        request_headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
        }
        if headers:
            request_headers.update(headers)
        return self.request(
            url,
            method="POST",
            data=json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers=request_headers,
        )


def _json_object(response: HTTPResponse, source: str) -> dict[str, Any]:
    try:
        text = response.body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceDataError(f"{source} returned non-UTF-8 JSON") from exc

    # SSE responds as JSONP even when the callback parameter is omitted or null.
    first = text.find("{")
    last = text.rfind("}")
    if first < 0 or last < first:
        raise SourceDataError(f"{source} returned a non-JSON response")
    try:
        result = json.loads(text[first : last + 1])
    except json.JSONDecodeError as exc:
        raise SourceDataError(f"{source} returned malformed JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise SourceDataError(f"{source} JSON root must be an object")
    return result


def _parse_date(value: Any, field_name: str) -> date:
    if not isinstance(value, str):
        raise SourceDataError(f"missing or invalid {field_name}: {value!r}")
    match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", value)
    if not match:
        raise SourceDataError(f"invalid {field_name}: {value!r}")
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError as exc:
        raise SourceDataError(f"invalid {field_name}: {value!r}") from exc


def _clean_markup(value: Any) -> str:
    if value is None:
        return ""
    text_value = str(value)
    try:
        fragment = html.fragment_fromstring(text_value, create_parent="div")
        return "".join(fragment.itertext()).strip()
    except (ValueError, TypeError):
        return text_value.strip()


def _normalise_sse_url(value: str) -> str:
    absolute = urljoin(SSE_SITE_ROOT, value)
    parts = urlsplit(absolute)
    path = re.sub(r"/{2,}", "/", parts.path)
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _extend_map(item: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    extend = item.get("extend", [])
    if not isinstance(extend, list):
        raise SourceDataError("SSE result field 'extend' must be a list")
    for entry in extend:
        if isinstance(entry, dict) and entry.get("name"):
            result[str(entry["name"])] = entry.get("value")
    return result


class SSESource:
    exchange = "SSE"
    page_size = 100

    def __init__(self, settings: Settings, client: HTTPClient | None = None) -> None:
        self.settings = settings
        self.client = client or HTTPClient(settings)

    def _search_page(self, target_date: date, page: int) -> dict[str, Any]:
        date_text = target_date.isoformat()
        fields = {
            "keyword": "做市服务",
            "spaceId": "3",
            "siteName": "sse",
            "keywordPosition": "title,paper_content",
            "page": str(page),
            "limit": str(self.page_size),
            "publishTimeStart": f"{date_text} 00:00:00",
            "publishTimeEnd": f"{date_text} 23:59:59",
            "channelId": "10001",
            "channelCode": "12635",
            "searchMode": "preciseMulti",
            "orderByDirection": "DESC",
            "orderByKey": "score",
        }
        response = self.client.post_form(
            SSE_SEARCH_URL,
            fields,
            headers={"Referer": "https://www.sse.com.cn/home/search/"},
        )
        payload = _json_object(response, "SSE search")
        if str(payload.get("code")) != "0":
            message = payload.get("message") or payload.get("error") or payload
            raise SourceDataError(f"SSE search rejected the query: {message}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise SourceDataError("SSE search response has no data object")
        return data

    def list_for_date(self, target_date: date) -> list[AnnouncementCandidate]:
        candidates: dict[str, AnnouncementCandidate] = {}
        page = 0
        total_pages: int | None = None
        total_size: int | None = None
        raw_result_count = 0

        while total_pages is None or page < total_pages:
            data = self._search_page(target_date, page)
            try:
                response_page = int(data["page"])
                response_limit = int(data["limit"])
                response_total_pages = int(data["totalPage"])
                response_total_size = int(data["totalSize"])
            except (KeyError, TypeError, ValueError) as exc:
                raise SourceDataError("SSE search response has invalid pagination") from exc
            if response_page != page or response_limit <= 0:
                raise SourceDataError(
                    f"SSE pagination mismatch: requested page {page}, got {response_page}"
                )
            if total_pages is None:
                total_pages = response_total_pages
                total_size = response_total_size
            elif total_pages != response_total_pages or total_size != response_total_size:
                raise SourceDataError("SSE result count changed while paging")

            items = data.get("knowledgeList")
            if items is None and response_total_size == 0:
                items = []
            if not isinstance(items, list):
                raise SourceDataError("SSE search response has invalid knowledgeList")
            if page < response_total_pages and not items:
                raise SourceDataError(f"SSE page {page} is unexpectedly empty")
            raw_result_count += len(items)

            for item in items:
                if not isinstance(item, dict):
                    raise SourceDataError("SSE result item must be an object")
                published_date = _parse_date(item.get("createTime"), "SSE createTime")
                if published_date != target_date:
                    raise SourceDataError(
                        f"SSE returned {published_date} for requested date {target_date}"
                    )
                extensions = _extend_map(item)
                relative_url = extensions.get("CURL") or item.get("url")
                if not relative_url:
                    raise SourceDataError("SSE result has no CURL")
                canonical_url = _normalise_sse_url(str(relative_url))
                external_id = str(item.get("documentId") or item.get("id") or "").strip()
                if not external_id:
                    raise SourceDataError("SSE result has no stable identifier")
                candidate = AnnouncementCandidate(
                    exchange=self.exchange,
                    external_id=external_id,
                    canonical_url=canonical_url,
                    title=_clean_markup(item.get("title")),
                    published_date=published_date,
                    publisher="上海证券交易所",
                    source_kind="html",
                    detail_url=canonical_url,
                    content_type="text/html",
                    metadata={
                        "search_id": item.get("id"),
                        "channel_code": extensions.get("CCHANNELCODE"),
                        "update_time": item.get("updateTime"),
                    },
                )
                existing = candidates.get(external_id)
                if existing and existing.canonical_url != candidate.canonical_url:
                    raise SourceDataError(
                        f"SSE identifier {external_id} maps to multiple URLs"
                    )
                candidates[external_id] = candidate
            page += 1

        if total_size is None or total_pages is None:
            raise SourceDataError("SSE search did not return pagination")
        if raw_result_count != total_size:
            raise SourceDataError(
                f"SSE pagination incomplete: expected {total_size}, got {raw_result_count}"
            )
        return list(candidates.values())

    def fetch(self, candidate: AnnouncementCandidate) -> FetchedDocument:
        if candidate.exchange != self.exchange:
            raise ValueError(f"SSESource cannot fetch {candidate.exchange} candidate")
        url = candidate.detail_url or candidate.canonical_url
        response = self.client.get(url, headers={"Referer": SSE_GENERAL_URL})
        if not response.body:
            raise SourceDataError(f"SSE detail is empty: {url}")
        try:
            html.fromstring(response.body)
        except (ValueError, TypeError) as exc:
            raise SourceDataError(f"SSE detail is not valid HTML: {url}") from exc
        fetched_candidate = replace(candidate, content_type="text/html")
        return FetchedDocument(fetched_candidate, response.body, "text/html")


def _infer_szse_publisher(title: str) -> str:
    title = title.split("：", 1)[-1]
    patterns = (
        r"关于(.{2,80}?(?:基金管理|资产管理)有限公司)",
        r"关于(.{2,80}?证券股份有限公司)",
    )
    for pattern in patterns:
        match = re.search(pattern, title)
        if match:
            return match.group(1).strip()
    return "基金管理人"


def _szse_attachment_url(path: Any) -> str | None:
    if not path:
        return None
    value = str(path).strip()
    if value.startswith(("https://", "http://")):
        return value
    return urljoin(SZSE_DISC_ROOT, value)


class SZSESource:
    exchange = "SZSE"
    page_size = 50

    def __init__(self, settings: Settings, client: HTTPClient | None = None) -> None:
        self.settings = settings
        self.client = client or HTTPClient(settings)

    def _list_page(
        self,
        target_date: date,
        channel: str,
        keyword: str,
        page: int,
    ) -> dict[str, Any]:
        date_text = target_date.isoformat()
        body = {
            "seDate": [date_text, date_text],
            # The API treats multiple channel/keyword values as a conjunction,
            # so each channel-keyword stream is paged independently.
            "channelCode": [channel],
            "searchKey": [keyword],
            "pageSize": self.page_size,
            "pageNum": page,
        }
        response = self.client.post_json(
            SZSE_LIST_URL,
            body,
            headers={"Referer": SZSE_FUND_URL},
        )
        return _json_object(response, "SZSE announcement list")

    @staticmethod
    def _candidate_from_item(
        item: Mapping[str, Any],
        target_date: date,
        channel: str,
        keyword: str,
    ) -> AnnouncementCandidate:
        external_id = str(item.get("id") or "").strip()
        if not external_id:
            raise SourceDataError("SZSE announcement has no id")
        published_date = _parse_date(item.get("publishTime"), "SZSE publishTime")
        if published_date != target_date:
            raise SourceDataError(
                f"SZSE returned {published_date} for requested date {target_date}"
            )
        title = _clean_markup(item.get("title"))
        canonical_url = f"{SZSE_CANONICAL_ROOT}{external_id}"
        attachment_url = _szse_attachment_url(item.get("attachPath"))
        return AnnouncementCandidate(
            exchange="SZSE",
            external_id=external_id,
            canonical_url=canonical_url,
            title=title,
            published_date=published_date,
            publisher=_infer_szse_publisher(title),
            source_kind="pdf",
            detail_url=f"{SZSE_DETAIL_ROOT}{external_id}",
            attachment_url=attachment_url,
            content_type="application/pdf",
            metadata={
                "ann_id": item.get("annId"),
                "attach_format": item.get("attachFormat"),
                "attach_size_kb": item.get("attachSize"),
                "security_codes": item.get("secCode") or [],
                "security_names": item.get("secName") or [],
                "matched_channels": [channel],
                "matched_keywords": [keyword],
            },
        )

    def list_for_date(self, target_date: date) -> list[AnnouncementCandidate]:
        candidates: dict[str, AnnouncementCandidate] = {}

        for channel in SZSE_CHANNELS:
            for keyword in SZSE_KEYWORDS:
                page = 1
                total_count: int | None = None
                stream_count = 0
                while total_count is None or stream_count < total_count:
                    payload = self._list_page(target_date, channel, keyword, page)
                    try:
                        current_total = int(payload["announceCount"])
                    except (KeyError, TypeError, ValueError) as exc:
                        raise SourceDataError(
                            "SZSE list response has invalid announceCount"
                        ) from exc
                    if total_count is None:
                        total_count = current_total
                    elif total_count != current_total:
                        raise SourceDataError("SZSE result count changed while paging")

                    items = payload.get("data")
                    if not isinstance(items, list):
                        raise SourceDataError("SZSE list response has invalid data")
                    if stream_count < current_total and not items:
                        raise SourceDataError(
                            f"SZSE page {page} is unexpectedly empty for "
                            f"{channel}/{keyword}"
                        )
                    if len(items) > self.page_size:
                        raise SourceDataError("SZSE returned more than the requested page size")

                    for item in items:
                        if not isinstance(item, dict):
                            raise SourceDataError("SZSE list item must be an object")
                        candidate = self._candidate_from_item(
                            item, target_date, channel, keyword
                        )
                        existing = candidates.get(candidate.external_id)
                        if existing is None:
                            candidates[candidate.external_id] = candidate
                        else:
                            if existing.canonical_url != candidate.canonical_url:
                                raise SourceDataError(
                                    f"SZSE identifier {candidate.external_id} maps to "
                                    "multiple URLs"
                                )
                            channels = existing.metadata.setdefault("matched_channels", [])
                            keywords = existing.metadata.setdefault("matched_keywords", [])
                            if channel not in channels:
                                channels.append(channel)
                            if keyword not in keywords:
                                keywords.append(keyword)
                    stream_count += len(items)
                    page += 1

                if total_count is None or stream_count != total_count:
                    raise SourceDataError(
                        f"SZSE pagination incomplete for {channel}/{keyword}: "
                        f"expected {total_count}, got {stream_count}"
                    )

        return list(candidates.values())

    def fetch(self, candidate: AnnouncementCandidate) -> FetchedDocument:
        if candidate.exchange != self.exchange:
            raise ValueError(f"SZSESource cannot fetch {candidate.exchange} candidate")
        detail_url = candidate.detail_url or f"{SZSE_DETAIL_ROOT}{candidate.external_id}"
        detail_response = self.client.get(
            detail_url, headers={"Referer": candidate.canonical_url}
        )
        detail = _json_object(detail_response, "SZSE announcement detail")
        listed_ann_id = candidate.metadata.get("ann_id")
        detail_ann_id = detail.get("annId")
        if (
            listed_ann_id is not None
            and detail_ann_id is not None
            and str(listed_ann_id) != str(detail_ann_id)
        ):
            raise SourceDataError(
                f"SZSE detail annId {detail_ann_id} does not match list annId "
                f"{listed_ann_id}"
            )
        detail_date = _parse_date(detail.get("publishTime"), "SZSE detail publishTime")
        if detail_date != candidate.published_date:
            raise SourceDataError(
                f"SZSE detail date {detail_date} does not match list date "
                f"{candidate.published_date}"
            )
        attachment_url = _szse_attachment_url(detail.get("attachPath"))
        if not attachment_url:
            raise SourceDataError(
                f"SZSE detail has no PDF attachment: {candidate.external_id}"
            )
        attachment_response = self.client.get(
            attachment_url, headers={"Referer": candidate.canonical_url}
        )
        raw = attachment_response.body
        if not raw.lstrip().startswith(b"%PDF-"):
            raise SourceDataError(
                f"SZSE attachment is not a PDF: {candidate.external_id}"
            )

        title = _clean_markup(detail.get("title")) or candidate.title
        metadata = dict(candidate.metadata)
        metadata.update(
            {
                "ann_id": detail.get("annId") or metadata.get("ann_id"),
                "attach_format": detail.get("attachFormat"),
                "attach_size_kb": detail.get("attachSize"),
                "security_codes": detail.get("secCode") or metadata.get("security_codes", []),
                "security_names": detail.get("secName") or metadata.get("security_names", []),
                "detail_channel_codes": detail.get("channelCode") or [],
            }
        )
        fetched_candidate = replace(
            candidate,
            title=title,
            publisher=_infer_szse_publisher(title),
            attachment_url=attachment_url,
            content_type="application/pdf",
            metadata=metadata,
        )
        return FetchedDocument(fetched_candidate, raw, "application/pdf")


# Friendly alias for callers that prefer conventional CamelCase.
HttpClient = HTTPClient


__all__ = [
    "FetchedDocument",
    "HTTPClient",
    "HTTPRequestError",
    "HttpClient",
    "SSESource",
    "SZSESource",
    "SourceDataError",
    "SourceError",
]
