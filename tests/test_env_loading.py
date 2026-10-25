"""Check secret-file precedence and automatic configuration loading."""

import os

import pytest

from matchos import llm_config


@pytest.mark.parametrize("installed", [False, True])
def test_dotenv_loading_works_from_source_and_installed_packages(monkeypatch, tmp_path, installed):
    package_root = tmp_path / ("site-packages" if installed else "checkout")
    package_root.mkdir()
    launch = tmp_path / "launch"
    launch.mkdir()
    monkeypatch.chdir(launch)
    monkeypatch.setattr(llm_config, "__file__", str(package_root / "matchos" / "llm_config.py"))
    if not installed:
        (package_root / "pyproject.toml").write_text('[project]\nname = "matchos"\n')
    expected = launch if installed else package_root
    (expected / ".env.local").write_text("MATCHOS_TEST_MARKER=expected-location\n")
    monkeypatch.setattr(llm_config, "ENV_DIR", llm_config._env_directory())
    monkeypatch.setattr(os, "environ", {})
    llm_config.load_env_files()
    assert os.environ["MATCHOS_TEST_MARKER"] == "expected-location"


def test_env_local_overrides_env_but_preserves_shell(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", {"AI_GATEWAY_API_KEY": "shell-key"})
    (tmp_path / ".env").write_text(
        "AI_GATEWAY_API_KEY=base-key\nVERTEXAI_PROJECT=base-project\nDECISION_MODEL=convaiinnovations/laya\n"
    )
    (tmp_path / ".env.local").write_text(
        "AI_GATEWAY_API_KEY=local-key\nVERTEXAI_PROJECT=local-project\n"
        'GOOGLE_APPLICATION_CREDENTIALS="/tmp/credentials with spaces.json"\n'
    )
    llm_config.load_env_files(tmp_path)
    assert os.environ["AI_GATEWAY_API_KEY"] == "shell-key"
    assert os.environ["VERTEXAI_PROJECT"] == "local-project"
    assert os.environ["DECISION_MODEL"] == "convaiinnovations/laya"
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == "/tmp/credentials with spaces.json"


def test_config_automatically_loads_project_files_from_another_cwd(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(llm_config, "ENV_DIR", tmp_path)
    other_directory = tmp_path / "elsewhere"
    other_directory.mkdir()
    monkeypatch.chdir(other_directory)
    (tmp_path / ".env.local").write_text(
        "REASONING_PROVIDER=litellm\nAI_GATEWAY_API_KEY=test-key\nVERTEXAI_PROJECT=test-project\n"
        "DECISION_TIMEOUT_S=3\n"
    )
    config = llm_config.LLMConfig.from_env()
    assert config.gateway_api_key == "test-key"
    assert config.vertex_project == "test-project"
    assert config.decision_timeout_s == 3
    assert config.reasoning_enabled


def test_missing_files_and_unassigned_values_are_optional(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", {})
    llm_config.load_env_files(tmp_path)
    assert os.environ == {}
    (tmp_path / ".env").write_text("UNASSIGNED\nEMPTY=\n")
    llm_config.load_env_files(tmp_path)
    assert "UNASSIGNED" not in os.environ
    assert os.environ["EMPTY"] == ""
