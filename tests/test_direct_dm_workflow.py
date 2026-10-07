"""Direct messages use the existing event receipts, without dispatch IDs."""

import json
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from tests.review_logs import write_action_log


SKILL = Path(__file__).resolve().parents[1] / 'solution/skills/observe-decide-review'


def test_wait_can_restart_after_process_kill_and_repeat_after_completion(tmp_path):
    root = tmp_path / 'skill'
    shutil.copytree(SKILL, root)
    workflow = runpy.run_path(str(root / 'scripts/workflow-state.py'))
    state = workflow['state']
    notes = root / 'notebooks'
    state['save'](notes / 'workflow-state.json', {'session_id': 'game'})
    current = {'session_id': 'game', 'time': {'current_month': 1},
               'status': dict(level='L1', output=2, skill=6, network=3, health=5,
                              dignity=5, wealth=4, energy=3)}
    observation = {
        'current_state': current,
        'current_event': {'title': 'event', 'description': 'story'},
        'choices': [{'choice': 1, 'action': 'action'}], 'events': '',
    }
    path = tmp_path / 'observe.json'
    state['save'](path, observation)
    subprocess.run([sys.executable, str(root / 'scripts/refresh-context.py'), str(path)],
                   capture_output=True, text=True, check=True)
    task = state['current_task']()
    directory = state['event_dir'](notes, task)

    def complete(metric):
        state['save'](directory / metric / 'complete.json', {
            'observation_hash': task['observation_hash'],
            'script_hash': task['scripts'][metric],
            'values': {'1': {letter: 0 for letter in metric}},
        })

    for metric in ('O', 'N', 'S', 'HW'):
        complete(metric)
    command = [sys.executable, str(root / 'scripts/workflow-state.py'), 'wait']

    def snapshot():
        return {str(p.relative_to(notes)): p.read_bytes() for p in notes.rglob('*') if p.is_file()}

    before = snapshot()
    for _ in range(2):
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                process.communicate(timeout=0.25)
        finally:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
        assert stdout == stderr == ''
        assert snapshot() == before

    complete('R')
    result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=5)
    assert json.loads(result.stdout)['current_event'] == observation['current_event']
    assert state['read'](notes / 'workflow-state.json')['phase'] == 'decide_running'

    # Same disk state as a completed wait whose stdout was lost on timeout.
    completed = snapshot()
    replay = subprocess.run(command, capture_output=True, text=True, check=True, timeout=5)
    assert replay.stdout == result.stdout
    assert snapshot() == completed
    assert not (directory / 'action.json').exists()


def test_wait_before_first_event_is_rejected_without_creating_receipts(tmp_path):
    root = tmp_path / 'skill'
    shutil.copytree(SKILL, root)
    workflow = runpy.run_path(str(root / 'scripts/workflow-state.py'))
    with pytest.raises(ValueError, match='wait is unavailable during phase: None'):
        workflow['wait']()
    assert not (root / 'notebooks/handbook-ready.json').exists()
    assert not (root / 'notebooks/workflow-state.json').exists()


