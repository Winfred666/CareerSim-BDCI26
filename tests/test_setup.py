"""Tests for simulator/JiuwenSwarm environment setup."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import career_sim_runner.setup as setup_api


@pytest.mark.parametrize("process_model", [None, "gpt-5.6-terra"])
def test_configured_model_name_ignores_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, process_model: str | None
) -> None:
    """Reused Jiuwen sessions should explicitly receive the current model."""
    env_path = tmp_path / ".env"
    env_path.write_text('MODEL_NAME="current-model"\n', encoding="utf-8")
    monkeypatch.delenv("MODEL_NAME", raising=False)
    if process_model:
        monkeypatch.setenv("MODEL_NAME", process_model)
    monkeypatch.setattr(setup_api, "repo_root", lambda: tmp_path)

    assert setup_api.configured_model_name() == "deepseek-flash"


@pytest.mark.parametrize("repo_env", [False, True])
def test_env_overlay_uses_repo_model_or_default(tmp_path: Path, monkeypatch, repo_env) -> None:
    instance_env = tmp_path / "config.env"
    instance_env.write_text('MODEL_NAME="gpt-5.6-terra"\nKEEP=value\n')
    if repo_env:
        (tmp_path / ".env").write_text('MODEL_NAME="deepseek-v4-flash"\nOTHER=value\n')
    monkeypatch.setattr(setup_api, "ensure_instance_initialized", lambda: tmp_path)
    monkeypatch.setattr(setup_api, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(setup_api, "jiuwenswarm_env_path", lambda: instance_env)
    setup_api.ensure_instance_env_overlay()
    values = setup_api._read_env_values(instance_env)
    assert values["MODEL_NAME"] == "deepseek-flash"
    assert values["KEEP"] == "value"


def test_ensure_instance_configured_uses_per_run_simulator_log_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run-specific log directory is passed to the Career Emulator MCP server."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mcp: {}\n", encoding="utf-8")
    instance_root = tmp_path / "instance"
    run_dir = tmp_path / "outputs" / "example" / "20260914T010203Z"

    monkeypatch.setattr(setup_api, "ensure_instance_initialized", lambda: instance_root)
    monkeypatch.setattr(setup_api, "jiuwenswarm_config_path", lambda: config_path)
    monkeypatch.setattr(setup_api, "default_db_path", lambda: tmp_path / "game.sqlite3")
    monkeypatch.setattr(setup_api, "ensure_instance_env_overlay", lambda: tmp_path / ".env")
    monkeypatch.setattr(
        setup_api,
        "tool_availability",
        lambda: {"career-emulator-mcp": True, "path": str(tmp_path / "venv" / "bin")},
    )

    setup_api.ensure_instance_configured(log_dir=run_dir)

    from ruamel.yaml import YAML

    yaml = YAML()
    config = yaml.load(config_path.read_text(encoding="utf-8"))
    server = config["mcp"]["servers"][0]
    assert server["env"]["CAREER_EMULATOR_LOG_DIR"] == str(run_dir.resolve())
    assert run_dir.is_dir()


def test_ensure_instance_configured_enables_requested_swarmflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A submission can opt into the platform's direct worker-result path."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "modes:\n  team:\n    test-team:\n      enable_swarmflow: false\nmcp: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(setup_api, "ensure_instance_initialized", lambda: tmp_path / "instance")
    monkeypatch.setattr(setup_api, "jiuwenswarm_config_path", lambda: config_path)
    monkeypatch.setattr(setup_api, "default_db_path", lambda: tmp_path / "game.sqlite3")
    monkeypatch.setattr(setup_api, "ensure_instance_env_overlay", lambda: tmp_path / ".env")
    monkeypatch.setattr(
        setup_api,
        "tool_availability",
        lambda: {"career-emulator-mcp": True, "path": str(tmp_path / "venv" / "bin")},
    )
    monkeypatch.setattr(
        "career_sim_runner.install.load_active_install",
        lambda: SimpleNamespace(manifest={"enable_swarmflow": True}),
    )

    setup_api.ensure_instance_configured()

    from ruamel.yaml import YAML

    config = YAML().load(config_path.read_text(encoding="utf-8"))
    assert config["modes"]["team"]["test-team"]["enable_swarmflow"] is True
