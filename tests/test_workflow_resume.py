"""Resume a paused event without replaying completed translations or actions."""
import json
from pathlib import Path
import runpy

import pytest

from tests.test_silent_workflow import complete, runtime as runtime
from tests.review_logs import write_action_log


def pending(runtime):
    root, notes, observation, path, state, refresh, _ = runtime
    (notes / 'handbook-ready.json').write_text(json.dumps({'session_id': 'game'}))
    state['begin_stage']('observe')
    refresh['refresh_observation'](notes, path, emit=False)
    state['finish_stage']('observe')
    state['begin_stage']('analyse')
    return runpy.run_path(str(root / 'scripts/workflow-state.py'))


def test_resume_dispatches_only_missing_role(runtime):
    workflow = pending(runtime)
    _, notes, _, path, state, _, _ = runtime
    for metric in ('O', 'S', 'HW', 'R'):
        complete(state, metric, notes)
    assert workflow['resume']()['stage'] == 'synchronize'
    assert workflow['resume'](path)['members'] == ['n-1']
    assert workflow['wait'](timeout_s=0) == 'waiting'
    assert workflow['resume'](path)['members'] == ['n-1']
    with pytest.raises(ValueError, match='completion timeout: N'):
        workflow['wait'](timeout_s=0)

    complete(state, 'N', notes)
    context = runpy.run_path(str(runtime[0] / 'scripts/read-context.py'))
    assert workflow['wait'](timeout_s=0) == context['read_context']()


def test_resume_preserves_completed_event_and_enters_decide(runtime):
    workflow = pending(runtime)
    _, notes, _, path, state, _, _ = runtime
    for metric in ('O', 'N', 'S', 'HW', 'R'):
        complete(state, metric, notes)
    before = (notes / 'translation-context.json').read_bytes()
    assert workflow['resume'](path)['stage'] == 'decide'
    assert workflow['phase']() == 'decide_running'
    assert (notes / 'translation-context.json').read_bytes() == before


def test_resume_refuses_changed_game_observation(runtime):
    workflow = pending(runtime)
    _, _, observation, path, _, _, _ = runtime
    observation['current_state']['status']['skill'] += 1
    path.write_text(json.dumps(observation))
    with pytest.raises(ValueError, match='does not match pending event'):
        workflow['resume'](path)


def test_resume_review_does_not_request_another_action(runtime, monkeypatch):
    workflow = pending(runtime)
    root, notes, observation, path, state, _, _ = runtime
    for metric in ('O', 'N', 'S', 'HW', 'R'):
        complete(state, metric, notes)
    workflow['resume'](path)
    log = write_action_log(root / 'game.log', observation, 1)
    record = runpy.run_path(str(root / 'scripts/record-review.py'))['record_log']
    def fail(*_):
        raise OSError('interrupted review')
    monkeypatch.setitem(record.__globals__, 'record', fail)
    with pytest.raises(OSError, match='interrupted review'):
        record(notes, log)
    assert workflow['resume']() == {'session_id': 'game', 'stage': 'review'}


def test_resume_recovers_confirmed_log_before_review_stage_was_saved(runtime, monkeypatch):
    workflow = pending(runtime)
    root, notes, observation, path, state, _, _ = runtime
    for metric in ('O', 'N', 'S', 'HW', 'R'):
        complete(state, metric, notes)
    workflow['resume'](path)
    log = write_action_log(root / 'game.log', observation, 1)
    record = runpy.run_path(str(root / 'scripts/record-review.py'))['record_log']
    original = record.__globals__['runpy'].run_path
    def interrupted_namespace(*args, **kwargs):
        loaded = original(*args, **kwargs)
        def fail(*_):
            raise OSError('interrupted stage write')
        if Path(args[0]).name == 'translation-state.py':
            loaded['begin_stage'] = fail
        return loaded
    monkeypatch.setattr(record.__globals__['runpy'], 'run_path', interrupted_namespace)
    with pytest.raises(OSError, match='interrupted stage write'):
        record(notes, log)
    assert workflow['phase']() == 'decide_running'
    assert workflow['resume']() == {'session_id': 'game', 'stage': 'review'}