def test_synchronous_dm_workflow_uses_event_receipts(tmp_path, monkeypatch):
    root = tmp_path / 'skill'
    shutil.copytree(SKILL, root)
    workflow = runpy.run_path(str(root / 'scripts/workflow-state.py'))

    def pause(_):
        raise InterruptedError('still waiting')

    monkeypatch.setattr(workflow['time'], 'sleep', pause)
    state = workflow['state']
    notes = root / 'notebooks'
    state['save'](notes / 'workflow-state.json', {'session_id': 'game'})
    with pytest.raises(ValueError, match='wait is unavailable during phase: None'):
        workflow['wait']()

    observation = {
        'current_state': {'session_id': 'game', 'time': {'current_month': 1},
                          'status': dict(level='L1', output=2, skill=6, network=3,
                                         health=5, dignity=5, wealth=4, energy=3)},
        'current_event': {'title': 'event', 'description': 'story'},
        'choices': [{'choice': 1, 'action': 'action'}], 'events': '',
    }
    path = tmp_path / 'observation.json'
    path.write_text(json.dumps(observation))

    def call(script, *args):
        return subprocess.run([sys.executable, str(root / 'scripts' / script), *args],
                              capture_output=True, text=True, check=True)

    assert call('refresh-context.py', str(path)).stdout.strip() == '请进入 dispatch 阶段，分配任务'
    assert state['read'](notes / 'workflow-state.json')['phase'] == 'analyse_running'
    assert not (notes / 'dispatch.json').exists()
    assert not (notes / 'dispatches').exists()
    with pytest.raises(InterruptedError, match='still waiting'):
        workflow['wait']()

    task = state['current_task']()
    directory = state['event_dir'](notes, task)
    for metric in state['GROUPS']:
        state['save'](directory / metric / 'complete.json', {
            'observation_hash': task['observation_hash'],
            'script_hash': task['scripts'][metric],
            'values': {'1': {letter: 0 for letter in metric}},
        })
        if metric != 'R':
            with pytest.raises(InterruptedError, match='still waiting'):
                workflow['wait']()
    # Completed answers are usable before wait advances the internal phase.
    early = json.loads(call('read-context.py').stdout)
    assert state['read'](notes / 'workflow-state.json')['phase'] == 'analyse_running'
    assert workflow['wait']() == early
    assert json.loads(call('read-context.py').stdout) == early
    log = write_action_log(tmp_path / 'game.log', observation, 1)
    assert call('record-review.py', str(log)).stdout.strip() == 'review_recorded'
    assert state['read'](notes / 'workflow-state.json')['phase'] == 'reviewed'
    assert call('refresh-context.py', str(path)).stdout.strip() == '请进入 dispatch 阶段，分配任务'
    assert state['current_task']()['event_id'] == '00002'


@pytest.mark.parametrize('title,updates,cost,metrics', [
    ('季度体力分配', {'Output': 2, 'Health': -1, 'HiddenRisk': -2}, 2,
     dict(O=2, N=0, S=0, H=-1, W=0, R=-2)),
    ('季度主行动', {'Skill': 5, 'Wealth': -3}, 0,
     dict(O=0, N=0, S=5, H=0, W=-3, R=0)),
])
def test_business_clis_advance_the_workflow(tmp_path, title, updates, cost, metrics):
    root = tmp_path / 'skill'
    shutil.copytree(SKILL, root)
    workflow = runpy.run_path(str(root / 'scripts/workflow-state.py'))
    state = workflow['state']
    notes = root / 'notebooks'
    state['save'](notes / 'workflow-state.json', {'session_id': 'game'})
    state['save'](notes / 'handbook-ready.json', {'session_id': 'game'})

    observation = {
        'current_state': {'session_id': 'game', 'time': {'current_month': 1},
                          'status': dict(level='L1', output=2, skill=6, network=3,
                                         health=5, dignity=5, wealth=4, energy=3)},
        'current_event': {'title': title, 'description': 'story'},
        'choices': [{'choice': 1, 'action': 'action', 'status_updates': updates, 'energy_cost': cost}],
        'events': '',
    }
    path = tmp_path / 'observation.json'
    path.write_text(json.dumps(observation))

    def call(script, *args):
        return subprocess.run([sys.executable, str(root / 'scripts' / script), *args],
                              capture_output=True, text=True, check=True)

    assert call('refresh-context.py', str(path)).stdout.strip() == '可直接进入 decide 阶段 read-context'
    assert workflow['phase']() == 'decide_running'
    assert not list(notes.rglob('.policy.json'))
    assert state['option_metrics']() == [
        {'choice': 1, 'metrics': metrics, 'selectable': True, 'energy_cost': cost}]
    log = write_action_log(tmp_path / 'game.log', observation, 1)
    assert call('record-review.py', str(log)).stdout.strip() == 'review_recorded'
    assert workflow['phase']() == 'reviewed'
    path.write_text(json.dumps({'current_state': observation['current_state'],
                               'ending_score': {'completed': True, 'survival_months': 48}}))
    assert call('refresh-context.py', str(path)).stdout.strip() == 'terminal'
    assert workflow['phase']() == 'terminal'
