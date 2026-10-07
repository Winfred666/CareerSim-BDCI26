"""The player sees one event, then current questions and choices until completion."""
import json
import re
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

import pytest

SKILL = Path(__file__).resolve().parents[1] / 'solution/skills/observe-decide-review'
ZERO = {str(i): 0 for i in (1, 2, 3)}
ANSWERS = {
    'O': [json.dumps({key: 1 for key in ZERO}), json.dumps(ZERO)],
    'N': [json.dumps({key: {'N': 0} for key in ZERO})],
    'S': [json.dumps({key: {'S': 0} for key in ZERO})],
    'HW': [json.dumps({'existing_H_load': False, 'physical_risk': False}),
           json.dumps({key: {'H': 0, 'W': 0} for key in ZERO}),
           json.dumps({key: False for key in ZERO})],
    'R': ['{"1":2,"2":0,"3":0}', '{"2":-2,"3":0}', '{"3":1}'],
}


@pytest.mark.parametrize('metric', ANSWERS)
def test_cli_repeats_current_question_on_invalid_answer_without_repeating_event(tmp_path, metric):
    root = tmp_path / 'skill'
    shutil.copytree(SKILL, root)
    state = runpy.run_path(str(root / 'scripts/translation-state.py'))
    notes = root / 'notebooks'
    state['save'](notes / 'workflow-state.json', {'session_id': 'test'})
    state['prepare'](notes, {
        'current_state': {'session_id': 'test'},
        'current_event': {'title': 'TITLE', 'description': 'PUBLIC STORY'},
        'event_history': {'steps': [{'description': 'PUBLIC HISTORY'}]},
        'choices': [{'choice': i, 'action': f'ACTION {i}', 'description': f'DETAIL {i}'}
                    for i in (1, 2, 3)],
    }, '00001')
    state['save'](notes / 'workflow-state.json', {'phase': 'analyse_running', 'session_id': 'test'})
    lane = state['event_dir'](notes, state['current_task']()) / metric

    def call(answer):
        return subprocess.run([sys.executable, str(root / 'scripts/translate.py'), metric, answer],
                              text=True, capture_output=True, check=True).stdout.strip()

    first = call('ready')
    assert '请重新调用脚本提交答案' not in first
    assert first.count('PUBLIC STORY') == first.count('PUBLIC HISTORY') == 1
    assert 'answer_schema' not in first and '"complete"' not in first and '"metric"' not in first
    pending, context, question = first.split('\n', 2)
    assert pending == 'complete=false'
    assert 'choices' not in json.loads(context)
    for i in (1, 2, 3):
        assert question.count(f'ACTION {i}') == question.count(f'DETAIL {i}') >= 1

    for answer in ANSWERS[metric]:
        template = question.split('\n答案格式（替换尖括号占位符）：', 1)[1].split('\n', 1)[0]
        assert not re.search(r':\s*(?:-?\d|true\b|false\b)', template)
        if metric == 'O' and template == '<编号>':
            assert answer.lstrip('-').isdigit()
        else:
            example = template.removeprefix('JSON ').replace('<整数>', '0').replace(
                '<布尔>', 'false').replace('<编号>', '0').replace('<情景原文>', '占位')

            def shape(value):
                return {key: shape(item) for key, item in value.items()} if isinstance(value, dict) else type(value)

            # Actual pending IDs and value types must match a valid answer
            # throughout routing and later questionnaire branches.
            assert shape(json.loads(example)) == shape(json.loads(answer))
        before = {p.name: p.read_bytes() for p in lane.iterdir() if p.is_file()}
        invalid = call('invalid')
        pending, correction, repeated_question = invalid.split('\n', 2)
        assert pending == 'complete=false'
        assert correction.startswith('答案格式错误，预期格式：')
        assert correction.endswith('，请重新调用脚本提交答案。')
        assert repeated_question.strip() == question.strip()
        if 'ACTION' not in question:
            assert 'ACTION' not in invalid and 'DETAIL' not in invalid
        assert before == {p.name: p.read_bytes() for p in lane.iterdir() if p.is_file()}
        result = call(answer)
        assert '请重新调用脚本提交答案' not in result
        assert 'PUBLIC STORY' not in result and 'PUBLIC HISTORY' not in result
        if result != 'complete=true':
            assert result.startswith('complete=false\n')
            assert 'ACTION' in result
            question = result.removeprefix('complete=false\n')
    assert result == 'complete=true'
    assert (lane / 'complete.json').exists()
    before = {p.name: p.read_bytes() for p in lane.iterdir() if p.is_file()}
    assert call('ready') == 'complete=true'
    assert before == {p.name: p.read_bytes() for p in lane.iterdir() if p.is_file()}
    assert not any((lane / name).exists() for name in ('.translation.json', '.joint.json', '.confirm.json'))
