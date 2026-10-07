"""Environment setup helpers for the standalone participant runner."""

import os
import re
import shutil
import socket
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import cast

from ruamel.yaml import YAML

from career_sim_runner.constants import (
    CAREER_EMULATOR_ACTIVE_LONG_CHAIN_LIMIT,
    CAREER_EMULATOR_MAX_NOTE_LENGTH,
    CAREER_EMULATOR_MONTHLY_EVENT_LIMIT,
    JIUWEN_PLAYER_MODEL,
)
from career_sim_runner.paths import (
    default_db_path,
    default_emulator_log_dir,
    default_instance_name,
    instance_root,
    jiuwenswarm_data_dir,
    jiuwenswarm_config_dir,
    jiuwenswarm_config_path,
    jiuwenswarm_env_path,
    repo_root,
)

WINDOWS_BINARY_EXT = re.compile(r"\.(bat|ps1|exe)$", flags=re.IGNORECASE)


def ensure_instance_initialized() -> Path:
    """Create the named JiuwenSwarm instance if needed."""
    root = instance_root()
    if root.exists():
        return root
    subprocess.run(
        ["jiuwenswarm-init", "--name", default_instance_name()],
        check=True,
    )
    return root


def reset_team_runtime() -> Path:
    """Delete the host team view and start a fresh one.

    The team store is SQLite-backed and must never be moved while JiuwenSwarm
    has it open.  The supported service unit gives us a reliable stop/start
    boundary; the old directory is deleted because it is only a disposable
    view of the current solution.
    """
    ensure_instance_initialized()
    # The checked-in unit uses a hyphenated, user-facing name while the
    # instance identifier uses an underscore (``career_emu``).
    service = f"{default_instance_name().replace('_', '-')}.service"
    active = subprocess.run(["systemctl", "--user", "is-active", service], check=False, capture_output=True)
    if active.returncode != 0:
        unit = subprocess.run(["systemctl", "--user", "cat", service], check=False, capture_output=True)
        if unit.returncode != 0:
            raise RuntimeError("career-emu.service is not installed; run `make service-install` before a fresh play")
        subprocess.run(["systemctl", "--user", "reset-failed", service], check=False)
        subprocess.run(["systemctl", "--user", "start", service], check=True)

    subprocess.run(["systemctl", "--user", "stop", service], check=True)
    team_dir = jiuwenswarm_data_dir() / ".agent_teams"
    try:
        if team_dir.exists():
            shutil.rmtree(team_dir)
        team_dir.mkdir(parents=True, exist_ok=True)
    finally:
        subprocess.run(["systemctl", "--user", "start", service], check=True)
    ws_url = resolve_instance_ws_url()
    host, port = ws_url.removeprefix("ws://").split(":", 1)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, int(port)), timeout=1):
                return team_dir
        except OSError:
            time.sleep(1)
    raise RuntimeError("career_emu.service restarted but AgentServer did not become reachable within 60s")


def ensure_instance_configured(log_dir: Path | None = None, *, coach_gate: Path | None = None,
                               db_path: Path | None = None) -> Path:
    """Write the required MCP server entry into the named instance config.

    ``career-emulator`` opens its per-session log files under the directory
    supplied by ``CAREER_EMULATOR_LOG_DIR`` when the MCP server starts.  Keep
    the historical runtime directory as the setup-time default, but allow a
    play run to point the simulator at that run's output directory.  The
    caller must reload the JiuwenSwarm agent after changing this value so the
    MCP subprocess receives the updated environment.

    :param log_dir: Optional per-run directory for Career Emulator logs.
    """
    from career_sim_runner.jiuwen_patch import ensure_openjiuwen_control_patch

    ensure_openjiuwen_control_patch()
    ensure_instance_initialized()
    config_path = jiuwenswarm_config_path()
    yaml = YAML()
    yaml.preserve_quotes = True
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.load(handle) or {}
    # SwarmFlow is a per-team runtime capability, not something a skill can
    # enable from its own frontmatter.  Let a submission opt in explicitly so
    # fixed workflows can use one-shot workers without changing the default
    # for other submissions.
    from career_sim_runner.install import load_active_install

    active_install = load_active_install()
    manifest = active_install.manifest if active_install is not None else {}
    if "enable_swarmflow" in manifest:
        team_modes = (data.get("modes") or {}).get("team") or {}
        if isinstance(team_modes, dict):
            for team_config in team_modes.values():
                if isinstance(team_config, dict):
                    team_config["enable_swarmflow"] = bool(manifest["enable_swarmflow"])
    mcp = data.setdefault("mcp", {})
    is_available = tool_availability()
    cwd = cast(str, is_available["path"])
    if not is_available["career-emulator-mcp"]:
        raise RuntimeError(
            f"\033[41mcareer-emulator[mcp]\033[0m is not installed in the same environment as JiuwenSwarm: {cwd}"
        )
    emulator_log_dir = Path(log_dir).expanduser().resolve() if log_dir is not None else default_emulator_log_dir()
    emulator_log_dir.mkdir(parents=True, exist_ok=True)
    mcp["servers"] = [
        {
            "name": "career-emulator",
            "enabled": True,
            "transport": "stdio",
            # Make the venv command discoverable to JiuwenSwarm's stdio
            # subprocess.  ``cwd`` alone is not guaranteed to be prepended to
            # PATH by the host, so explicitly add it while retaining the
            # parent's PATH for the executable's interpreter/dependencies.
            "command": sys.executable,
            "args": ["-m", "career_sim_runner.emulator_adapter"],
            "cwd": str(repo_root()),
            "env": {
                "CAREER_SIM_INSTANCE_NAME": default_instance_name(),
                "PATH": os.pathsep.join(part for part in (cwd, os.environ.get("PATH", "")) if part),
                "CAREER_EMULATOR_DB": str(db_path or default_db_path()),
                "CAREER_OBSERVE_SNAPSHOTS": str(emulator_log_dir / "observations"),
                **({"CAREER_COACH_GATE": str(coach_gate)} if coach_gate else {}),
                "CAREER_EMULATOR_LOG_DIR": str(emulator_log_dir),
                "CAREER_EMULATOR_ACTIVE_LONG_CHAIN_LIMIT": str(CAREER_EMULATOR_ACTIVE_LONG_CHAIN_LIMIT),
                "CAREER_EMULATOR_MONTHLY_EVENT_LIMIT": str(CAREER_EMULATOR_MONTHLY_EVENT_LIMIT),
                "CAREER_EMULATOR_MAX_NOTE_LENGTH": str(CAREER_EMULATOR_MAX_NOTE_LENGTH),
            },
        }
    ]
    with config_path.open("w", encoding="utf-8") as handle:
        yaml.dump(data, handle)
    ensure_instance_env_overlay()
    return config_path


