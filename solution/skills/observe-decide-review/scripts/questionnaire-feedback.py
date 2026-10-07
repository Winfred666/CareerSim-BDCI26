"""Retain public direction mistakes and one-shot questionnaire edit feedback."""
import json
from pathlib import Path
import runpy
import shlex


def direction_errors(predicted, actual):
    return [metric for metric, value in predicted.items()
            if (value > 0) - (value < 0) != (actual[metric] > 0) - (actual[metric] < 0)]


def with_event(feedback, public):
    selected = next(c for c in public['choices'] if c['choice'] == feedback['choice'])
    return {**feedback, 'event': {k: v for k, v in public.items() if k != 'choices'},
            'action': selected['action'],
            **({'description': selected['description']} if 'description' in selected else {})}


def record_answers(api, group, lane, event, engine, before, text):
    """Retain the executed question path before engines discard their routing state."""
    if group == 'R':
        return
    value = json.loads(text)
    if group == 'O':
        choices, key = engine['pending'](event, before)
        answers = value if isinstance(value, dict) else {str(choices[0]['choice']): value}
    elif group == 'HW':
        if '_route' not in before:
            key = 'route'
            answers = {str(c['choice']): value for c in event['choices']}
        elif '_joint' not in before:
            route = before['_route']
            key = ('physical_' if route['physical_risk'] else '') + (
                'existing' if route['existing_H_load'] else 'ordinary')
            answers = value
        else:
            key = 'confirm_history' if event.get('event_history') else 'confirm'
            answers = value
    else:
        key, answers = 'question', value
    path = lane / '.answer-trace.json'
    trace = api['read'](path) if path.exists() else {}
    question = api['read'](api['ROOT'] / 'scripts/questionnaires' / f'{group}.json')['questions'][key]['text']
    for choice, answer in answers.items():
        trace.setdefault(choice, []).append({'question': key, 'text': question, 'answer': answer})
    api['save'](path, trace)


def history_path(api, notebooks, task, group):
    return api['event_dir'](notebooks, task).parent / f'{group}-direction-errors.md'


def append_history(api, notebooks, task, group, feedback):
    """Called under .feedback.lock; repeated review cannot duplicate a case."""
    if group == 'R':
        return
    if group == 'HW':
        for metric in feedback['predicted']:
            append_history(api, notebooks, task, metric, {
                **feedback, 'predicted': {metric: feedback['predicted'][metric]},
                'actual': {metric: feedback['actual'][metric]}})
        return
    path = history_path(api, notebooks, task, group)
    previous = path.read_text(encoding='utf-8') if path.exists() else f'# {group} 方向误判历史\n'
    marker = f'## {feedback["event_id"]} | '
    if any(line.startswith(marker) for line in previous.splitlines()):
        return
    event = feedback.get('event', {})
    current = event.get('current_event', {})
    title = current.get('title', feedback.get('title', '未记录标题')).replace('\n', ' ')
    lines = ['', marker + title,
             '预测：' + json.dumps(feedback['predicted'], ensure_ascii=False)
             + '；真实：' + json.dumps(feedback['actual'], ensure_ascii=False)]
    for index, step in enumerate(event.get('event_history', {}).get('steps', []), 1):
        lines += [f'前情{index}：' + step.get('description', ''),
                  '当时选择：' + step.get('selected_choice', {}).get('action', '')]
    lines += ['当前情境：' + current.get('description', ''),
              f'所选 #{feedback["choice"]}：' + feedback.get('action', '')]
    if feedback.get('description'):
        lines.append('选项说明：' + feedback['description'])
    lines.append('答题轨迹：')
    for index, step in enumerate(feedback.get('answer_path', []), 1):
        lines += [f'{index}. {step["question"]}：' + step.get('text', '旧记录未保存原题文案'),
                  '   回答：' + json.dumps(step['answer'], ensure_ascii=False)]
    if not feedback.get('answer_path'):
        lines.append('旧记录未保存逐题轨迹；已存答案：' + json.dumps(
            feedback.get('prior_answers', {}), ensure_ascii=False))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(previous + '\n'.join(lines) + '\n', encoding='utf-8')
    temporary.replace(path)


def restore_questions(api, notebooks, session):
    path = notebooks / 'questionnaire-overrides.json'
    if path.exists():
        saved = api['read'](path)
        if saved.get('session_id') == session:
            for group, document in saved.get('questionnaires', {}).items():
                if group not in api['GROUPS']:
                    raise ValueError('invalid saved questionnaire group')
                if group == 'R':
                    continue
                api['save'](api['ROOT'] / 'scripts/questionnaires' / f'{group}.json', document)


