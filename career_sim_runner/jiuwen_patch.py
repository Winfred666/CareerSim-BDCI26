"""Install the narrow JiuwenSwarm control-plane compatibility branch."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import shutil
import subprocess
from pathlib import Path


SUPPORTED_OPENJIUWEN = "0.1.19"
SUPPORTED_JIUWENSWARM = "0.2.8b1"
PATCH_FILE = "openjiuwen-0.1.19-careersim-control.patch"
JIUWENSWARM_PATCH_FILE = "workswarm-0.2.8b1-careersim-delete.patch"
REWIND_PATCH_FILE = "workswarm-0.2.8b1-careersim-rewind.patch"
LLM_RETRY_PATCH_FILE = "openjiuwen-0.1.19-careersim-llm-retry.patch"
LLM_RETRY_TARGETS = {
    "openjiuwen/harness/rails/model_anomaly_detection_rail.py": (
        "Successful calls replenish the consecutive-failure retry budget.",
    ),
}
TASK_ERROR_PATCH_FILE = "openjiuwen-0.1.19-careersim-task-error.patch"
TASK_ERROR_TARGETS = {
    "openjiuwen/harness/task_loop/task_loop_event_handler.py": (
        '{"error": str(error_msg), "result_type": "error",',
    ),
}
REWIND_TARGETS = {
    "jiuwenswarm/server/agent_ws_server.py": (
        "from jiuwenswarm.agents.harness.common.careersim_rewind import rewind_before_tool",
    ),
    "jiuwenswarm/agents/harness/common/session_ops_service.py": (
        "return persist_ok", "deep_agent.save_state(session, DeepAgentState())",
    ),
}
TARGETS = {
    "openjiuwen/agent_teams/workflow/engine/budget.py": (
        "def spent_for(self, scope: str)",
        "_spent_by_scope",
    ),
    "openjiuwen/agent_teams/workflow/backends/budget_rail.py": (
        "scope: str | None = None",
        "self._budget.add(tokens, scope=self._scope)",
    ),
    "openjiuwen/agent_teams/workflow/backends/team_worker_backend.py": (
        'KNOWN_OPTIONS = frozenset({"cwd"})',
        'requested_cwd != "team_skills"',
        "scope=self._run_id",
    ),
    "openjiuwen/agent_teams/workflow/backends/avatar_session_backend.py": (
        "scope=self._run_id",
    ),
    "openjiuwen/agent_teams/workflow/tool_swarmflow.py": (
        "def _emit_usage(",
        '"usage_id": f"swarmflow:{task_id}"',
    ),
}
JIUWENSWARM_TARGETS = {
    "jiuwenswarm/agents/harness/team/team_manager.py": (
        "failed to read delete team binding",
        "if session_id in binding.session_ids",
    ),
}


def _site_packages_root() -> Path:
    spec = importlib.util.find_spec("openjiuwen")
    locations = list(spec.submodule_search_locations or []) if spec else []
    if len(locations) != 1:
        raise RuntimeError("Cannot locate the installed openjiuwen package")
    return Path(locations[0]).resolve().parent


def _ensure_patch(
    *,
    distribution: str,
    version: str,
    patch_file: str,
    targets: dict[str, tuple[str, ...]],
    site_root: Path,
) -> Path:
    installed = importlib.metadata.version(distribution)
    if installed != version:
        raise RuntimeError(f"Control patch supports {distribution} {version}, found {installed}")
    states = {
        relative: _target_is_patched(site_root, relative, markers)
        for relative, markers in targets.items()
    }
    patch_path = Path(__file__).resolve().parent / "patches" / patch_file
    if all(states.values()):
        return patch_path
    if any(states.values()):
        raise RuntimeError(f"{distribution} control patch is only partially applied")

    patch_command = shutil.which("patch")
    if patch_command is None:
        raise RuntimeError("The 'patch' command is required to prepare Jiuwen")
    if not patch_path.is_file():
        raise RuntimeError(f"Missing Jiuwen control patch: {patch_path}")

    result = subprocess.run(
        [patch_command, "--batch", "--forward", "--fuzz=0", "-p1", "-i", str(patch_path)],
        cwd=site_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Failed to apply {distribution} control patch: {details}")
    if not all(
        _target_is_patched(site_root, relative, markers)
        for relative, markers in targets.items()
    ):
        raise RuntimeError(f"{distribution} control patch completed without expected markers")
    return patch_path


def _target_is_patched(site_root: Path, relative: str, markers: tuple[str, ...]) -> bool:
    path = site_root / relative
    if not path.is_file():
        raise RuntimeError(f"Missing openjiuwen source file: {path}")
    text = path.read_text(encoding="utf-8")
    return all(marker in text for marker in markers)


def ensure_openjiuwen_control_patch() -> Path:
    """Apply the pinned Jiuwen control patches once and return the core patch.

    The OpenJiuwen patch adds opt-in primitives used by the coach workflow:
    ``cwd='team_skills'`` for one-shot workers. Upstream now provides per-run
    journals and atomic metadata writes. The patch publishes the worker total
    from SwarmFlow's existing token ledger onto the outer usage stream, without
    changing workflow results. The JiuwenSwarm patch makes team deletion recover
    ownership from its persisted binding after rewind. Upstream defaults are
    unchanged.
    """
    for distribution, version in (("openjiuwen", SUPPORTED_OPENJIUWEN), ("workswarm", SUPPORTED_JIUWENSWARM)):
        installed = importlib.metadata.version(distribution)
        if installed != version:
            raise RuntimeError(f"Control patch supports {distribution} {version}, found {installed}")
    site_root = _site_packages_root()
    # Service children run from their instance directory, not the repository.
    # Install the small extension beside the pinned package so the WS handler
    # never depends on a checkout being present in its Python import path.
    package = Path(__file__).resolve().parent
    extension_root = site_root / "jiuwenswarm" / "agents" / "harness" / "common"
    for source, name in ((package / "coach" / "history.py", "careersim_history.py"),
                         (package / "jiuwen_rewind.py", "careersim_rewind.py")):
        content = source.read_text(encoding="utf-8").replace(
            "from career_sim_runner.coach.history import retained_history",
            "from jiuwenswarm.agents.harness.common.careersim_history import retained_history",
        )
        target = extension_root / name
        if not target.exists() or target.read_text(encoding="utf-8") != content:
            temporary = target.with_suffix(".tmp")
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(target)
    patch_path = _ensure_patch(
        distribution="openjiuwen",
        version=SUPPORTED_OPENJIUWEN,
        patch_file=PATCH_FILE,
        targets=TARGETS,
        site_root=site_root,
    )
    _ensure_patch(
        distribution="workswarm",
        version=SUPPORTED_JIUWENSWARM,
        patch_file=JIUWENSWARM_PATCH_FILE,
        targets=JIUWENSWARM_TARGETS,
        site_root=site_root,
    )
    _ensure_patch(
        distribution="workswarm", version=SUPPORTED_JIUWENSWARM,
        patch_file=REWIND_PATCH_FILE, targets=REWIND_TARGETS, site_root=site_root,
    )
    _ensure_patch(
        distribution="openjiuwen", version=SUPPORTED_OPENJIUWEN,
        patch_file=LLM_RETRY_PATCH_FILE, targets=LLM_RETRY_TARGETS, site_root=site_root,
    )
    _ensure_patch(
        distribution="openjiuwen", version=SUPPORTED_OPENJIUWEN,
        patch_file=TASK_ERROR_PATCH_FILE, targets=TASK_ERROR_TARGETS, site_root=site_root,
    )
    _ensure_patch(
        distribution="workswarm", version=SUPPORTED_JIUWENSWARM,
        patch_file="workswarm-0.2.8b1-careersim-leader-prompt.patch",
        targets={"jiuwenswarm/agents/harness/team/config_loader.py": (
            "Preserve the leader's private working agreement during cold bootstrap.",
        )}, site_root=site_root,
    )
    _ensure_patch(
        distribution="openjiuwen", version=SUPPORTED_OPENJIUWEN,
        patch_file="openjiuwen-0.1.19-careersim-stop-cycle.patch",
        targets={"openjiuwen/agent_teams/harness/team_harness.py": (
            "A stopped native can retain its session until asynchronous teardown commits.",
        )}, site_root=site_root,
    )
    return patch_path


__all__ = ["ensure_openjiuwen_control_patch"]
