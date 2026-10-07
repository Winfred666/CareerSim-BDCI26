"""Controller-owned snapshots of runtime notebooks; never write to a submission."""

from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path

from career_sim_runner.install import load_active_install
from career_sim_runner.paths import jiuwenswarm_data_dir

NotebookSnapshot = dict[str, dict[str, str]]


def align_notebook_snapshot(snapshot: NotebookSnapshot, skill_root: Path | None) -> NotebookSnapshot:
    """Carry one skill's notebooks across a one-to-one skill rename."""
    if skill_root is None or len(snapshot) != 1:
        return snapshot
    installed = [path.name for path in skill_root.iterdir() if path.is_dir()]
    if len(installed) != 1:
        return snapshot
    old_name = next(iter(snapshot))
    new_name = installed[0]
    return {new_name: snapshot[old_name]} if old_name != new_name else snapshot


def runtime_skills_dir(agent_session_id: str, *, data_dir: Path | None = None) -> Path | None:
    data_dir = data_dir if data_dir is not None else jiuwenswarm_data_dir()
    history = data_dir / "agent" / "sessions" / agent_session_id / "history.jsonl"
    team_id = ""
    if history.is_file():
        for line in history.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            event = record.get("event")
            candidate = event.get("team_id") if isinstance(event, dict) else record.get("team_id")
            if candidate and Path(str(candidate)).name == str(candidate):
                team_id = str(candidate)
    if team_id:
        root = data_dir / ".agent_teams" / team_id / "team-workspace" / "skills"
        if root.is_dir():
            return root
    install = load_active_install()
    if install and install.skill_dir and Path(install.skill_dir).is_dir():
        return Path(install.skill_dir)
    return None


def _resolved_runtime_skill(skill_root: Path, skill: Path) -> Path:
    """Allow only a native team link to this instance's installed skill."""
    resolved = skill.resolve()
    if resolved.is_relative_to(skill_root.resolve()):
        return resolved
    install = load_active_install()
    if install and install.skill_dir and skill.is_symlink():
        installed = Path(install.skill_dir).resolve() / skill.name
        if installed.is_dir() and not installed.is_symlink() and resolved == installed:
            return resolved
    raise ValueError(f"Skill escapes runtime skill directory: {skill}")


def capture_notebooks(skill_root: Path | None) -> NotebookSnapshot:
    """Capture every notebook, including dictionaries, caps and nested records."""
    snapshot: NotebookSnapshot = {}
    if skill_root is None:
        return snapshot
    for skill in sorted(skill_root.iterdir()):
        if not skill.is_dir():
            continue
        resolved_skill = _resolved_runtime_skill(skill_root, skill)
        files = snapshot[skill.name] = {}
        notebooks = skill / "notebooks"
        for path in sorted(notebooks.rglob("*")):
            if not path.resolve().is_relative_to(resolved_skill / "notebooks"):
                raise ValueError(f"Notebook escapes runtime skill directory: {path}")
            if path.is_file():
                files[path.relative_to(notebooks).as_posix()] = base64.b64encode(path.read_bytes()).decode("ascii")
    return snapshot


def _decoded_snapshot(snapshot: NotebookSnapshot) -> dict[str, dict[Path, bytes]]:
    decoded = {}
    for skill, files in snapshot.items():
        if not skill or skill in {".", ".."} or Path(skill).name != skill:
            raise ValueError(f"Invalid notebook skill name: {skill}")
        decoded[skill] = {}
        for name, content in files.items():
            relative = Path(name)
            if not name or relative.is_absolute() or any(part in {".", ".."} for part in relative.parts):
                raise ValueError(f"Invalid notebook path: {name}")
            decoded[skill][relative] = base64.b64decode(content, validate=True)
    return decoded


def restore_notebooks(skill_root: Path | None, snapshot: NotebookSnapshot) -> list[Path]:
    """Replace notebook trees exactly, removing files from withdrawn events.

    Skills removed from the installed solution stay removed. Other skill files
    (stage instructions, scripts) come from the newly installed submission.
    """
    decoded = _decoded_snapshot(snapshot)  # Validate the whole snapshot before writing.
    if skill_root is None:
        if snapshot:
            raise RuntimeError("Runtime skill directory is unavailable for notebook restore")
        return []
    restored = []
    for skill, files in decoded.items():
        root = skill_root / skill
        if not root.is_dir():
            continue
        _resolved_runtime_skill(skill_root, root)
        target = root / "notebooks"
        if target.is_symlink():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        target.mkdir()
        for relative, content in files.items():
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            restored.append(path)
    return restored


def verify_notebooks(skill_root: Path | None, snapshot: NotebookSnapshot) -> None:
    """Require byte-identical restored trees, including absence of future files."""
    actual = capture_notebooks(skill_root)
    for skill, files in snapshot.items():
        # Reinstall can remove an SDK-only skill with no notebook files.
        if actual.get(skill, {}) != files:
            raise RuntimeError(f"Notebook restore verification failed for {skill}")
