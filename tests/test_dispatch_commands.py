"""Event-bound native calls keep fresh names and target only pending translators."""
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

import pytest


SKILL = Path(__file__).resolve().parents[1] / 'solution/skills/observe-decide-review'


@pytest.fixture
def dispatch_runtime(tmp_path):
    root = tmp_path / 'skill'
    shutil.copytree(SKILL, root, ignore=shutil.ignore_patterns('__pycache__'))
    notes = root / 'notebooks'
    state = runpy.run_path(str(root / 'scripts/translation-state.py'))
    state['save'](notes / 'workflow-state.json', {'session_id': 'game'})
    dispatch = runpy.run_path(str(root / 'scripts/dispatch.py'))

    def event(number, *, official=False, phase='analyse_running'):
        observation = {
            'current_state': {'session_id': 'game', 'time': {'current_month': 1}},
            'current_event': {'title': f'事件{number}', 'description': '讨论项目'},
            'choices': [{'choice': 1, 'action': '提出方案'}],
        }
        if official:
            observation['choices'][0]['status_updates'] = {'skill': 1}
        state['prepare'](notes, observation, f'{number:05d}')
        state['save'](notes / 'workflow-state.json', {
            'session_id': 'game', 'event_id': f'{number:05d}', 'phase': phase})
        return state['current_task'](notes)

    def complete(task, *, skip=()):
        directory = state['event_dir'](notes, task)
        ids = [str(c['choice']) for c in task['choices']
               if not isinstance(c.get('status_updates'), dict)]
        for metric in state['GROUPS']:
            if metric not in skip:
                state['save'](directory / metric / 'complete.json', {
                    'observation_hash': task['observation_hash'],
                    'script_hash': task['scripts'][metric],
                    'values': {choice: {letter: 0 for letter in metric} for choice in ids},
                })

    return root, notes, state, dispatch, event, complete


def invoke(root, *args):
    return subprocess.run([sys.executable, str(root / 'scripts/dispatch.py'), *args],
                          capture_output=True, text=True, timeout=5)


def test_first_dispatch_cli_has_exact_native_arguments_and_minimal_prompts(dispatch_runtime):
    root, notes, state, dispatch, event, _ = dispatch_runtime
    task = event(1)
    result = invoke(root)
    assert result.returncode == 0, result.stderr
    calls = json.loads(result.stdout)['commands']
    assert [c['tool'] for c in calls] == ['spawn_teammate'] * 5 + ['send_message'] * 5
    for metric, spawn, dm in zip(state['GROUPS'], calls[:5], calls[5:]):
        name = f'{metric.lower()}-1'
        assert spawn['arguments'] == {
            'member_name': name, 'display_name': metric, 'desc': '',
            'prompt': f'仅在收到 Leader 的“{name}” DM 后，严格读取并遵守 '
                      f'{root}/stages/translate_{metric}.md，不偏离。',
        }
        assert dm == {'tool': 'send_message', 'arguments': {'to': name, 'content': name}}
    assert state['read'](notes / 'dispatch-state.json') == dispatch['batch'](task)


def test_next_batch_spawns_before_shutdown_then_dispatches_after_skipped_events(dispatch_runtime):
    root, notes, _, dispatch, event, complete = dispatch_runtime
    first = event(1)
    dispatch['commands']()
    complete(first)
    previous = (notes / 'dispatch-state.json').read_bytes()
    event(2, official=True, phase='decide_running')
    result = invoke(root)
    assert result.returncode == 2 and result.stdout == ''
    assert (notes / 'dispatch-state.json').read_bytes() == previous
    event(9)
    calls = dispatch['commands']()['commands']
    assert [c['tool'] for c in calls] == (
        ['spawn_teammate'] * 5 + ['shutdown_member'] * 5 + ['send_message'] * 5)
    assert [c['arguments']['member_name'] for c in calls[:5]] == ['o-9', 'n-9', 's-9', 'hw-9', 'r-9']
    assert [c['arguments'] for c in calls[5:10]] == [
        {'member_name': name, 'force': False} for name in ('o-1', 'n-1', 's-1', 'hw-1', 'r-1')]
    assert [c['arguments']['to'] for c in calls[10:]] == ['o-9', 'n-9', 's-9', 'hw-9', 'r-9']


