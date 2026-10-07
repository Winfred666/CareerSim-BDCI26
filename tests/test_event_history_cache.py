"""The live ready packet restores only executed history across interleaved events."""
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

import pytest
from tests.review_logs import write_action_log

SKILL = Path(__file__).resolve().parents[1] / 'solution/skills/observe-decide-review'
TITLE = '应酬席上客户开越界玩笑长链'


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / 'skill'
    shutil.copytree(SKILL, root)
    state = runpy.run_path(str(root / 'scripts/translation-state.py'))
    notes = root / 'notebooks'
    state['save'](notes / 'workflow-state.json', {'session_id': 'game'})
    return root, notes, state


def observation(title, description, month=1, **extra):
    return {
        'current_state': {'session_id': 'game', 'time': {'current_month': month},
                          'status': dict(level='L1', output=2, skill=6, network=3,
                                         health=5, dignity=5, wealth=4, energy=3)},
        'current_event': {'title': title, 'description': description},
        'choices': [
            {'choice': 1, 'action': '未选行动', 'description': '未发生细节'},
            {'choice': 2, 'action': '岔开话题帮助小周', 'description': '实际行动细节'},
        ], **extra,
    }


def act(runtime, event_id, choice=2):
    root, notes, state = runtime
    state['save'](notes / 'workflow-state.json', {**state['read'](notes / 'workflow-state.json'), 'phase': 'decide_running', 'event_id': event_id})
    task = state['current_task'](notes)
    directory = state['event_dir'](notes, task)
    public = state['read'](directory / 'public-event.json')
    current = observation('', '', public['current_month'])['current_state']
    state['save'](notes / 'review-context.json', {'event_key': event_id,
                                               'current_state': current, 'choices': task['choices']})
    (notes / 'redline-guardian-keeps.tsv').write_text(
        'event_key\tstate\tlabels\n'
        f'{int(event_id) - 1:05d}\tL=1;O2;S6;N3;H5;D5;W4;R0\t已观察\n')
    for metric in state['GROUPS']:
        state['save'](directory / metric / 'complete.json', {
            'observation_hash': task['observation_hash'], 'script_hash': task['scripts'][metric],
            'values': {str(c['choice']): {m: 0 for m in metric} for c in task['choices']}})
    log = root / 'game.log'
    previous = log.read_text() if log.exists() else ''
    write_action_log(log, {'current_state': current, **public}, choice)
    if previous:
        log.write_text(previous + log.read_text().split('\n', 1)[1])
    subprocess.run([sys.executable, str(root / 'scripts/record-review.py'), str(log)],
                   cwd=root.parent, text=True, capture_output=True, check=True)


def test_live_ready_restores_both_actions_after_other_events_and_quarters(runtime):
    root, notes, state = runtime
    first = observation(TITLE, '饭局上客户对小周开越界玩笑')
    state['prepare'](notes, first, '00001')
    assert 'event_history' not in state['answer']('O', 'ready', notes)['event']
    act(runtime, '00001')

    # Even a title sharing the same keywords must keep its own history.
    other = observation(TITLE + '·另一客户', '另一场饭局', 4)
    state['prepare'](notes, other, '00002')
    assert 'event_history' not in state['answer']('N', 'ready', notes)['event']
    act(runtime, '00002', 1)
    menu = observation('季度体力分配', '当季行动', 4)
    menu['choices'] = [{'choice': 1, 'action': '运动', 'status_updates': {'Health': 1}}]
    state['prepare'](notes, menu, '00003')
    act(runtime, '00003', 1)

    second = observation('  ' + TITLE + '\n', '小周感谢你，现在考虑向领导同步', 7)
    second['choices'][1]['action'] = '向领导客观同步并商定后续安排'
    state['prepare'](notes, second, '00004')
    expected = [{'description': first['current_event']['description'], 'current_month': 1,
                 'selected_choice': {'action': '岔开话题帮助小周', 'description': '实际行动细节'}}]
    packets = [state['answer'](g, 'ready', notes) for g in state['GROUPS']]
    for packet in packets:
        assert packet['event']['event_history'] == {'steps': expected}
        assert packet['event']['current_month'] == 7
        assert 'choices' not in packet['event']
        assert '另一场饭局' not in json.dumps(packet, ensure_ascii=False)
        assert '未选行动' not in json.dumps(packet['event'], ensure_ascii=False)
        assert '向领导客观同步并商定后续安排' not in json.dumps(packet['event'], ensure_ascii=False)
    act(runtime, '00004')
    expected.append({'description': second['current_event']['description'], 'current_month': 7,
                     'selected_choice': {'action': '向领导客观同步并商定后续安排', 'description': '实际行动细节'}})

    # The dedicated dictionary is sufficient after losing previous event/lane files.
    shutil.rmtree(notes / 'translations')
    (notes / 'translation-context.json').unlink()
    state = runpy.run_path(str(root / 'scripts/translation-state.py'))
    state['prepare'](notes, observation(TITLE, '又一次应酬前，公司安排人员', 13), '00005')
    for group in state['GROUPS']:
        packet = state['answer'](group, 'ready', notes)
        assert packet['event']['event_history'] == {'steps': expected}
        assert packet['event']['current_month'] == 13
        history = json.dumps(packet['event']['event_history'], ensure_ascii=False)
        for forbidden in ('未选行动', '未发生细节', 'status_updates', 'event_id', 'source', 'selectable'):
            assert forbidden not in history
    cache = state['read'](notes / 'event-history.json')['game']
    assert cache[TITLE]['steps'] == expected
    assert '季度体力分配' not in cache


