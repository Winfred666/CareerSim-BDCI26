"""Protocol compatibility for the conservative R and HW ports."""
import json
import runpy
import shutil
from pathlib import Path
import pytest

SKILL = Path(__file__).resolve().parents[1] / 'solution/skills/observe-decide-review'

@pytest.fixture
def event(tmp_path):
    root = tmp_path / 'skill'
    shutil.copytree(SKILL, root)
    state = runpy.run_path(str(root / 'scripts/translation-state.py'))
    notes = root / 'notebooks'
    state['save'](notes / 'workflow-state.json', {'session_id': 'test'})
    observation = {
        'current_state': {'session_id': 'test'},
        'current_event': {'title': 'TITLE {x}', 'description': 'PUBLIC STORY'},
        'event_history': {'steps': [{'description': 'PUBLIC HISTORY'}]},
        'choices': [{'choice': i, 'action': f'ACTION {i}', 'description': f'DETAIL {i}'} for i in (1, 2, 3)],
    }
    state['prepare'](notes, observation, '00001')
    return state, notes


def test_ready_discloses_same_complete_public_context(event):
    state, notes = event
    ready = {m: state['answer'](m, 'ready', notes) for m in ('R', 'HW', 'N', 'S', 'O')}
    assert all(r['event'] == ready['N']['event'] for r in ready.values())
    assert 'DETAIL 1' in ready['R']['question']
    assert ready['R']['event']['event_history']['steps'][0]['description'] == 'PUBLIC HISTORY'
    assert 'PUBLIC STORY' not in ready['R']['question']
    assert 'existing_H_load' in ready['HW']['question']
    assert 'answer_schema' not in ready['R']


def test_r_deferred_zero_and_signed_final_values(event):
    state, notes = event
    answer = lambda text: state['answer']('R', text, notes)
    assert '格式错误' in answer('{"1":1,"2":0,"3":0}')['question']
    second = answer('{"1":+2,"2":0,"3":0}')
    assert 'ACTION 1' not in second['question']
    assert all(f'ACTION {i}' in second['question'] for i in (2, 3))
    third = answer('{"2":-2,"3":0}')
    assert 'ACTION 1' not in third['question'] and 'ACTION 2' not in third['question']
    assert 'ACTION 3' in third['question']
    assert '格式错误' in answer('{"3":-3}')['question']
    assert answer('{"3":+1}')['complete']
    lane = state['event_dir'](notes, state['current_task'](notes)) / 'R'
    original = (lane / 'complete.json').read_bytes()
    assert json.loads(original)['values'] == {'1': {'R': 2}, '2': {'R': -2}, '3': {'R': 1}}
    assert answer('{"1":0,"2":0,"3":0}')['complete']
    assert (lane / 'complete.json').read_bytes() == original


@pytest.mark.parametrize('existing,physical', [(False, False), (True, False), (False, True), (True, True)])
def test_hw_routes_and_preserves_valid_signed_answers(event, existing, physical):
    state, notes = event
    answer = lambda text: state['answer']('HW', text, notes)
    assert '格式错误' in answer('{"existing_H_load":0,"physical_risk":false}')['question']
    routed = answer(json.dumps({'existing_H_load': existing, 'physical_risk': physical}))
    assert not routed['complete'] and 'event' not in routed
    text = routed['question']
    assert ('已有身心负荷时' in text) == existing
    assert ('承担身体风险的选项按 H 的负向档位判断' in text) == physical
    assert '没有本人的具体经济变化 0' in text
    values = {'1': {'H': -3, 'W': -2}, '2': {'H': 0, 'W': 1}, '3': {'H': 2, 'W': 3}}
    result = answer(json.dumps(values))
    if not existing and not physical:
        assert not result['complete']
        result = answer(json.dumps({key: True for key in values}))
    assert result['complete']
    lane = state['event_dir'](notes, state['current_task'](notes)) / 'HW'
    assert state['read'](lane / 'complete.json')['values'] == values
