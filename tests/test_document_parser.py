from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from market_maker_tool.config import Settings
from market_maker_tool.document_parser import extract_pdf_text


class DocumentParserTests(unittest.TestCase):
    def test_low_quality_pdf_uses_configured_ocr_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "scan.pdf"
            pdf_path.write_bytes(b"not-a-valid-pdf")
            settings = Settings.load(root_dir=root)
            settings.ocr_command = [
                sys.executable,
                "-c",
                "from pathlib import Path; Path(r'{output}').write_text('OCR识别出的流动性服务商公告正文', encoding='utf-8')",
            ]
            text, parser, warnings = extract_pdf_text(pdf_path.read_bytes(), pdf_path, settings)
            self.assertEqual(parser, "ocr")
            self.assertIn("流动性服务商", text)
            self.assertTrue(any("OCR" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