def test_unmarked_narrative_is_cached_and_new_game_is_isolated(runtime):
    _, notes, state = runtime
    title = '自研内核 vs 开源方案技术选型'
    state['prepare'](notes, observation(title, '选型开始'), '00001')
    act(runtime, '00001')
    state['prepare'](notes, observation('其他项目', '中间事件', 3), '00002')
    act(runtime, '00002')
    state['prepare'](notes, observation(title, '选型进入评审', 6), '00003')
    assert state['answer']('S', 'ready', notes)['event']['event_history']['steps'][0]['description'] == '选型开始'

    state['save'](notes / 'workflow-state.json', {'session_id': 'new-game'})
    fresh = observation(title, '新对局中的选型开始')
    fresh['current_state']['session_id'] = 'new-game'
    state['prepare'](notes, fresh, '00001')
    assert 'event_history' not in state['answer']('S', 'ready', notes)['event']
    assert title.replace(' ', '') in state['read'](notes / 'event-history.json')['game']


def test_observed_but_unexecuted_action_never_becomes_history(runtime):
    _, notes, state = runtime
    state['prepare'](notes, observation(TITLE, '只观察未行动'), '00001')
    state['prepare'](notes, observation('其他事件', '实际发生的其他事件'), '00002')
    act(runtime, '00002')
    state['prepare'](notes, observation(TITLE, '下一情境'), '00003')
    assert 'event_history' not in state['answer']('R', 'ready', notes)['event']


def test_reprepare_and_action_receipt_retry_do_not_duplicate_history(runtime):
    _, notes, state = runtime
    first = observation(TITLE, '首次情境')
    state['prepare'](notes, first, '00001')
    original = state['answer']('O', 'ready', notes)
    state['save'](notes / 'workflow-state.json', {**state['read'](notes / 'workflow-state.json'), 'phase': 'decide_running', 'event_id': '00001'})
    act(runtime, '00001')
    before = (notes / 'event-history.json').read_bytes()
    subprocess.run([sys.executable, str(runtime[0] / 'scripts/record-review.py'),
                    str(runtime[0] / 'game.log')], text=True, capture_output=True, check=True)
    state['prepare'](notes, first, '00001')
    assert state['answer']('O', 'ready', notes) == original
    assert (notes / 'event-history.json').read_bytes() == before
    state['prepare'](notes, observation(TITLE, '下一情境', 4), '00002')
    assert len(state['answer']('O', 'ready', notes)['event']['event_history']['steps']) == 1


def test_missing_cache_rebuilds_interleaved_confirmed_actions_without_duplicates(runtime):
    _, notes, state = runtime
    for event_id, title, description, month in [
            ('00001', TITLE, '长链第一段', 1),
            ('00002', '其他事件', '无关事件', 2),
            ('00003', TITLE, '长链第二段', 4)]:
        state['prepare'](notes, observation(title, description, month), event_id)
        act(runtime, event_id)
    # Simulate pre-cache event snapshots where only adjacent events carried history.
    for public_path in (notes / 'translations').glob('*/*/public-event.json'):
        public = state['read'](public_path)
        public.pop('event_history', None)
        state['save'](public_path, public)
    (notes / 'event-history.json').unlink()
    state['prepare'](notes, observation(TITLE, '长链第三段', 7), '00004')
    steps = state['answer']('HW', 'ready', notes)['event']['event_history']['steps']
    assert [step['description'] for step in steps] == ['长链第一段', '长链第二段']
    assert [step['current_month'] for step in steps] == [1, 4]


def test_supplied_benchmark_history_is_kept_and_extended_with_actual_action(runtime):
    _, notes, state = runtime
    supplied = {'source': 'internal bookkeeping', 'steps': [{
        'description': '已知前置情境', 'event_id': 'hidden-id',
        'selected_choice': {'choice': 1, 'action': '已执行前置行动', 'status_updates': {'N': 999}},
    }]}
    state['prepare'](notes, observation(TITLE, '当前题', 3, event_history=supplied), '00001')
    packet = state['answer']('N', 'ready', notes)
    clean = [{'description': '已知前置情境', 'selected_choice': {'action': '已执行前置行动'}}]
    assert packet['event']['event_history'] == {'steps': clean}
    act(runtime, '00001')
    state['prepare'](notes, observation(TITLE, '后续题', 6), '00002')
    steps = state['answer']('N', 'ready', notes)['event']['event_history']['steps']
    assert steps[:-1] == clean
    assert steps[-1]['description'] == '当前题'
