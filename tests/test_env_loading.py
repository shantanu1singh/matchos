"""Check secret-file precedence and automatic configuration loading."""

import os

from matchos import llm_config


def test_env_local_overrides_env_but_preserves_shell(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", {"AI_GATEWAY_API_KEY": "shell-key"})
    (tmp_path / ".env").write_text(
        'AI_GATEWAY_API_KEY=base-key\nVERTEXAI_PROJECT=base-project\n'
        'DECISION_MODEL=convaiinnovations/laya\n'
    )
    (tmp_path / ".env.local").write_text(
        'AI_GATEWAY_API_KEY=local-key\nVERTEXAI_PROJECT=local-project\n'
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
        "AI_GATEWAY_API_KEY=test-key\nVERTEXAI_PROJECT=test-project\n"
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
