"""Tests for participant submission installation."""

from pathlib import Path
import shutil
import sqlite3

import pytest

import career_sim_runner.install as install_module
from career_sim_runner.install import install_submission


def fixture_submission() -> Path:
    """Return the real sample submission path."""
    return Path(__file__).resolve().with_name("fixtures") / "solution"


def test_install_submission_generates_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Install into isolated runtime paths without touching a live instance."""
    skills_dir = tmp_path / "skills"
    isolated_runtime = tmp_path / "runner-runtime"
    isolated_instance = tmp_path / "jiuwen-instance"
    (isolated_instance / "agent" / "workspace" / "skills").mkdir(parents=True)
    isolated_runtime.mkdir()
    monkeypatch.setattr(install_module, "ensure_runtime_dirs", lambda: None)
    monkeypatch.setattr(install_module, "ensure_instance_initialized", lambda: isolated_instance)
    monkeypatch.setattr(install_module, "active_install_path", lambda: isolated_runtime / "active_install.json")
    monkeypatch.setattr(install_module, "jiuwenswarm_data_dir", lambda: isolated_instance)
    monkeypatch.setattr(
        install_module,
        "jiuwenswarm_skills_state_path",
        lambda: isolated_instance / "agent" / "workspace" / "skills" / "skills_state.json",
    )
    (tmp_path / "memory").mkdir()
    record = install_submission(fixture_submission(), skills_dir=skills_dir)

    assert Path(record.skill_dir) == skills_dir
    assert record.submission_name == "flash-tomato"
    assert record.manifest["submission_mode"] == "skill_bundle"
    assert sorted(record.manifest["participant_skill_names"]) == ["dummy-skill"]
    assert record.skill_name == "submission-skills"
    assert not (skills_dir / "career-emulator-player").exists()
    assert (skills_dir / "dummy-skill" / "SKILL.md").is_file()
    assert (isolated_runtime / "active_install.json").is_file()


def test_reinstall_preserves_open_team_database_and_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated installs must not unlink a live host's SQLite database or workspaces."""
    instance = tmp_path / "instance"
    skills = instance / "agent" / "workspace" / "skills"
    skills.mkdir(parents=True)
    teams = instance / ".agent_teams"
    team_workspace = teams / "existing-team" / "team-workspace"
    team_workspace.mkdir(parents=True)
    evidence = team_workspace / "review.txt"
    evidence.write_text("review completed", encoding="utf-8")
    session = instance / "agent" / "sessions" / "existing-session"
    session.mkdir(parents=True)
    history = session / "history.jsonl"
    history.write_text('{"role":"leader"}\n', encoding="utf-8")
    monkeypatch.setattr(install_module, "ensure_runtime_dirs", lambda: None)
    monkeypatch.setattr(install_module, "ensure_instance_initialized", lambda: instance)
    monkeypatch.setattr(install_module, "active_install_path", lambda: tmp_path / "install.json")
    monkeypatch.setattr(install_module, "jiuwenswarm_data_dir", lambda: instance)
    monkeypatch.setattr(install_module, "jiuwenswarm_skills_state_path", lambda: skills / "skills_state.json")

    db_path = teams / "team.db"
    writer = sqlite3.connect(db_path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE team_member (name TEXT PRIMARY KEY)")
        writer.execute("INSERT INTO team_member VALUES ('event-translator')")
        writer.commit()
        inode = db_path.stat().st_ino
        for name in ("event-reviewer", "redline-guardian"):
            install_submission(fixture_submission(), skills_dir=skills)
            assert db_path.stat().st_ino == inode
            writer.execute("INSERT INTO team_member VALUES (?)", (name,))
            writer.commit()
            reader = sqlite3.connect(db_path)
            try:
                assert reader.execute("SELECT name FROM team_member WHERE name = ?", (name,)).fetchone() == (name,)
                assert reader.execute("PRAGMA quick_check").fetchone() == ("ok",)
            finally:
                reader.close()
            assert evidence.read_text(encoding="utf-8") == "review completed"
            assert history.is_file()
    finally:
        writer.close()


def test_reinstall_does_not_write_runtime_learning_to_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installing is never an implicit approval to change submission notebooks."""
    submission = tmp_path / "submission"
    shutil.copytree(fixture_submission(), submission)
    source = submission / "skills" / "dummy-skill" / "notebooks"
    runtime_root = tmp_path / "instance" / "agent" / "workspace"
    runtime = runtime_root / "skills" / "dummy-skill" / "notebooks"
    source.mkdir(parents=True)
    runtime.mkdir(parents=True)
    source_dict = source / "event-translator-dictionary.tsv"
    runtime_dict = runtime / "event-translator-dictionary.tsv"
    source_dict.write_text("职场黑话\t状态变化\n有效沟通\tN++\n", encoding="utf-8")
    runtime_dict.write_text("职场黑话\t状态变化\n有效沟通\tN+\n", encoding="utf-8")

    instance = tmp_path / "instance"
    runner_runtime = tmp_path / "runner-runtime"
    runner_runtime.mkdir()
    monkeypatch.setattr(install_module, "ensure_runtime_dirs", lambda: None)
    monkeypatch.setattr(install_module, "ensure_instance_initialized", lambda: instance)
    monkeypatch.setattr(
        install_module,
        "active_install_path",
        lambda: runner_runtime / "active_install.json",
    )
    monkeypatch.setattr(install_module, "jiuwenswarm_data_dir", lambda: instance)
    monkeypatch.setattr(
        install_module,
        "jiuwenswarm_skills_state_path",
        lambda: runtime_root / "skills" / "skills_state.json",
    )

    install_submission(submission, skills_dir=runtime_root / "skills")

    assert source_dict.read_text(encoding="utf-8").endswith("有效沟通\tN++\n")
    assert runtime_dict.read_text(encoding="utf-8").endswith("有效沟通\tN++\n")
