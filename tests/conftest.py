"""Offline defaults: a developer's credentials and dotenv files are never test inputs."""

import os

import pytest

# Set before test modules import LiteLLM; its bundled cost map is sufficient for tests.
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch, tmp_path):
    from matchos import llm_config

    monkeypatch.setattr(llm_config, "ENV_DIR", tmp_path / "unused-env")
    for name, value in {
        "DECISION_PROVIDER": "local",
        "REASONING_PROVIDER": "local",
        "REASONING_MODEL": "local",
        "MATCHOS_OTEL_ENABLED": "0",
        "MATCHOS_SPEECH_ENABLED": "0",
        "MATCHOS_BASIC_AUTH_ENABLED": "0",
        "MATCHOS_MODEL_LOG_CONTENT": "1",
        "APPLICATIONINSIGHTS_CONNECTION_STRING": "",
    }.items():
        monkeypatch.setenv(name, value)
