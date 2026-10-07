"""Expand real runtime instructions and exercise a game cycle without session.json."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from tests.review_logs import write_action_log

SKILL = Path(__file__).resolve().parents[1] / 'solution/skills/observe-decide-review'
SESSION = '0123456789abcdef0123456789abcdef'


@pytest.fixture
def runtime(tmp_path):
    workspace = tmp_path / 'workspace with spaces'
    root = workspace / 'skills' / SKILL.name
    shutil.copytree(SKILL, root)
    return workspace, root


def call(root, script, *args, check=True):
    return subprocess.run([sys.executable, str(root / 'scripts' / script), *args],
                          cwd=root.parent.parent.parent, text=True,
                          capture_output=True, check=check)


def initialize(workspace, root):
    return call(root, 'workflow-state.py', 'create_new', SESSION)


def test_expands_all_markdown_and_returns_only_reread_instruction(runtime):
    workspace, root = runtime
    (root / 'stages/extra.md').write_text('<K> <session_id>')
    result = initialize(workspace, root)
    assert result.stdout.strip() == (
        '已替换此后命令所需 <K> 和 <session_id>，请重读 SKILL.md ，'
        '并继续初始化第 2 步，不允许读其他文件')
    skill = (root / 'SKILL.md').read_text()
    assert f'observe(session_id="{SESSION}")' in skill
    assert f'{workspace}/skills/{SKILL.name}/scripts/read-context.py' in skill
    assert '<!-- runtime-instructions -->' not in skill
    for document in root.rglob('*.md'):
        assert '<K>' not in document.read_text()
        assert '<session_id>' not in document.read_text()
    assert (root / 'stages/extra.md').read_text() == f'{workspace} {SESSION}'
    assert not (root / 'notebooks/session.json').exists()
    assert '<K>' in (SKILL / 'SKILL.md').read_text()


def test_initialization_through_symlink_uses_script_workspace(runtime, tmp_path):
    workspace, root = runtime
    alias = tmp_path / '另一个工作目录' / 'skills'
    alias.parent.mkdir()
    alias.symlink_to(root.parent, target_is_directory=True)
    linked_root = alias / root.name
    call(linked_root, 'workflow-state.py', 'create_new', SESSION)
    saved = json.loads((root / 'notebooks/workflow-state.json').read_text())
    assert saved['workspace'] == str(workspace)
    assert str(workspace / 'skills' / root.name) in (root / 'SKILL.md').read_text()


def test_invalid_session_only_does_not_expand_markdown(runtime):
    _, root = runtime
    before = {p: p.read_bytes() for p in root.rglob('*.md')}
    failed = call(root, 'workflow-state.py', 'create_new', 'invalid', check=False)
    assert failed.returncode == 2
    assert before == {p: p.read_bytes() for p in before}
    assert not (root / 'notebooks/workflow-state.json').exists()


def test_repeat_is_idempotent_and_different_game_cannot_rebind(runtime):
    workspace, root = runtime
    first = initialize(workspace, root)
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert initialize(workspace, root).stdout == first.stdout
    assert before == {p: p.read_bytes() for p in before}
    failed = call(root, 'workflow-state.py', 'create_new', str(workspace),
                  'fedcba9876543210fedcba9876543210', check=False)
    assert failed.returncode == 2 and 'cannot replace an existing session' in failed.stderr
    assert before == {p: p.read_bytes() for p in before}


@pytest.mark.parametrize('workspace_arg,session_arg', [('relative', SESSION), (None, 'invalid')])
def test_invalid_initialization_does_not_write(runtime, workspace_arg, session_arg):
    workspace, root = runtime
    before = (root / 'SKILL.md').read_bytes()
    failed = call(root, 'workflow-state.py', 'create_new', workspace_arg or str(workspace),
                  session_arg, check=False)
    assert failed.returncode == 2
    assert (root / 'SKILL.md').read_bytes() == before
    assert not (root / 'notebooks/workflow-state.json').exists()


def test_observe_decide_review_terminal_keep_bound_identity(runtime):
    workspace, root = runtime
    initialize(workspace, root)
    observation = root / 'observe.json'
    current = {'session_id': SESSION, 'time': {'current_month': 1}, 'status': {
        'level': 'L1', 'output': 2, 'skill': 6, 'network': 3, 'health': 5,
        'dignity': 5, 'wealth': 4, 'energy': 3}}
    payload = {'current_state': current,
        'current_event': {'title': '季度体力行动分配', 'decision_type': 'energy_action'},
        'choices': [{'choice': 1, 'action': '学习', 'energy_cost': 1,
                     'status_updates': {'Skill': 1}}], 'events': ''}
    observation.write_text(json.dumps(payload))
    call(root, 'refresh-context.py', str(observation))
    decision = json.loads(call(root, 'workflow-state.py', 'wait').stdout)
    assert decision['推荐选项'] == 1
    assert json.loads(call(root, 'read-context.py').stdout) == decision
    log = write_action_log(root / (SESSION + '.log'), payload, 1, {'Skill': 1})
    call(root, 'record-review.py', str(log))
    path = root / 'notebooks/workflow-state.json'
    workflow = json.loads(path.read_text())
    assert workflow['session_id'] == SESSION and workflow['workspace'] == str(workspace)
    assert workflow['phase'] == 'reviewed'
    observation.write_text(json.dumps({'current_state': {'session_id': SESSION},
                                       'ending_score': {'quantitative_score': 10}}))
    assert call(root, 'refresh-context.py', str(observation)).stdout.strip() == 'terminal'
    workflow = json.loads(path.read_text())
    assert workflow['session_id'] == SESSION and workflow['workspace'] == str(workspace)
    assert workflow['phase'] == 'terminal'
    assert not (root / 'notebooks/session.json').exists()
