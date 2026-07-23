from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class LLMProviderConfig:
    """One independently callable OpenAI-compatible model endpoint."""

    name: str
    api_base: str
    api_key: str = field(default="", repr=False)
    model: str = ""
    enabled: bool = True
    timeout_seconds: int = 90

    @property
    def available(self) -> bool:
        return self.enabled and bool(self.api_base and self.api_key and self.model)

    @property
    def missing_fields(self) -> list[str]:
        return [
            field_name
            for field_name in ("api_base", "api_key", "model")
            if not getattr(self, field_name)
        ]


@dataclass(slots=True)
class Settings:
    root_dir: Path
    data_dir: Path
    raw_dir: Path
    text_dir: Path
    report_dir: Path
    log_dir: Path
    db_path: Path
    timeout_seconds: int = 30
    requests_per_second: float = 1.0
    lookback_days: int = 3
    llm_enabled: bool = True
    llm_providers: list[LLMProviderConfig] = field(default_factory=list)
    llm_max_parallel_requests: int = 4
    ocr_command: list[str] = field(default_factory=list)
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ExchangeMarketMakerReporter/0.1"

    @classmethod
    def load(cls, root_dir: str | Path | None = None, config_path: str | Path | None = None) -> "Settings":
        root = Path(root_dir or Path.cwd()).resolve()
        values: dict[str, Any] = {}
        cfg_path = Path(config_path) if config_path else root / "config.json"
        if cfg_path.exists():
            values = json.loads(cfg_path.read_text(encoding="utf-8"))
            if not isinstance(values, dict):
                raise ValueError("配置文件根节点必须是 JSON 对象")

        data_dir = root / values.get("data_dir", "data")
        llm_cfg = values.get("llm", {})
        if not isinstance(llm_cfg, dict):
            raise ValueError("配置项 llm 必须是 JSON 对象")
        providers = _load_llm_providers(llm_cfg)
        ocr_cfg = values.get("ocr", {})
        settings = cls(
            root_dir=root,
            data_dir=data_dir,
            raw_dir=data_dir / "raw",
            text_dir=data_dir / "text",
            report_dir=root / values.get("report_dir", "reports"),
            log_dir=root / values.get("log_dir", "logs"),
            db_path=data_dir / values.get("db_name", "app.db"),
            timeout_seconds=int(values.get("timeout_seconds", 30)),
            requests_per_second=float(values.get("requests_per_second", 1.0)),
            lookback_days=int(values.get("lookback_days", 3)),
            llm_enabled=bool(llm_cfg.get("enabled", True)),
            llm_providers=providers,
            llm_max_parallel_requests=_positive_int(
                llm_cfg.get("max_parallel_requests", 4),
                "llm.max_parallel_requests",
            ),
            ocr_command=list(ocr_cfg.get("command", [])),
            user_agent=values.get("user_agent", cls.__dataclass_fields__["user_agent"].default),
        )
        settings.ensure_directories()
        return settings

    def ensure_directories(self) -> None:
        for path in (self.data_dir, self.raw_dir, self.text_dir, self.report_dir, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def llm_available(self) -> bool:
        return bool(self.available_llm_providers)

    @property
    def available_llm_providers(self) -> list[LLMProviderConfig]:
        if not self.llm_enabled:
            return []
        return [provider for provider in self.llm_providers if provider.available]


def _load_llm_providers(llm_cfg: dict[str, Any]) -> list[LLMProviderConfig]:
    """Load the new provider list, with a file-only legacy config migration.

    Environment variables are intentionally not consulted.  A former single
    model block in ``config.json`` is still accepted and becomes ``default``.
    """

    raw_providers = llm_cfg.get("providers")
    if raw_providers is None:
        legacy_fields = {"api_base", "api_key", "model", "timeout_seconds"}
        if legacy_fields.intersection(llm_cfg):
            raw_providers = [{
                "name": "default",
                "enabled": True,
                "api_base": llm_cfg.get("api_base", "https://api.openai.com/v1"),
                "api_key": llm_cfg.get("api_key", ""),
                "model": llm_cfg.get("model", ""),
                "timeout_seconds": llm_cfg.get("timeout_seconds", 90),
            }]
        else:
            raw_providers = []
    if not isinstance(raw_providers, list):
        raise ValueError("配置项 llm.providers 必须是 JSON 数组")

    providers: list[LLMProviderConfig] = []
    names: set[str] = set()
    for index, raw in enumerate(raw_providers):
        if not isinstance(raw, dict):
            raise ValueError(f"配置项 llm.providers[{index}] 必须是 JSON 对象")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError(f"配置项 llm.providers[{index}].name 不能为空")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
            raise ValueError(
                f"配置项 llm.providers[{index}].name 只能使用1至64位字母、数字、点、下划线或连字符"
            )
        name_key = name.casefold()
        if name_key == "rule":
            raise ValueError("大模型接口名称 RULE 为系统保留名称")
        if name_key in names:
            raise ValueError(f"大模型接口名称重复: {name}")
        names.add(name_key)
        providers.append(
            LLMProviderConfig(
                name=name,
                enabled=bool(raw.get("enabled", True)),
                api_base=str(raw.get("api_base") or "").strip(),
                api_key=str(raw.get("api_key") or "").strip(),
                model=str(raw.get("model") or "").strip(),
                timeout_seconds=_positive_int(
                    raw.get("timeout_seconds", 90),
                    f"llm.providers[{index}].timeout_seconds",
                ),
            )
        )
    return providers


def _positive_int(value: Any, field_name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"配置项 {field_name} 必须是正整数") from exc
    if parsed <= 0:
        raise ValueError(f"配置项 {field_name} 必须是正整数")
    return parsed