def ensure_instance_env_overlay() -> Path:
    """Overlay repo-level ``.env`` values onto the named instance env file."""
    ensure_instance_initialized()
    overlay_path = repo_root() / ".env"
    instance_env_path = jiuwenswarm_env_path()
    overlay_values = _read_env_values(overlay_path) if overlay_path.is_file() else {}
    overlay_values["MODEL_NAME"] = configured_model_name()
    _apply_env_values(instance_env_path, overlay_values)
    return instance_env_path


def configured_model_name() -> str:
    """Use the single Player model for fresh and reused sessions."""
    return JIUWEN_PLAYER_MODEL


def _apply_env_values(path: Path, overlay_values: dict[str, str]) -> None:
    """Apply env overrides in place while preserving unrelated keys."""
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    updated_lines: list[str] = []
    seen: set[str] = set()
    for line in raw_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            updated_lines.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in overlay_values:
            updated_lines.append(f"{key}={overlay_values[key]}")
            seen.add(key)
        else:
            updated_lines.append(line)
    for key, value in overlay_values.items():
        if key not in seen:
            updated_lines.append(f"{key}={value}")
    path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")


def _read_env_values(path: Path) -> dict[str, str]:
    """Parse simple ``KEY=value`` assignments from an env file."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def setup_summary() -> dict[str, str]:
    """Return a participant-facing setup summary."""
    ensure_instance_initialized()
    return {
        "instance_name": default_instance_name(),
        "instance_root": str(instance_root()),
        "jiuwenswarm_config_dir": str(jiuwenswarm_config_dir()),
        "jiuwenswarm_config_path": str(jiuwenswarm_config_path()),
        "jiuwenswarm_env_path": str(jiuwenswarm_env_path()),
        "repo_env_overlay_path": str(repo_root() / ".env"),
        "recommended_db_path": str(default_db_path()),
    }


@lru_cache(maxsize=1)
def tool_availability() -> dict[str, bool | str]:
    """Return whether key local commands are available."""
    jiuwenswarm_path = shutil.which("jiuwenswarm-start")
    if isinstance(jiuwenswarm_path, str):
        cwd = WINDOWS_BINARY_EXT.sub("", jiuwenswarm_path, count=1).removesuffix("jiuwenswarm-start")
    else:
        cwd = ""
    return {
        "career-emulator-mcp": bool(list(Path(cwd).glob("career-emulator-mcp*")) and list(Path(cwd).glob("fastmcp*"))),
        "jiuwenswarm-start": jiuwenswarm_path is not None,
        "path": cwd,
    }


def resolve_instance_ws_url() -> str:
    """Resolve the AgentServer websocket URL from ``jiuwenswarm-start --list`` output."""
    result = subprocess.run(
        ["jiuwenswarm-start", "--list"],
        check=True,
        capture_output=True,
        text=True,
    )
    instance = default_instance_name()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == instance:
            ports = parts[-1]
            agent_server_port = ports.split("/", 1)[0]
            return f"ws://127.0.0.1:{agent_server_port}"
    raise RuntimeError(f"JiuwenSwarm does not seem to have instance '{instance}'")
