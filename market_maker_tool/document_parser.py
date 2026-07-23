from __future__ import annotations

import io
import json
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

from lxml import etree, html
from pypdf import PdfReader

from .config import Settings
from .models import AnnouncementCandidate, ParsedAnnouncement
from .storage import sha256_bytes


def parse_and_store(
    candidate: AnnouncementCandidate,
    raw_content: bytes,
    content_type: str,
    settings: Settings,
) -> ParsedAnnouncement:
    suffix = choose_suffix(candidate, content_type)
    safe_id = re.sub(r"[^0-9A-Za-z_.-]+", "_", candidate.external_id)[:120]
    content_hash = sha256_bytes(raw_content)
    version_tag = content_hash[:12]
    date_dir = candidate.published_date.isoformat()
    raw_dir = settings.raw_dir / candidate.exchange.lower() / date_dir
    text_dir = settings.text_dir / candidate.exchange.lower() / date_dir
    raw_dir.mkdir(parents=True, exist_ok=True)
    text_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"{safe_id}_{version_tag}{suffix}"
    text_path = text_dir / f"{safe_id}_{version_tag}.txt"
    meta_path = text_dir / f"{safe_id}_{version_tag}.meta.json"
    raw_path.write_bytes(raw_content)

    warnings: list[str] = []
    if suffix == ".pdf" or raw_content.startswith(b"%PDF"):
        text, parser, pdf_warnings = extract_pdf_text(raw_content, raw_path, settings)
        warnings.extend(pdf_warnings)
    else:
        text = extract_html_text(raw_content, candidate.exchange)
        parser = "lxml-html"

    text = normalize_text(text)
    if not text:
        warnings.append("未提取到正文文本")
    text_path.write_text(text, encoding="utf-8")
    meta_path.write_text(
        json.dumps(
            {
                "exchange": candidate.exchange,
                "external_id": candidate.external_id,
                "canonical_url": candidate.canonical_url,
                "title": candidate.title,
                "published_date": candidate.published_date.isoformat(),
                "parser": parser,
                "warnings": warnings,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return ParsedAnnouncement(
        candidate=candidate,
        text=text,
        raw_path=str(raw_path),
        text_path=str(text_path),
        raw_sha256=content_hash,
        parser=parser,
        parse_warnings=warnings,
    )


def choose_suffix(candidate: AnnouncementCandidate, content_type: str) -> str:
    joined = " ".join(
        item or "" for item in (content_type, candidate.content_type, candidate.attachment_url, candidate.detail_url)
    ).lower()
    return ".pdf" if "pdf" in joined else ".html"


def extract_html_text(content: bytes, exchange: str) -> str:
    parser = html.HTMLParser(encoding="utf-8", recover=True)
    document = html.fromstring(content, parser=parser)
    etree.strip_elements(document, "script", "style", "noscript", with_tail=False)
    selectors = (
        ("sse", "//*[contains(concat(' ', normalize-space(@class), ' '), ' allZoom ')]"),
        ("sse", "//*[contains(concat(' ', normalize-space(@class), ' '), ' article-infor ')]"),
        ("szse", "//*[contains(concat(' ', normalize-space(@class), ' '), ' bd_body ')]"),
    )
    nodes = []
    for source, xpath in selectors:
        if source == exchange.lower():
            nodes = document.xpath(xpath)
            if nodes:
                break
    if not nodes:
        nodes = document.xpath("//body") or [document]
    return "\n".join(" ".join(node.itertext()) for node in nodes)


def extract_pdf_text(content: bytes, raw_path: Path, settings: Settings) -> tuple[str, str, list[str]]:
    warnings: list[str] = []
    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
        pages: list[str] = []
        for page_no, page in enumerate(reader.pages, start=1):
            page_text = page.extract_text() or ""
            pages.append(f"[PAGE {page_no}]\n{page_text}")
        text = "\n\n".join(pages)
    except Exception as exc:  # malformed PDFs should still be eligible for OCR
        text = ""
        warnings.append(f"PDF文本层解析失败: {exc}")

    if text_quality_ok(text):
        return text, "pypdf-text", warnings

    warnings.append("PDF文本层质量不足，尝试OCR")
    ocr_text = run_ocr(raw_path, settings, warnings)
    if ocr_text:
        return ocr_text, "ocr", warnings
    warnings.append("OCR不可用或未产生结果")
    return text, "pypdf-low-quality", warnings


def text_quality_ok(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 80:
        return False
    chinese_count = len(re.findall(r"[\u3400-\u9fff]", compact))
    replacement_count = compact.count("�")
    return chinese_count >= 25 and replacement_count / max(len(compact), 1) < 0.02


def run_ocr(raw_path: Path, settings: Settings, warnings: list[str]) -> str:
    if settings.ocr_command:
        with tempfile.TemporaryDirectory(prefix="market_maker_ocr_") as tmp:
            output_path = Path(tmp) / "ocr.txt"
            args = [
                part.replace("{input}", str(raw_path)).replace("{output}", str(output_path))
                for part in settings.ocr_command
            ]
            try:
                completed = subprocess.run(args, capture_output=True, text=True, timeout=300, check=False)
                if completed.returncode != 0:
                    warnings.append(f"OCR命令失败({completed.returncode}): {completed.stderr[-300:]}")
                    return ""
                if output_path.exists():
                    return output_path.read_text(encoding="utf-8", errors="replace")
                return completed.stdout
            except Exception as exc:
                warnings.append(f"OCR命令执行失败: {exc}")
                return ""

    # Optional Python OCR path. It is intentionally lazy so the base install remains light.
    try:
        import fitz  # type: ignore
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
    except ImportError:
        return ""

    try:
        engine = RapidOCR()
        document = fitz.open(raw_path)
        pages: list[str] = []
        for page_no, page in enumerate(document, start=1):
            pix = page.get_pixmap(matrix=fitz.Matrix(3, 3), alpha=False)
            result, _ = engine(pix.tobytes("png"))
            lines = [item[1] for item in (result or []) if len(item) >= 2]
            pages.append(f"[PAGE {page_no}]\n" + "\n".join(lines))
        return "\n\n".join(pages)
    except Exception as exc:
        warnings.append(f"RapidOCR失败: {exc}")
        return ""


def normalize_text(value: str) -> str:
    value = value.replace("\u3000", " ").replace("\xa0", " ").replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n[ \t]+", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()