def cache_review(notebooks, changes, choice):
    if not (notebooks / 'translation-context.json').exists():
        return  # No stored prediction can be compared for legacy review notebooks.
    api = runpy.run_path(str(Path(__file__).with_name('translation-state.py')))
    task = api['current_task'](notebooks)
    selected = next(c for c in task['choices'] if c['choice'] == choice)
    if isinstance(selected.get('status_updates'), dict):
        return  # Official quarter actions must neither teach nor erase pending feedback.
    directory = api['event_dir'](notebooks, task)
    public = api['read'](directory / 'public-event.json')
    path = notebooks / 'questionnaire-feedback.json'
    with api['locked'](notebooks / '.feedback.lock'):
        saved = api['read'](path) if path.exists() else {}
        pending = saved.get('pending', {}) if saved.get('session_id') == task['session_id'] else {}
        pending.pop('R', None)
        for group in ('O', 'N', 'S', 'HW'):  # R is hidden; public review supplies no true R delta.
            receipt = api['read'](directory / group / 'complete.json')
            if (receipt['observation_hash'] != task['observation_hash']
                    or receipt['script_hash'] != task['scripts'][group]):
                raise ValueError('feedback prediction does not match reviewed event')
            predicted = receipt['values'][str(choice)]
            actual = {metric: changes.get(metric, 0) for metric in group}
            errors = direction_errors(predicted, actual)
            if errors:
                pending[group] = with_event({
                    'event_id': task['event_id'], 'choice': choice,
                    'predicted': {m: predicted[m] for m in errors},
                    'actual': {m: actual[m] for m in errors}}, public)
                if group == 'O':
                    pending[group]['prior_answers'] = api['read'](
                        directory / group / '.answers.json')[str(choice)]
                elif group == 'N':
                    answers = api['read'](directory / group / '.answers.json')
                    pending[group]['prior_answers'] = answers[str(choice)]
                    if '_joint' in answers:
                        pending[group]['prior_answers'] = {
                            'joint': answers['_joint'], 'face': answers['_face'][str(choice)],
                            **answers[str(choice)]}
                trace_path = directory / group / '.answer-trace.json'
                if trace_path.exists():
                    pending[group]['answer_path'] = api['read'](trace_path).get(str(choice), [])
                append_history(api, notebooks, task, group, pending[group])
        api['save'](path, {'session_id': task['session_id'], 'pending': pending})


def editing_question(group, request, error=''):
    feedback = request['feedback']
    title = feedback.get('event', {}).get('current_event', {}).get('title', feedback.get('title', '未记录标题'))
    single = len(request['allowed_keys']) == 1
    key = '<题键：' + '/'.join(request['allowed_keys']) + '>'
    return {'metric': group, 'complete': False, 'kind': 'questionnaire_edit',
            'question': error + '问答题最简修改：最新「' + title + '」预测'
            + json.dumps(feedback['predicted'], ensure_ascii=False) + '；真实'
            + json.dumps(feedback['actual'], ensure_ascii=False) + '。\n'
            '从尾部读取本指标历史，总结典型错误：tail -n 120 '
            + ' '.join(shlex.quote(path) for path in request['history_paths']) + '\n'
            '做最简修复，可合并或增强语义，或补充有效边界，保留已有有效边界，'
            '不可用过于冗杂的表述拟合情境。只改单题文案，不改取值、流程或算式；'
            '未发现典型错误可提交 skip。提交后直接答当前题。',
            'edit_arguments': ([] if single else [key]) + ['<原短句，空串表示追加>', '<新短句>'],
            'answer_template': 'JSON {' + ('' if single else '"question":' + json.dumps(key, ensure_ascii=False) + ',')
            + '"old":"<原短句，空串表示追加>","new":"<新短句>"}'}


