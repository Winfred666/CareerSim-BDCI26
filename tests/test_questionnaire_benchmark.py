"""Regression gates for the parallel API transport and six-metric scorer."""
import json
from pathlib import Path
import runpy
import shutil

import pytest

from career_sim_runner.translation_benchmark.cases import TranslationCase
from career_sim_runner.translation_benchmark.questionnaires import question_cursor, scores, submission_tool, usage_summary

ROOT = Path(__file__).resolve().parents[1] / 'solution/skills/observe-decide-review'


def fixture(tmp_path):
    root = tmp_path / 'skill'
    shutil.copytree(ROOT,root)
    state = runpy.run_path(str(root/'scripts/translation-state.py'))
    notes = root/'notebooks'
    state['save'](notes/'workflow-state.json',{'session_id':'test'})
    state['prepare'](notes,{'current_state':{'session_id':'test'},
        'current_event':{'title':'公共题目','description':'公开内容'},
        'choices':[{'choice':1,'action':'甲'},{'choice':2,'action':'乙'}]},'00001')
    return state,notes


def test_six_metric_scores_count_zero_and_false_positive_and_ignore_d():
    c = TranslationCase('a','a',{}, {1:{'D':'+++','O':'+'},2:{}})
    p = {'a':{'1':{'O':1},'2':{'S':1}}}
    r = scores([c],p)
    assert r['metric_accuracy_pct'] == 100*11/12
    assert r['option_accuracy_pct'] == 50
    assert r['fp']==1 and r['fn']==0


def test_usage_counts_failed_calls_and_cohort_retries_without_inventing_reasoning():
    a=[{'cursor':'x','attempt':0,'usage':None,'error':'transport'},
       {'cursor':'x','attempt':1,'usage':{'input_tokens':4,'output_tokens':1}},
       {'cursor':'x','attempt':0,'usage':{'input_tokens':4,'output_tokens':1}}]
    r=usage_summary(a)
    assert r['retries']==2 and r['automatic_retries']==1 and r['targeted_retries']==1
    assert r['unavailable_usage_calls']==1 and r['unavailable_reasoning_calls']==3


def test_invalid_flat_answer_does_not_advance_or_partly_commit(tmp_path):
    state,notes=fixture(tmp_path)
    first=state['answer']('S','ready',notes)
    failed=state['answer']('S','{"1":{"S":1}}',notes)
    assert not failed['complete'] and failed['question'].startswith('答案格式错误')
    lane=state['event_dir'](notes,state['current_task'](notes))/'S'
    assert not (lane/'.answers.json').exists()
    assert '2. 乙' in first['question']
    assert state['answer']('S','{"1":{"S":1},"2":{"S":0}}',notes)['complete']


def test_benchmark_replay_cursor_tracks_progress_without_a_runtime_cursor_file(tmp_path):
    state, notes = fixture(tmp_path)
    state['answer']('N', 'ready', notes)
    before = question_cursor(state, notes, 'N')
    state['answer']('N', 'invalid', notes)
    assert question_cursor(state, notes, 'N') == before
    assert not state['answer']('N', '{"joint":false}', notes)['complete']
    assert question_cursor(state, notes, 'N') == before
    state['answer']('N', '{"1":{"N":0},"2":{"N":1}}', notes)
    assert question_cursor(state, notes, 'N') is None
    assert not list(notes.rglob('cursor.json'))


def test_submit_schema_constrains_o_numbers_and_only_current_r_options(tmp_path):
    state, notes = fixture(tmp_path)
    schema = lambda g: submission_tool(state, notes, g)['function']['parameters']['properties']['answer']
    state['answer']('O', 'ready', notes)
    assert schema('O')['required'] == ['1', '2']
    assert schema('O')['properties']['1'] == {'type': 'integer', 'enum': [1, 2]}
    state['answer']('O', '1', notes)
    assert schema('O')['enum'] == ['0', '1', '2']
    state['answer']('O', '2', notes)
    assert schema('O')['enum'] == ['1', '2', '3']
    state['answer']('R', 'ready', notes)
    assert schema('R')['required'] == ['1', '2']
    assert schema('R')['properties']['1']['enum'] == [0, 2, 3]
    state['answer']('R', '{"1":2,"2":0}', notes)
    assert schema('R')['required'] == ['2']
    assert schema('R')['properties']['2']['enum'] == [-2, -1, 0]


