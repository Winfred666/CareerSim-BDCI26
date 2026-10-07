"""Native teammates stay silent until their role receives a direct message."""
import shutil
import subprocess
import sys
from pathlib import Path


SKILL = Path(__file__).resolve().parents[1] / 'solution/skills/observe-decide-review'


def test_submission_has_no_runtime_adapter_or_platform_tasks(tmp_path):
    root = tmp_path / 'skill'
    shutil.copytree(SKILL, root)
    assert not (root / 'scripts/team-state.py').exists()
    assert not (root / 'scripts/silent-team.py').exists()
    assert not (root / 'workflows').exists()
    for path in (root / 'scripts').rglob('*.py'):
        text = path.read_text()
        assert 'import openjiuwen' not in text
        assert 'SilentTeamRail' not in text
    for path in (root / 'stages').glob('*.md'):
        text = path.read_text()
        assert 'team-state.py' not in text
        assert 'member_complete_task' not in text


def test_analysis_cli_rejects_startup_before_loading_event(tmp_path):
    root = tmp_path / 'skill'
    shutil.copytree(SKILL, root)
    notes = root / 'notebooks'
    (notes / 'workflow-state.json').write_text('{"phase":"observe_running"}')
    before = {str(path.relative_to(notes)): path.read_bytes()
              for path in notes.rglob('*') if path.is_file()}
    result = subprocess.run([sys.executable, str(root / 'scripts/translate.py'), 'N', 'ready'],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert 'only available during the analysis stage' in result.stderr
    assert before == {str(path.relative_to(notes)): path.read_bytes()
                      for path in notes.rglob('*') if path.is_file()}
