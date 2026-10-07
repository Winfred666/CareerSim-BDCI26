"""A single Leader call waits for receipts and returns the current decision packet."""
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

import pytest


SKILL = Path(__file__).resolve().parents[1] / 'solution/skills/observe-decide-review'


@pytest.fixture
def pending(tmp_path):
    root = tmp_path / 'skill'
    shutil.copytree(SKILL, root, ignore=shutil.ignore_patterns('__pycache__'))
    notes = root / 'notebooks'
    state = runpy.run_path(str(root / 'scripts/translation-state.py'))
    state['save'](notes / 'workflow-state.json', {'session_id': 'game'})
    observation = {
        'current_state': {'session_id': 'game', 'time': {'current_month': 1},
                          'status': dict(level='L1', output=2, skill=6, network=3,
                                         health=5, dignity=5, wealth=4, energy=3)},
        'current_event': {'title': '公开剧情', 'description': '讨论项目'},
        'choices': [{'choice': 1, 'action': '提出方案'}], 'events': '',
    }
    path = tmp_path / 'observation.json'
    path.write_text(json.dumps(observation))
    subprocess.run([sys.executable, str(root / 'scripts/refresh-context.py'), str(path)],
                   capture_output=True, text=True, check=True)
    task = state['current_task'](notes)
    directory = state['event_dir'](notes, task)

    def complete(skill_gain=0, skip=None, safe_growth=False, output_gain=0):
        values = {'S': skill_gain, 'O': output_gain}
        if safe_growth:
            values.update(O=2, N=2, H=1, W=1, R=-1)
        for metric in state['GROUPS']:
            if metric == skip:
                continue
            state['save'](directory / metric / 'complete.json', {
                'observation_hash': task['observation_hash'],
                'script_hash': task['scripts'][metric],
                'values': {'1': {letter: values.get(letter, 0) for letter in metric}},
            })

    return root, notes, complete


def invoke(root, *args):
    return subprocess.run([sys.executable, str(root / 'scripts/workflow-state.py'), *args],
                          capture_output=True, text=True, timeout=5)


@pytest.mark.parametrize('skill_gain,safe_growth,output_gain,correction', [
    (0, False, 0, True), (1, False, 0, False), (1, True, 0, False), (0, False, 1, True),
])
def test_wait_defaults_to_current_packet_and_semantic_fallback(pending, skill_gain, safe_growth, output_gain, correction):
    root, notes, complete = pending
    complete(skill_gain, safe_growth=safe_growth, output_gain=output_gain)
    expected = subprocess.run([sys.executable, str(root / 'scripts/read-context.py')],
                              capture_output=True, text=True, check=True).stdout
    combined = invoke(root, 'wait')
    assert combined.returncode == 0, combined.stderr
    assert combined.stdout == expected
    packet = json.loads(combined.stdout)
    assert ('current_event' in packet) == correction
    assert ('推荐选项' in packet) != correction
    if correction:
        assert set(packet['constraints']) == {'守红线', '晋升条件'}
        assert {k:v for k,v in packet.items() if k!='constraints'} == {
            'current_event': {'title': '公开剧情', 'description': '讨论项目'},
            'options': [{'choice': 1, 'action': '提出方案'}],
        }
    else:
        assert set(packet) == {'推荐选项', 'notes'}
        assert packet['推荐选项'] == 1
        assert '专业技能+1' in packet['notes']
    assert json.loads((notes / 'workflow-state.json').read_text())['phase'] == 'decide_running'
    assert invoke(root, 'wait').stdout == expected
    assert not list(notes.rglob('action.json'))
    workflow = runpy.run_path(str(root / 'scripts/workflow-state.py'))
    assert workflow['wait'](timeout_s=0) == packet


def test_wait_context_does_not_read_context_before_all_roles_complete(pending):
    root, notes, complete = pending
    complete(skip='R')
    command = [sys.executable, str(root / 'scripts/workflow-state.py'), 'wait']
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            process.communicate(timeout=0.3)
    finally:
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
    assert stdout == stderr == ''
    assert json.loads((notes / 'workflow-state.json').read_text())['phase'] == 'analyse_running'
    complete()
    assert invoke(root, 'wait').returncode == 0


def test_wait_context_terminal_skips_context_reader(pending):
    root, notes, _ = pending
    path = notes / 'workflow-state.json'
    state = json.loads(path.read_text())
    path.write_text(json.dumps({**state, 'phase': 'terminal'}))
    (root / 'scripts/read-context.py').unlink()
    result = invoke(root, 'wait')
    assert result.returncode == 0 and result.stdout.strip() == 'terminal'


def test_context_failure_is_distinct_from_wait_failure(pending):
    root, notes, complete = pending
    complete()
    (notes / 'redline-guardian-keeps.tsv').unlink()
    result = invoke(root, 'wait')
    assert result.returncode == 2 and result.stdout == ''
    assert result.stderr.startswith('context_error:')


def test_removed_context_flag_is_rejected_without_advancing_workflow(pending):
    root, notes, complete = pending
    complete()
    before = (notes / 'workflow-state.json').read_bytes()
    result = invoke(root, 'wait', '--context')
    assert result.returncode == 2 and result.stdout == ''
    assert result.stderr.startswith('workflow_error: usage:')
    assert (notes / 'workflow-state.json').read_bytes() == before


@pytest.mark.parametrize('phase', [None, 'observed', 'observe_running', 'reviewed', 'review_running'])
def test_wait_rejects_phases_without_a_pending_decision(pending, phase):
    root, notes, _ = pending
    path = notes / 'workflow-state.json'
    current = json.loads(path.read_text())
    path.write_text(json.dumps({**current, 'phase': phase}))
    before = path.read_bytes()
    result = invoke(root, 'wait')
    assert result.returncode == 2 and result.stdout == ''
    assert 'wait is unavailable during phase:' in result.stderr
    assert path.read_bytes() == before


def test_wait_rejects_workflow_event_mismatch(pending):
    root, notes, complete = pending
    complete()
    path = notes / 'workflow-state.json'
    current = json.loads(path.read_text())
    path.write_text(json.dumps({**current, 'event_id': '00002'}))
    result = invoke(root, 'wait')
    assert result.returncode == 2 and result.stdout == ''
    assert 'wait event does not match current workflow' in result.stderr


def test_wait_cannot_return_a_decision_for_an_already_executed_action(pending):
    root, notes, complete = pending
    complete()
    assert invoke(root, 'wait').returncode == 0
    state = runpy.run_path(str(root / 'scripts/translation-state.py'))
    task = state['current_task'](notes)
    state['save'](state['event_dir'](notes, task) / 'action.json', {
        'event_id': task['event_id'], 'observation_hash': task['observation_hash'], 'choice': 1})
    result = invoke(root, 'wait')
    assert result.returncode == 2 and result.stdout == ''
    assert 'event already acted' in result.stderr