def take_feedback(api, group, notebooks, task, lane):
    request_path = lane / '.edit-request.json'
    request_path.unlink(missing_ok=True)
    if group == 'R':
        return None  # Hidden risk has no public ground truth for questionnaire edits.
    if all(isinstance(c.get('status_updates'), dict) for c in task['choices']):
        return None
    path = notebooks / 'questionnaire-feedback.json'
    if not path.exists():
        return None
    with api['locked'](notebooks / '.feedback.lock'):
        saved = api['read'](path)
        if saved.get('session_id') != task['session_id']:
            return None
        feedback = saved.get('pending', {}).get(group)
        if not feedback or feedback['event_id'] >= task['event_id']:
            return None
        saved['pending'].pop(group)
        api['save'](path, saved)  # Consumed on first ready even if the caller skips the edit.
    errors = direction_errors(feedback['predicted'], feedback['actual'])
    if not errors:
        return None  # Ignore magnitude-only feedback saved by an older script.
    feedback = {**feedback, 'predicted': {m: feedback['predicted'][m] for m in errors},
                'actual': {m: feedback['actual'][m] for m in errors}}
    if 'event' not in feedback:
        previous = {**task, 'event_id': feedback['event_id']}
        public_path = api['event_dir'](notebooks, previous) / 'public-event.json'
        if public_path.exists():
            feedback = with_event(feedback, api['read'](public_path))
    with api['locked'](notebooks / '.feedback.lock'):
        append_history(api, notebooks, task, group, feedback)
    questionnaire = api['read'](api['ROOT'] / 'scripts/questionnaires' / f'{group}.json')
    keys = [step['question'] for step in feedback.get('answer_path', [])]
    if not keys and group == 'O':
        keys = [key for key in feedback.get('prior_answers', {}) if key != 'O']
    if not keys and group in ('N', 'S'):
        keys = ['question']
    # Legacy HW feedback has no routing trace: do not invent one.
    allowed = list(dict.fromkeys(key for key in keys if key in questionnaire['questions']))
    histories = [str(history_path(api, notebooks, task, metric).resolve()) for metric in group
                 if history_path(api, notebooks, task, metric).exists()]
    request = {'feedback': feedback, 'questionnaire': questionnaire,
               'allowed_keys': allowed or list(questionnaire['questions']),
               'history_path': histories[0], 'history_paths': histories}
    api['save'](request_path, request)
    return editing_question(group, request)


def apply_edit(api, group, text, notebooks, task, lane):
    if group == 'R':
        raise ValueError('R is hidden; questionnaire edits are disabled')
    request_path = lane / '.edit-request.json'
    request = api['read'](request_path)
    if text == 'skip':
        request_path.unlink()
        return {'complete': False, 'kind': 'questionnaire_skipped', 'metric': group}
    try:
        patch = json.loads(text)
        if type(patch) is dict and len(request['allowed_keys']) == 1:
            patch['question'] = request['allowed_keys'][0]
        if (type(patch) is not dict or set(patch) not in ({'question', 'text'}, {'question', 'old', 'new'})
                or any(type(v) is not str for v in patch.values())):
            raise ValueError('按当前模板提交字符串字段')
        document = request['questionnaire']
        key = patch['question']
        if key not in document['questions']:
            raise ValueError('题键只能使用：' + '、'.join(request['allowed_keys']))
        if key not in request['allowed_keys']:
            raise ValueError('只修改实际答题路径涉及的题')
        old = document['questions'][key]['text']
        if 'text' not in patch:
            fragment, replacement = patch['old'], patch['new']
            if max(len(fragment), len(replacement)) > 160 or not (fragment or replacement):
                raise ValueError('只修改一处最多160字的短句')
            if fragment and old.count(fragment) != 1:
                raise ValueError('原短句须在该题中恰好出现一次')
            patch['text'] = old.replace(fragment, replacement, 1) if fragment else old + '\n' + replacement
        if not patch['text'].strip() or len(patch['text']) > len(old) + 160:
            raise ValueError('修订须非空，最多增加160字，只作最简修改')
        with api['locked'](api['event_dir'](notebooks, task) / '.questionnaire.lock'):
            path = api['ROOT'] / 'scripts/questionnaires' / f'{group}.json'
            if api['read'](path) != document:
                raise ValueError('问卷已变化，请重新 ready')
            document['questions'][key]['text'] = patch['text']
            api['save'](path, document)
            with api['locked'](notebooks / '.feedback.lock'):
                overrides_path = notebooks / 'questionnaire-overrides.json'
                overrides = api['read'](overrides_path) if overrides_path.exists() else {}
                if overrides.get('session_id') != task['session_id']:
                    overrides = {'session_id': task['session_id'], 'questionnaires': {}}
                overrides['questionnaires'][group] = document
                api['save'](overrides_path, overrides)
            fresh = api['current_task'](notebooks, task['event_id'])
            fresh['scripts'][group] = api['source_hash'](group)
            api['save'](api['event_dir'](notebooks, fresh) / 'task.json', fresh)
            api['save'](notebooks / 'translation-context.json', fresh)
            if group != 'O':
                policy = runpy.run_path(str(api['ROOT'] / 'scripts/questionnaires' / f'{group}.py'))['policy']()
                api['save'](lane / '.policy.json', policy)
            api['save'](lane / 'questionnaire-edit.json', {
                'feedback': request['feedback'], 'question': key, 'before': old, 'after': patch['text']})
        request_path.unlink()
        return {'complete': False, 'kind': 'questionnaire_updated', 'metric': group}
    except (ValueError, KeyError, TypeError) as error:
        return editing_question(group, request, '答案格式错误，' + str(error) + '。\n')
