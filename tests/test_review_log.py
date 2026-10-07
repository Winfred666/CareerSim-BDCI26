"""Review uses public logs without a leader-supplied choice or metric delta."""
import json
from pathlib import Path
import runpy
import shutil

import pytest

from tests.review_logs import write_action_log

SKILL = Path(__file__).resolve().parents[1] / 'solution/skills/observe-decide-review'


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / 'skill'
    shutil.copytree(SKILL, root)
    notes = root / 'notebooks'
    (notes / 'workflow-state.json').write_text('{"session_id":"game"}')
    observation = {
        'current_state': {'session_id': 'game', 'time': {'current_month': 6},
                          'status': dict(level='L4', output=26, skill=86, network=36,
                                         health=9, dignity=1, wealth=5, energy=3)},
        'current_event': {'title': '季度主行动选择', 'description': '公开题面'},
        'choices': [{'choice': 2, 'action': '学习并支付费用', 'status_updates': {}}],
        'events': '',
    }
    path = root / 'observe.json'
    path.write_text(json.dumps(observation))
    refresh = runpy.run_path(str(root / 'scripts/refresh-context.py'))
    state = runpy.run_path(str(root / 'scripts/translation-state.py'))
    state['begin_stage']('observe', notes)
    refresh['refresh_observation'](notes, path, emit=False)
    state['finish_stage']('observe', notes)
    state['begin_stage']('analyse', notes)
    state['finish_stage']('analyse', notes)
    state['begin_stage']('decide', notes)
    module = runpy.run_path(str(root / 'scripts/record-review.py'))
    log = write_action_log(root / 'game.log', observation, 2,
                           {'Health': 3, 'Dignity': -4, 'Skill': 1, 'Wealth': -2})
    return root, notes, observation, state, module, log


def snapshot(notes):
    return {str(p.relative_to(notes)): p.read_bytes() for p in notes.rglob('*') if p.is_file()}


def test_log_review_ignores_settlements_and_is_idempotent(runtime):
    root, notes, _, state, module, log = runtime
    with log.open('a') as output:
        output.write('[event] Month 7 | 剧情结束\nHealth -9\n'
                     '[salary] Wealth +100\n[promotion] Skill -3\n'
                     '[talk] 半年谈话\n[ending_score] 终局\n')
    assert module['record_log'](notes, log) == 'review_recorded 00001'
    workflow = state['read'](notes / 'workflow-state.json')
    assert workflow['phase'] == 'reviewed'
    assert workflow['review_log']['entry'] == 0
    receipt = state['read'](state['event_dir'](notes, state['current_task'](notes)) / 'action.json')
    assert receipt['choice'] == 2
    assert receipt['updates'] == {'H': 3, 'D': -4, 'S': 1, 'W': -2}
    assert (notes / 'redline-guardian-keeps.tsv').read_text().splitlines()[-1] == (
        '00001\tL=4;绩效产出26;专业技能87;人脉36;身心健康10;尊严0;个人财富1;隐患0\t待判定')
    before = snapshot(notes)
    module['record_log'](notes, log)
    assert snapshot(notes) == before


def test_logged_dignity_loss_never_becomes_health_loss(runtime):
    _, notes, observation, state, module, log = runtime
    write_action_log(log, observation, 2, {'Dignity': -1, 'Skill': 0})
    module['record_log'](notes, log)
    receipt = state['read'](state['event_dir'](notes, state['current_task'](notes)) / 'action.json')
    assert receipt['updates'] == {'D': -1, 'S': 0}
    assert (notes / 'redline-guardian-keeps.tsv').read_text().splitlines()[-1].endswith(
        ';身心健康9;尊严0;个人财富5;隐患0\t待判定')


@pytest.mark.parametrize('old,new', [
    ('Month 6 |', 'Month 7 |'),
    ('季度主行动选择', '其他事件'),
    ('Chose #2:', 'Chose #3:'),
    ('学习并支付费用', '做另一件事'),
    ('Health +3', 'Health +3, Health -1'),
    ('Health +3', 'Health malformed'),
    ('Chose #2:', 'Missing #2:'),
])
def test_wrong_or_malformed_latest_decision_does_not_write(runtime, old, new):
    _, notes, _, _, module, log = runtime
    log.write_text(log.read_text().replace(old, new))
    before = snapshot(notes)
    with pytest.raises(ValueError):
        module['record_log'](notes, log)
    assert snapshot(notes) == before


def test_wrong_session_log_does_not_write(runtime):
    root, notes, observation, _, module, _ = runtime
    log = write_action_log(root / 'another-game.log', observation, 2)
    before = snapshot(notes)
    with pytest.raises(ValueError, match='log session mismatch'):
        module['record_log'](notes, log)
    assert snapshot(notes) == before


def test_same_quarter_menu_cannot_consume_previous_action_again(runtime):
    _, notes, _, state, module, log = runtime
    module['record_log'](notes, log)
    old = state['current_task'](notes)
    new = {**old, 'event_id': '00002'}
    state['save'](notes / 'translation-context.json', new)
    source = state['event_dir'](notes, old)
    target = state['event_dir'](notes, new)
    shutil.copytree(source, target)
    (target / 'action.json').unlink()
    state['save'](notes / 'review-context.json', {
        **state['read'](notes / 'review-context.json'), 'event_key': '00002'})
    state['save'](notes / 'workflow-state.json', {
        **state['read'](notes / 'workflow-state.json'),
        'event_id': '00002', 'phase': 'decide_running'})
    before = snapshot(notes)
    with pytest.raises(ValueError, match='already reviewed'):
        module['record_log'](notes, log)
    assert snapshot(notes) == before


def test_interrupted_result_write_can_resume_without_repeating_action(runtime, monkeypatch):
    _, notes, _, state, module, log = runtime
    record = module['record']
    original = record.__globals__['refresh'].atomic_update
    def fail_redline(path, *args):
        if path.name == 'redline-guardian-keeps.tsv':
            raise OSError('interrupted result write')
        return original(path, *args)
    monkeypatch.setattr(record.__globals__['refresh'], 'atomic_update', fail_redline)
    with pytest.raises(OSError, match='interrupted result write'):
        module['record_log'](notes, log)
    assert state['read'](notes / 'workflow-state.json')['phase'] == 'review_running'
    monkeypatch.setattr(record.__globals__['refresh'], 'atomic_update', original)
    module['record_log'](notes, log)
    assert state['read'](notes / 'workflow-state.json')['phase'] == 'reviewed'
    assert (notes / 'redline-guardian-keeps.tsv').read_text().count('00001\t') == 1
