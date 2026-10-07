#!/usr/bin/env python3
"""Run one metric's next question against the event loaded by observe."""
import json
from pathlib import Path
import runpy
import hashlib
import shlex
import sys

state = runpy.run_path(str(Path(__file__).with_name('translation-state.py')))


def render(result):
    notice = '问答题修改成功，请开始当前事件答题。\n' if result.get('questionnaire_updated') else ''
    if result.get('questionnaire_skipped'):
        notice = '已跳过问答题修改，请开始当前事件答题。\n'
    if result['complete']:
        return notice + 'complete=true'
    event = result.get('event')
    context = json.dumps(event, ensure_ascii=False) + '\n' if event else ''
    question = result['question']
    canonical = question.split('\n', 1)[-1] if question.startswith('答案格式错误，') else question
    marker = hashlib.sha256(canonical.encode()).hexdigest()[:12]
    command = 'python3 ' + shlex.quote(str(Path(__file__).resolve())) + ' ' + result['metric']
    if result.get('kind') == 'questionnaire_edit':
        arguments = 'edit ' + ' '.join(shlex.quote(arg) for arg in result['edit_arguments'])
        template = '\n修题只替换下面命令的短句参数，原短句为空表示追加；不拼JSON、不检索题键。'
        skip = '\n不修改时：' + command + ' skip --call=' + marker
    else:
        arguments = "'<答案JSON或编号>'"
        template = '\n答案格式（替换尖括号占位符）：' + result['answer_template']
        if result.get('answer_choices') is not None:
            template = ('\n答题编号不是O增减值，只能填' + '、'.join(map(str, result['answer_choices']))
                        + '；一次提交本题全部选项。' + template)
        skip = ''
    return notice + 'complete=false\n' + context + question + template + '\n提交命令：' + command + ' ' + arguments + ' --call=' + marker + skip


def submission(arguments):
    arguments = list(arguments)
    if arguments and arguments[-1].startswith('--call='):
        arguments.pop()
    if len(arguments) >= 2 and arguments[1] == 'edit':
        if len(arguments) not in (4, 5):
            raise ValueError('edit requires [question] old new; use an empty string to append')
        metric = arguments[0]
        if metric == 'R':
            raise ValueError('R is hidden; questionnaire edits are disabled')
        patch = {'old': arguments[-2], 'new': arguments[-1]}
        if len(arguments) == 5:
            patch['question'] = arguments[2]
        return metric, json.dumps(patch, ensure_ascii=False)
    if len(arguments) != 2:
        raise ValueError('usage: translate.py <metric> <ready|answer|edit [question] old new> [--call=marker]')
    return arguments[0], arguments[1]


if __name__ == '__main__':
    try:
        metric, answer = submission(sys.argv[1:])
        phase = state['read'](state['NOTEBOOKS'] / 'workflow-state.json')
        if phase.get('phase') != 'analyse_running':
            raise ValueError('analysis is only available during the analysis stage')
        result = state['answer'](metric, answer)
        print(render(result))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'analysis_error: {exc}', file=sys.stderr)
        sys.exit(2)
