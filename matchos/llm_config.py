"""Model selection shared by the decision and reasoning adapters."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


def _env_directory() -> Path:
    """Use the checkout root for source runs and the launch directory for installed wheels."""
    source_root = Path(__file__).resolve().parent.parent
    return source_root if (source_root / "pyproject.toml").is_file() else Path.cwd()


ENV_DIR = _env_directory()


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
    decision_provider: str = "azure"
    decision_model: str = "convaiinnovations/laya-free"
    decision_base_url: str = "https://ai-gateway.vercel.sh/v1"
    gateway_api_key: str = field(default="", repr=False)
    decision_timeout_s: float = 10.0
    cloudflare_model: str = "@cf/cloudflare/clef-flash"
    cloudflare_account_id: str = ""
    cloudflare_api_token: str = field(default="", repr=False)
    reasoning_provider: str = "azure"
    azure_endpoint: str = ""
    azure_api_key: str = field(default="", repr=False)
    azure_decision_api_key: str = field(default="", repr=False)
    azure_reasoning_api_key: str = field(default="", repr=False)
    azure_auth_mode: str = "key"
    azure_decision_endpoint: str = ""
    azure_reasoning_endpoint: str = ""
    azure_decision_deployment: str = "Decision-1"
    azure_reasoning_deployment: str = "gpt-6-luna"
    azure_openai_api_version: str = "preview"
    reasoning_model: str = "vertex_ai/gemini-3.8-flash"
    reasoning_api_key: str = field(default="", repr=False)
    reasoning_base_url: str = ""
    reasoning_timeout_s: float = 15.0
    vertex_project: str = ""
    vertex_location: str = "global"

    @classmethod
    def from_env(cls) -> LLMConfig:
        load_env_files()
        provider = _env("DECISION_PROVIDER", "azure").lower()
        if provider not in {"azure", "cloudflare", "vercel", "local"}:
            raise ValueError("DECISION_PROVIDER must be azure, cloudflare, vercel, or local")
        reasoning_provider = _env("REASONING_PROVIDER", "azure").lower()
        if reasoning_provider not in {"azure", "litellm", "local"}:
            raise ValueError("REASONING_PROVIDER must be azure, litellm, or local")
        auth_mode = _env("AZURE_AUTH_MODE", "key").lower()
        if auth_mode not in {"key", "entra"}:
            raise ValueError("AZURE_AUTH_MODE must be key or entra")
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
            reasoning_provider=reasoning_provider,
            azure_endpoint=_azure_endpoint("AZURE_FOUNDRY_ENDPOINT"),
            azure_api_key=_env("AZURE_FOUNDRY_API_KEY"),
            azure_decision_api_key=_env("AZURE_DECISION_API_KEY"),
            azure_reasoning_api_key=_env("AZURE_REASONING_API_KEY"),
            azure_auth_mode=auth_mode,
            azure_decision_endpoint=_azure_endpoint("AZURE_DECISION_ENDPOINT"),
            azure_reasoning_endpoint=_azure_endpoint("AZURE_REASONING_ENDPOINT"),
            azure_decision_deployment=_env("AZURE_DECISION_DEPLOYMENT", "Decision-1"),
            azure_reasoning_deployment=_env("AZURE_REASONING_DEPLOYMENT", "gpt-6-luna"),
            azure_openai_api_version=_env("AZURE_OPENAI_API_VERSION", "preview"),
            reasoning_model=_env("REASONING_MODEL", "vertex_ai/gemini-3.8-flash"),
            reasoning_api_key=_env("REASONING_API_KEY"),
            reasoning_base_url=_env("REASONING_BASE_URL"),
            reasoning_timeout_s=_timeout("REASONING_TIMEOUT_S", "15"),
            vertex_project=_env("VERTEXAI_PROJECT", _env("GOOGLE_CLOUD_PROJECT")),
            vertex_location=_env("VERTEXAI_LOCATION", "global"),
        )

    @property
    def reasoning_enabled(self) -> bool:
        if self.reasoning_provider == "local" or self.reasoning_model == "local":
            return False
        if self.reasoning_provider == "azure":
            return self.azure_configured("reasoning")
        if self.reasoning_model.startswith("vertex_ai/"):
            return bool(self.vertex_project)
        # Other providers use REASONING_API_KEY or LiteLLM's native credentials.
        return True

    def azure_resource_endpoint(self, kind: str) -> str:
        return (
            self.azure_decision_endpoint if kind == "decision" else self.azure_reasoning_endpoint
        ) or self.azure_endpoint

    def azure_configured(self, kind: str) -> bool:
        return bool(
            self.azure_resource_endpoint(kind) and (self.azure_key(kind) or self.azure_auth_mode == "entra")
        )

    def azure_key(self, kind: str) -> str:
        return (
            self.azure_decision_api_key if kind == "decision" else self.azure_reasoning_api_key
        ) or self.azure_api_key

    def completion_options(self) -> dict:
        options = {"model": self.reasoning_model, "timeout": self.reasoning_timeout_s, "num_retries": 0}
        if self.reasoning_model.startswith("vertex_ai/"):
            options.update(vertex_project=self.vertex_project, vertex_location=self.vertex_location)
        if self.reasoning_api_key:
            options["api_key"] = self.reasoning_api_key
        if self.reasoning_base_url:
            options["api_base"] = self.reasoning_base_url
        return options


def _azure_endpoint(name: str) -> str:
    endpoint = _env(name).rstrip("/")
    if endpoint:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(f"{name} must be an HTTPS resource root, without a project or API path")
    return endpoint