def test_n_b16_keeps_atomic_all_option_answers_and_schema(tmp_path):
    state, notes = fixture(tmp_path)
    answer = lambda value: state['answer']('N', json.dumps(value), notes)
    packet = state['answer']('N', 'ready', notes)
    lane = state['event_dir'](notes, state['current_task'](notes)) / 'N'
    schema = lambda: submission_tool(state, notes, 'N')['function']['parameters']['properties']['answer']
    assert schema()['required'] == ['1', '2']
    assert schema()['properties']['1']['properties']['N']['enum'] == list(range(-3, 4))
    assert '1. 甲' in packet['question'] and '2. 乙' in packet['question']
    for invalid in ({'joint': 0}, {'joint': False, 'extra': 1}, {'1': {'N': 0}}):
        assert answer(invalid)['question'].startswith('答案格式错误')
        assert not (lane / '.answers.json').exists()
    for invalid in (
        {'1': {'N': 0}}, {'1': {'N': -4}, '2': {'N': 0}},
        {'1': {'N': True}, '2': {'N': 0}}, {'1': {}, '2': {'N': 0}},
        {'1': {'N': 0, 'reason': '旧字段'}, '2': {'N': 0}}, {'1': -1, '2': 2},
    ):
        assert answer(invalid)['question'].startswith('答案格式错误')
        assert not (lane / '.answers.json').exists()
        assert not (lane / 'complete.json').exists()
    assert answer({'1': {'N': -3}, '2': {'N': 3}})['complete']
    assert state['read'](lane / 'complete.json')['values'] == {'1': {'N': -3}, '2': {'N': 3}}
    saved = (lane / 'complete.json').read_bytes()
    assert state['answer']('N', 'ready', notes)['complete']
    assert (lane / 'complete.json').read_bytes() == saved


@pytest.mark.parametrize('score', range(-3, 4))
def test_n_b16_preserves_each_signed_integer_in_one_answer(tmp_path, score):
    state, notes = fixture(tmp_path)
    answer = lambda value: state['answer']('N', json.dumps(value), notes)
    state['answer']('N', 'ready', notes)
    assert answer({str(i): {'N': score} for i in (1, 2)})['complete']
    lane = state['event_dir'](notes, state['current_task'](notes)) / 'N'
    assert state['read'](lane / 'complete.json')['values'] == {str(i): {'N': score} for i in (1, 2)}


def test_o_v23_negative_branch_keeps_magnitude_and_skips_positive_scale(tmp_path):
    state, notes = fixture(tmp_path)
    answer = lambda value: state['answer']('O', value, notes)
    assert not answer('ready')['complete']
    assert not answer('1')['complete']
    assert not answer('2')['complete']
    assert not answer('2')['complete']
    assert not answer('2')['complete']
    assert answer('0')['complete']
    lane = state['event_dir'](notes, state['current_task'](notes)) / 'O'
    assert state['read'](lane / 'complete.json')['values'] == {'1': {'O': -2}, '2': {'O': 0}}


@pytest.mark.parametrize('kind,direction,magnitude,expected', [
    (1, 0, None, 0), (2, 0, None, 0),
    *[(1, 1, i, i) for i in (0, 1, 2, 3)],
    *[(2, 2, i, i) for i in (0, 1, 2, 3)],
    *[(1, 2, i, -i) for i in (1, 2, 3)],
    *[(2, 1, i, -i) for i in (1, 2, 3)],
])
def test_v23_business_and_other_routes_preserve_signed_scale(tmp_path, kind, direction, magnitude, expected):
    state, notes = fixture(tmp_path)
    answer = lambda value: state['answer']('O', value, notes)
    answer('ready')
    assert not answer(json.dumps({'1': kind, '2': kind}))['complete']
    result = answer(json.dumps({'1': direction, '2': direction}))
    if magnitude is not None:
        assert not result['complete']
        result = answer(json.dumps({'1': magnitude, '2': magnitude}))
    assert result['complete']
    lane = state['event_dir'](notes, state['current_task'](notes)) / 'O'
    assert state['read'](lane / 'complete.json')['values'] == {str(i): {'O': expected} for i in (1, 2)}
