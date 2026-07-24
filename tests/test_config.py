from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from market_maker_tool.config import Settings


class ConfigTests(unittest.TestCase):
    def write_config(self, root: str, payload: dict) -> Path:
        path = Path(root) / "config.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_loads_multiple_models_only_from_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(tmp, {
                "llm": {
                    "max_parallel_requests": 2,
                    "providers": [
                        {
                            "name": "first",
                            "api_base": "https://first.example/v1",
                            "api_key": "file-key-1",
                            "model": "model-1",
                        },
                        {
                            "name": "second",
                            "api_base": "https://second.example/v1",
                            "api_key": "file-key-2",
                            "model": "model-2",
                            "timeout_seconds": 45,
                            "thinking": "disabled",
                        },
                    ],
                }
            })
            with patch.dict(os.environ, {
                "LLM_API_BASE": "https://environment-should-not-win.example/v1",
                "LLM_API_KEY": "environment-key",
                "LLM_MODEL": "environment-model",
            }):
                settings = Settings.load(root_dir=tmp, config_path=path)

            self.assertEqual([provider.name for provider in settings.llm_providers], ["first", "second"])
            self.assertEqual(settings.llm_providers[0].api_key, "file-key-1")
            self.assertEqual(settings.llm_providers[0].model, "model-1")
            self.assertEqual(settings.llm_providers[1].timeout_seconds, 45)
            self.assertEqual(settings.llm_providers[1].thinking, "disabled")
            self.assertEqual(settings.llm_max_parallel_requests, 2)
            self.assertTrue(settings.llm_available)

    def test_legacy_single_model_file_is_migrated_without_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(tmp, {
                "llm": {
                    "api_base": "https://legacy.example/v1",
                    "api_key": "legacy-file-key",
                    "model": "legacy-model",
                    "timeout_seconds": 30,
                }
            })
            settings = Settings.load(root_dir=tmp, config_path=path)
            self.assertEqual(len(settings.llm_providers), 1)
            self.assertEqual(settings.llm_providers[0].name, "default")
            self.assertEqual(settings.llm_providers[0].api_key, "legacy-file-key")
            self.assertTrue(settings.llm_available)

    def test_duplicate_provider_names_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(tmp, {
                "llm": {
                    "providers": [
                        {"name": "same", "api_base": "a", "api_key": "k", "model": "m"},
                        {"name": "same", "api_base": "b", "api_key": "k", "model": "m"},
                    ]
                }
            })
            with self.assertRaisesRegex(ValueError, "大模型接口名称重复"):
                Settings.load(root_dir=tmp, config_path=path)

    def test_incomplete_models_are_not_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(tmp, {
                "llm": {
                    "providers": [
                        {"name": "missing-key", "api_base": "a", "api_key": "", "model": "m"},
                    ]
                }
            })
            settings = Settings.load(root_dir=tmp, config_path=path)
            self.assertFalse(settings.llm_available)
            self.assertEqual(settings.available_llm_providers, [])
            self.assertEqual(settings.llm_providers[0].missing_fields, ["api_key"])

    def test_legacy_enabled_flags_cannot_disable_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(tmp, {
                "llm": {
                    "enabled": False,
                    "providers": [{
                        "name": "model",
                        "enabled": False,
                        "api_base": "https://example.test/v1",
                        "api_key": "key",
                        "model": "model",
                    }],
                }
            })
            settings = Settings.load(root_dir=tmp, config_path=path)
            self.assertTrue(settings.llm_available)
            self.assertEqual(
                [provider.name for provider in settings.available_llm_providers],
                ["model"],
            )

    def test_invalid_provider_name_is_rejected_without_echoing_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(tmp, {
                "llm": {
                    "providers": [{
                        "name": "bad\nname",
                        "api_base": "a",
                        "api_key": "k",
                        "model": "m",
                    }]
                }
            })
            with self.assertRaises(ValueError) as caught:
                Settings.load(root_dir=tmp, config_path=path)
            self.assertNotIn("bad\nname", str(caught.exception))

    def test_invalid_thinking_mode_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(tmp, {
                "llm": {
                    "providers": [{
                        "name": "model",
                        "api_base": "a",
                        "api_key": "k",
                        "model": "m",
                        "thinking": "sometimes",
                    }]
                }
            })
            with self.assertRaisesRegex(ValueError, "thinking"):
                Settings.load(root_dir=tmp, config_path=path)

    def test_config_root_must_be_an_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "根节点必须是 JSON 对象"):
                Settings.load(root_dir=tmp, config_path=path)


if __name__ == "__main__":
    unittest.main()
