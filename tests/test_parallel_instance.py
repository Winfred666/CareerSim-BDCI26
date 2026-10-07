"""Verify a symlinked runner keeps state in its importing worktree."""

import json
import os
from pathlib import Path
import subprocess
import sys


def test_symlinked_worktree_isolates_runner_and_coach(tmp_path: Path) -> None:
    """Shared code must not redirect coach state to the source checkout."""
    source = Path(__file__).resolve().parents[1]
    worktree = tmp_path / "parallel"
    worktree.mkdir()
    (worktree / "career_sim_runner").symlink_to(source / "career_sim_runner", target_is_directory=True)
    (worktree / "scripts").symlink_to(source / "scripts", target_is_directory=True)
    (worktree / ".career-instance.json").write_text('{"instance_name": "career_emu_test_parallel"}')
    env = os.environ.copy()
    for key in ("CAREER_SIM_REPO_ROOT", "JIUWENSWARM_DATA_DIR", "PYTHONPATH"):
        env.pop(key, None)
    code = """
import json
from career_sim_runner.constants import REPO_ROOT, DEFAULT_INSTANCE_NAME, DEFAULT_DB_PATH, DEFAULT_OUTPUT_ROOT
from career_sim_runner.coach.cli import REPO_ROOT as COACH_ROOT, _state_path, _registry_path
from career_sim_runner.paths import jiuwenswarm_data_dir
print(json.dumps([str(REPO_ROOT), DEFAULT_INSTANCE_NAME, str(DEFAULT_DB_PATH), str(DEFAULT_OUTPUT_ROOT),
                  str(COACH_ROOT), str(_state_path(DEFAULT_DB_PATH)),
                  str(_registry_path(DEFAULT_DB_PATH)), str(jiuwenswarm_data_dir())]))
"""
    def inspect(directory):
        return json.loads(subprocess.check_output([sys.executable, "-c", code], cwd=directory, env=env, text=True))
    parallel = inspect(worktree)
    primary = inspect(source)
    assert parallel[0] == parallel[4] == str(worktree)
    assert parallel[1] == "career_emu_test_parallel"
    for index in (2, 5, 6):
        assert Path(parallel[index]).is_relative_to(worktree)
        assert parallel[index] != primary[index]
    assert parallel[3] == str(worktree / ".career_sim_runner" / "career_emu" / "outputs")
    assert primary[3] == str(source / ".career_sim_runner" / "career_emu" / "outputs")
    assert parallel[7] != primary[7]
    assert primary[0] == primary[4] == str(source)
    assert primary[1] == "career_emu"