def test_retry_only_resends_current_unfinished_roles_and_never_rotates(dispatch_runtime):
    _, notes, _, dispatch, event, complete = dispatch_runtime
    task = event(12)
    dispatch['commands']()
    before = (notes / 'dispatch-state.json').read_bytes()
    complete(task, skip=('N', 'HW'))
    expected = {'commands': [
        {'tool': 'send_message', 'arguments': {'to': 'n-12', 'content': 'n-12'}},
        {'tool': 'send_message', 'arguments': {'to': 'hw-12', 'content': 'hw-12'}},
    ]}
    assert dispatch['commands']() == expected
    assert dispatch['commands']() == expected
    assert (notes / 'dispatch-state.json').read_bytes() == before
    complete(task)
    assert dispatch['commands']() == {'commands': []}


def test_already_complete_event_does_not_create_unneeded_members(dispatch_runtime):
    _, notes, _, dispatch, event, complete = dispatch_runtime
    task = event(1)
    complete(task)
    assert dispatch['commands']() == {'commands': []}
    assert not (notes / 'dispatch-state.json').exists()


def test_cannot_retire_an_unfinished_batch(dispatch_runtime):
    _, notes, _, dispatch, event, complete = dispatch_runtime
    first = event(1)
    dispatch['commands']()
    complete(first, skip=('R',))
    previous = (notes / 'dispatch-state.json').read_bytes()
    event(2)
    with pytest.raises(ValueError, match='unfinished translator batch'):
        dispatch['commands']()
    assert (notes / 'dispatch-state.json').read_bytes() == previous


@pytest.mark.parametrize('corruption', ['session', 'members', 'receipt', 'observation', 'earlier', 'phase'])
def test_mismatched_state_never_emits_calls_or_replaces_saved_batch(dispatch_runtime, corruption):
    root, notes, state, dispatch, event, complete = dispatch_runtime
    first = event(3)
    dispatch['commands']()
    complete(first)
    path = notes / 'dispatch-state.json'
    saved = state['read'](path)
    if corruption == 'session':
        state['save'](path, {**saved, 'session_id': 'other-game'})
    elif corruption == 'members':
        saved['members']['O'] = 'wrong-target'
        state['save'](path, saved)
    elif corruption == 'receipt':
        receipt = state['event_dir'](notes, first) / 'R/complete.json'
        state['save'](receipt, {**state['read'](receipt), 'observation_hash': 'stale'})
        event(4)
    elif corruption == 'observation':
        state['save'](notes / 'translation-context.json', {**first, 'observation_hash': 'changed'})
    elif corruption == 'earlier':
        event(2)
    elif corruption == 'phase':
        state['save'](notes / 'workflow-state.json', {
            'session_id': 'game', 'event_id': '00003', 'phase': 'decide_running'})
    before = path.read_bytes()
    result = invoke(root)
    assert result.returncode == 2 and result.stdout == ''
    assert result.stderr.startswith('dispatch_error:')
    assert path.read_bytes() == before


def test_cli_rejects_arguments_and_official_delta_events_without_issuing_batch(dispatch_runtime):
    root, notes, _, _, event, _ = dispatch_runtime
    event(1)
    result = invoke(root, '1')
    assert result.returncode == 2 and result.stdout == ''
    assert 'no arguments' in result.stderr
    event(2, official=True)
    result = invoke(root)
    assert result.returncode == 2 and result.stdout == ''
    assert 'direct decide' in result.stderr
    assert not (notes / 'dispatch-state.json').exists()


def test_208_events_have_1040_unique_members_and_never_shutdown_the_current_batch(dispatch_runtime):
    _, _, _, dispatch, event, complete = dispatch_runtime
    roster = {}
    for number in range(1, 209):
        task = event(number)
        calls = dispatch['commands']()['commands']
        assert len(calls) == (10 if number == 1 else 15)
        for command in calls:
            args = command['arguments']
            if command['tool'] == 'spawn_teammate':
                assert args['member_name'] not in roster
                roster[args['member_name']] = True
            elif command['tool'] == 'shutdown_member':
                assert roster[args['member_name']]
                assert not args['member_name'].endswith(f'-{number}')
                roster[args['member_name']] = False
                assert sum(roster.values()) >= 5
            else:
                assert command['tool'] == 'send_message'
                assert roster[args['to']] and args['to'] == args['content']
        complete(task)
    assert len(roster) == 1040
    assert {name for name, alive in roster.items() if alive} == {
        'o-208', 'n-208', 's-208', 'hw-208', 'r-208'}
