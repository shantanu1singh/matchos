"""Model selection shared by the decision and reasoning adapters."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

ENV_DIR = Path(__file__).resolve().parent.parent


def load_env_files(directory: Path | None = None) -> None:
    """Load project defaults, then local overrides, preserving process variables."""
    directory = directory if directory is not None else ENV_DIR
    values = {
        **dotenv_values(directory / ".env"),
        **dotenv_values(directory / ".env.local"),
    }
    for name, value in values.items():
        if value is not None:
            os.environ.setdefault(name, value)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, "").strip() or default


def _timeout(name: str, default: str) -> float:
    value = float(_env(name, default))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


@dataclass(frozen=True)
class LLMConfig:
    decision_provider: str = "cloudflare"
    decision_model: str = "convaiinnovations/laya-free"
    decision_base_url: str = "https://ai-gateway.vercel.sh/v1"
    gateway_api_key: str = field(default="", repr=False)
    decision_timeout_s: float = 10.0
    cloudflare_model: str = "@cf/cloudflare/clef-flash"
    cloudflare_account_id: str = ""
    cloudflare_api_token: str = field(default="", repr=False)
    reasoning_model: str = "vertex_ai/gemini-3.8-flash"
    reasoning_api_key: str = field(default="", repr=False)
    reasoning_base_url: str = ""
    reasoning_timeout_s: float = 15.0
    vertex_project: str = ""
    vertex_location: str = "global"

    @classmethod
    def from_env(cls) -> LLMConfig:
        load_env_files()
        provider = _env("DECISION_PROVIDER", "cloudflare").lower()
        if provider not in {"cloudflare", "vercel", "local"}:
            raise ValueError("DECISION_PROVIDER must be cloudflare, vercel, or local")
        cloudflare_model = _env("CLOUDFLARE_MODEL", "@cf/cloudflare/clef-flash")
        if cloudflare_model not in {"@cf/cloudflare/clef", "@cf/cloudflare/clef-flash"}:
            raise ValueError("CLOUDFLARE_MODEL must be @cf/cloudflare/clef or @cf/cloudflare/clef-flash")
        return cls(
            decision_provider=provider,
            decision_model=_env("DECISION_MODEL", "convaiinnovations/laya-free"),
            decision_base_url=_env("AI_GATEWAY_BASE_URL", "https://ai-gateway.vercel.sh/v1").rstrip("/"),
            gateway_api_key=_env("AI_GATEWAY_API_KEY"),
            decision_timeout_s=_timeout("DECISION_TIMEOUT_S", "10"),
            cloudflare_model=cloudflare_model,
            cloudflare_account_id=_env("CLOUDFLARE_ACCOUNT_ID"),
            cloudflare_api_token=_env("CLOUDFLARE_API_TOKEN", _env("CLOUDFLARE_AUTH_TOKEN")),
            reasoning_model=_env("REASONING_MODEL", "vertex_ai/gemini-3.8-flash"),
            reasoning_api_key=_env("REASONING_API_KEY"),
            reasoning_base_url=_env("REASONING_BASE_URL"),
            reasoning_timeout_s=_timeout("REASONING_TIMEOUT_S", "15"),
            vertex_project=_env("VERTEXAI_PROJECT", _env("GOOGLE_CLOUD_PROJECT")),
            vertex_location=_env("VERTEXAI_LOCATION", "global"),
        )

    @property
    def reasoning_enabled(self) -> bool:
        if self.reasoning_model == "local":
            return False
        if self.reasoning_model.startswith("vertex_ai/"):
            return bool(self.vertex_project)
        # Other providers use REASONING_API_KEY or LiteLLM's native credentials.
        return True

    def completion_options(self) -> dict:
        options = {"model": self.reasoning_model, "timeout": self.reasoning_timeout_s, "num_retries": 0}
        if self.reasoning_model.startswith("vertex_ai/"):
            options.update(vertex_project=self.vertex_project, vertex_location=self.vertex_location)
        if self.reasoning_api_key:
            options["api_key"] = self.reasoning_api_key
        if self.reasoning_base_url:
            options["api_base"] = self.reasoning_base_url
        return options
