"""Event-bound questionnaire storage and the silent workflow's completion gates."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import re
import runpy
import tempfile
import os
import unicodedata

ROOT = Path(__file__).resolve().parent.parent
NOTEBOOKS = ROOT / 'notebooks'
GROUPS = ('O', 'N', 'S', 'HW', 'R')
METRICS = ('O', 'N', 'S', 'H', 'W', 'R')
FIELDS = {'O': 'output', 'N': 'network', 'S': 'skill', 'H': 'health', 'W': 'wealth', 'R': 'hiddenrisk'}

def source_hash(group):
    paths = [ROOT / 'scripts/questionnaires' / (group + ext)
             for ext in ('.py', '.json')]
    paths.append(ROOT / 'stages' / f'translate_{group}.md')
    paths.append(ROOT / 'scripts/questionnaire-feedback.py')
    if group == 'R':
        paths.append(ROOT / 'scripts/questionnaires/R-integer-json.py')
    return digest({p.name: p.read_text() for p in paths})


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def bound_session(notebooks=NOTEBOOKS, *, required=True):
    path = notebooks / 'workflow-state.json'
    session = read(path).get('session_id') if path.exists() else None
    if session is None and not required:
        return None
    if not isinstance(session, str) or not session.strip():
        raise ValueError('workflow-state.json: session_id is not initialized')
    return session


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
    if path.exists() and path.read_text(encoding='utf-8') == body:
        return
    fd, name = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(body)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def event_dir(notebooks, task):
    return notebooks / 'translations' / hashlib.sha256(task['session_id'].encode()).hexdigest()[:16] / task['event_id']


def current_task(notebooks=NOTEBOOKS, event_id=None):
    task = read(notebooks / 'translation-context.json')
    if event_id is not None and event_id != task['event_id']:
        raise ValueError('stale event_id')
    if bound_session(notebooks) != task['session_id']:
        raise ValueError('translation session mismatch')
    if not re.fullmatch(r'[0-9]{5}', task['event_id']):
        raise ValueError('invalid translation event_id')
    return task


def history_key(event, choices):
    """Use the full public title, including narratives without a long-chain suffix."""
    title = event.get('title', '')
    if not title or not event.get('description') or not any(
            not isinstance(c.get('status_updates'), dict) for c in choices):
        return None
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', title))


def history_step(event, choice=None, month=None):
    """Keep narrative facts and the executed action, without menu or metric metadata."""
    step = {'description': event.get('description', '')}
    if type(month) is int:
        step['current_month'] = month
    if choice is not None:
        step['selected_choice'] = {k: choice[k] for k in ('action', 'description') if k in choice}
    return step


def public_history(history):
    return [history_step(step, step.get('selected_choice'), step.get('current_month'))
            for step in (history or {}).get('steps', [])]


def load_histories(notebooks, session):
    """Restore old confirmed actions once when upgrading a game without a cache."""
    path = notebooks / 'event-history.json'
    cache = read(path) if path.exists() else {}
    if session not in cache:
        chains = cache[session] = {}
        archive = notebooks / 'translations' / hashlib.sha256(session.encode()).hexdigest()[:16]
        for directory in sorted(archive.glob('*')):
            if not all((directory / name).exists() for name in ('task.json', 'public-event.json', 'action.json')):
                continue
            task = read(directory / 'task.json')
            if task['session_id'] != session:
                continue
            public = read(directory / 'public-event.json')
            key = history_key(public['current_event'], task['choices'])
            if key is None:
                continue
            chosen = read(directory / 'action.json')['choice']
            choice = next(c for c in public['choices'] if c['choice'] == chosen)
            steps = chains[key]['steps'] if key in chains else public_history(public.get('event_history'))
            chains[key] = {'event_id': task['event_id'], 'steps': [*steps,
                history_step(public['current_event'], choice, public.get('current_month'))]}
        save(path, cache)
    return cache


def prior_history(notebooks, session, event_id, public, choices):
    cache = load_histories(notebooks, session)
    key = history_key(public['current_event'], choices)
    chain = cache[session].get(key)
    if chain:
        # Re-loading the just-executed event must not include its own action as prior history.
        return chain['steps'][:-1] if chain['event_id'] == event_id else chain['steps']
    return public_history(public.get('event_history'))


def remember_action(notebooks, task, choice):
    public = read(event_dir(notebooks, task) / 'public-event.json')
    key = history_key(public['current_event'], task['choices'])
    if key is None:
        return
    selected = next(c for c in public['choices'] if c['choice'] == choice)
    cache = load_histories(notebooks, task['session_id'])
    cache[task['session_id']][key] = {'event_id': task['event_id'], 'steps': [
        *public_history(public.get('event_history')),
        history_step(public['current_event'], selected, public.get('current_month'))]}
    save(notebooks / 'event-history.json', cache)


def prepare(notebooks, observation, event_id):
    current = observation['current_state']
    session = current['session_id']
    if bound_session(notebooks) != session:
        raise ValueError('translation session mismatch')
    runpy.run_path(str(ROOT / 'scripts/questionnaire-feedback.py'))['restore_questions'](
        globals(), notebooks, session)
    choices = observation.get('choices') or []
    ids = [c['choice'] for c in choices]
    if any(type(i) is not int for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('invalid or duplicate choice id')
    public = {**({'event_history': observation['event_history']} if observation.get('event_history') else {}),
              'current_event': {k: v for k, v in (observation.get('current_event') or {}).items() if k in ('title', 'description')},
              'choices': [{'choice': c['choice'], 'action': c.get('action', ''),
                           **({'description': c['description']} if 'description' in c else {})} for c in choices]}
    month = (current.get('time') or {}).get('current_month')
    if type(month) is int:
        public['current_month'] = month
    task = {'event_id': event_id, 'session_id': session,
            'observation_hash': digest(observation), 'choices': choices,
            'scripts': {m: source_hash(m) for m in GROUPS}}
    directory = event_dir(notebooks, task)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / 'task.json'
    if manifest.exists() and read(manifest) != task:
        raise ValueError('event_id already loaded with different observation or questionnaire')
    public_path = directory / 'public-event.json'
    if public_path.exists():
        public = read(public_path)
    else:
        steps = prior_history(notebooks, session, event_id, public, choices)
        public.pop('event_history', None)
        if steps:
            public['event_history'] = {'steps': steps}
    save(manifest, task)
    save(public_path, public)
    for metric in GROUPS:
        lane = directory / metric
        lane.mkdir(exist_ok=True)
        # Only public choices without official deltas require interpretation.
        pending = [c for c in public['choices'] if not isinstance(
            next(v for v in choices if v['choice'] == c['choice']).get('status_updates'), dict)]
        save(lane / '.event.json', {**public, 'choices': pending})
        if not pending:
            save(lane / 'complete.json', {'observation_hash': task['observation_hash'],
                                        'script_hash': task['scripts'][metric], 'values': {}})
        elif metric != 'O':
            config = runpy.run_path(str(ROOT / 'scripts/questionnaires' / f'{metric}.py'))['policy']()
            save(lane / '.policy.json', config)
    save(notebooks / 'translation-context.json', task)


def official_delta(choice, metric):
    updates = choice['status_updates']
    values = [v for key, v in updates.items() if key.lower() in (metric.lower(), FIELDS[metric])]
    if len(values) > 1 or any(type(v) is not int for v in values):
        raise ValueError('invalid official metric delta')
    return values[0] if values else 0


def expected_answer(metric, event, state, engine, lane, *, template_only=False):
    """Describe the current schema without suggesting an answer value."""
    if metric == 'O':
        choices, key = engine['pending'](event, state)
        if len(choices) > 1:
            item = '<编号>' if template_only else '编号'
            fields = ','.join(json.dumps(str(c['choice'])) + ':' + item for c in choices)
            return 'JSON {' + fields + '}' + ('' if template_only else '；编号须为' + '、'.join(map(str, sorted(engine['ALLOWED'][key]))))
        if template_only:
            return '<编号>'
        _, key = engine['current'](event, state)
        return '只填一个编号：' + '、'.join(str(v) for v in sorted(engine['ALLOWED'][key]))
    if metric == 'N' and 'current' in engine:
        phase = engine['current'](state)
        if phase == 'route':
            return 'JSON {"joint":<布尔>}' if template_only else 'JSON {"joint":true或false}'
        if phase == 'participation' and not template_only:
            fields = ','.join(json.dumps(str(c['choice'])) + ':{"N":整数}' for c in event['choices'])
            return 'JSON {' + fields + '}；整数须为-1或0'
    if metric == 'HW' and '_route' not in state:
        if template_only:
            return 'JSON {"existing_H_load":<布尔>,"physical_risk":<布尔>}'
        return 'JSON {"existing_H_load":true或false,"physical_risk":true或false}'

    choices = event['choices']
    if metric == 'HW' and '_joint' in state:
        item, bounds = 'true或false', ''
    elif metric == 'R':
        phase, choices = engine['current'](event, state, read(lane / '.policy.json'))
        allowed = read(lane / '.policy.json')['allowed'][str(phase)]
        item, bounds = '整数', '；整数须为' + '、'.join(map(str, allowed)) + '之一'
    else:
        item = '{' + ','.join(json.dumps(m) + ':整数' for m in metric) + '}'
        lower, upper = -3, 3
        bounds = f'；整数范围 {lower} 至 {upper}'
    if template_only:
        item = item.replace('整数', '<整数>').replace('true或false', '<布尔>')
        bounds = ''
    fields = ','.join(json.dumps(str(c['choice'])) + ':' + item for c in choices)
    return 'JSON {' + fields + '}' + bounds


def answer(metric, text, notebooks=NOTEBOOKS, event_id=None):
    if metric not in GROUPS:
        raise ValueError('unknown analysis metric')
    task = current_task(notebooks, event_id)
    directory = event_dir(notebooks, task)
    lane = directory / metric
    with locked(lane / '.lock'):
        task = current_task(notebooks, task['event_id'])
        module = ROOT / 'scripts/questionnaires' / f'{metric}.py'
        if source_hash(metric) != task['scripts'][metric]:
            raise ValueError('questionnaire changed during event; refresh required')
        state_path = lane / '.answers.json'
        questionnaire_updated = False
        questionnaire_skipped = False
        if text == 'ready':
            feedback = runpy.run_path(str(ROOT / 'scripts/questionnaire-feedback.py'))
            request_path = lane / '.edit-request.json'
            if metric != 'R' and request_path.exists():
                return feedback['editing_question'](metric, read(request_path))
            if not state_path.exists():
                result = feedback['take_feedback'](globals(), metric, notebooks, task, lane)
                if result is not None:
                    return result
        elif metric != 'R' and text is not None and (lane / '.edit-request.json').exists():
            feedback = runpy.run_path(str(ROOT / 'scripts/questionnaire-feedback.py'))
            result = feedback['apply_edit'](globals(), metric, text, notebooks, task, lane)
            if result.get('kind') not in ('questionnaire_updated', 'questionnaire_skipped'):
                return result
            task = current_task(notebooks, task['event_id'])
            state_path.unlink(missing_ok=True)
            (lane / 'complete.json').unlink(missing_ok=True)
            questionnaire_updated = result['kind'] == 'questionnaire_updated'
            questionnaire_skipped = result['kind'] == 'questionnaire_skipped'
            text = 'ready'
        engine = runpy.run_path(str(module))
        event = read(lane / '.event.json')
        before = read(state_path) if state_path.exists() else {}
        # A completed official menu needs no model answer.
        if not event['choices']:
            question = '无问题，事件翻译结束。'
        else:
            question = engine['main']('ready' if text is None else text, lane)
        state = read(state_path) if state_path.exists() else {}
        if text not in (None, 'ready') and before != state and not question.startswith('答案格式错误'):
            feedback = runpy.run_path(str(ROOT / 'scripts/questionnaire-feedback.py'))
            feedback['record_answers'](globals(), metric, lane, event, engine, before, text)
        if question.startswith('答案格式错误'):
            expected = expected_answer(metric, event, state, engine, lane)
            current_question = question.split('\n', 1)[1]
            question = ('答案格式错误，预期格式：' + expected
                        + '，请重新调用脚本提交答案。\n' + current_question)
        completed = all(all(m in state.get(str(c['choice']), {}) for m in metric) for c in event['choices'])
        if completed:
            save(lane / 'complete.json', {
                'observation_hash': task['observation_hash'], 'script_hash': task['scripts'][metric],
                'values': {str(c['choice']): {m: state[str(c['choice'])][m] for m in metric} for c in event['choices']}})
        result = {'metric': metric, 'complete': completed, 'question': question}
        if questionnaire_updated:
            result['questionnaire_updated'] = True
        elif questionnaire_skipped:
            result['questionnaire_skipped'] = True
        if not completed:
            result['answer_template'] = expected_answer(
                metric, event, state, engine, lane, template_only=True)
            if metric == 'O':
                _, key = engine['pending'](event, state)
                result['answer_choices'] = sorted(engine['ALLOWED'][key])
        if not completed and text in (None, 'ready'):
            public = read(directory / 'public-event.json')
            # Engines render only the choices needed by the current question.
            result['event'] = {k: v for k, v in public.items() if k != 'choices'}
        return result


def question(metric, notebooks=NOTEBOOKS, event_id=None):
    """Inspect a saved question for offline benchmark resume without restarting it."""
    return answer(metric, None, notebooks, event_id)


def option_metrics(notebooks=NOTEBOOKS, event_id=None):
    task = current_task(notebooks, event_id)
    review = read(notebooks / 'review-context.json')
    if review.get('event_key') != task['event_id'] or review.get('current_state', {}).get('session_id') != task['session_id']:
        raise ValueError('energy state does not match current event')
    energy = review['current_state']['status']['energy']
    if type(energy) is not int or energy < 0:
        raise ValueError('invalid remaining energy')
    harmless_action = any(
        choice.get('action_id') != 'no_action'
        and type(choice.get('energy_cost')) is int and choice['energy_cost'] <= energy
        and isinstance(choice.get('status_updates'), dict)
        and all(type(delta) is int and (delta <= 0 if key.lower() in ('hiddenrisk', 'r') else delta >= 0)
                for key, delta in choice['status_updates'].items())
        for choice in task['choices'])
    directory = event_dir(notebooks, task)
    receipts = {}
    pending_ids = {str(c['choice']) for c in task['choices'] if not isinstance(c.get('status_updates'), dict)}
    for metric in GROUPS:
        path = directory / metric / 'complete.json'
        if not path.exists():
            raise ValueError(f'analysis incomplete: {metric}')
        receipt = read(path)
        actual_hash = source_hash(metric)
        if (receipt['observation_hash'] != task['observation_hash']
                or receipt['script_hash'] != task['scripts'][metric] or actual_hash != task['scripts'][metric]
                or set(receipt['values']) != pending_ids):
            raise ValueError(f'analysis receipt mismatch: {metric}')
        if any(not isinstance(row, dict) or set(row) != set(metric) or any(type(v) is not int or not -3 <= v <= 3 for v in row.values()) for row in receipt['values'].values()):
            raise ValueError('invalid analysis delta')
        for m in metric:
            receipts[m] = {k: v[m] for k, v in receipt['values'].items()}
    result = []
    for choice in task['choices']:
        official = isinstance(choice.get('status_updates'), dict)
        row = {'choice': choice['choice'], 'metrics': {
            m: official_delta(choice, m) if official else receipts[m][str(choice['choice'])] for m in METRICS},
            'selectable': (choice.get('energy_cost', 0) <= energy
                           and (choice.get('action_id') != 'no_action'
                                or not str(choice.get('action', '')).startswith('保留体力')
                                or not harmless_action))}
        if 'energy_cost' in choice:
            row['energy_cost'] = choice['energy_cost']
        result.append(row)
    return result


def begin_stage(stage, notebooks=NOTEBOOKS):
    previous = {'observe': (None, 'reviewed'), 'analyse': ('observed',),
                'decide': ('analysed',), 'review': ('decide_running',)}
    if stage not in previous:
        raise ValueError('unknown workflow stage')
    with locked(notebooks / '.workflow.lock'):
        path = notebooks / 'workflow-state.json'
        state = read(path) if path.exists() else {}
        if state.get('phase') not in previous[stage]:
            raise ValueError(f"stage {stage} blocked by {state.get('phase')}")
        if stage != 'observe':
            current_task(notebooks, state.get('event_id'))
        save(path, {**state, 'phase': stage + '_running'})


def finish_stage(stage, notebooks=NOTEBOOKS):
    with locked(notebooks / '.workflow.lock'):
        path = notebooks / 'workflow-state.json'
        state = read(path)
        if state['phase'] != stage + '_running':
            raise ValueError('workflow stage changed during call')
        if stage == 'observe' and (notebooks / 'ending.json').exists():
            ending = read(notebooks / 'ending.json')
            if ending['session_id'] != bound_session(notebooks):
                raise ValueError('terminal session mismatch')
            save(path, {**state, 'phase': 'terminal', 'session_id': ending['session_id']})
            return {'phase': 'terminal'}
        task = current_task(notebooks)
        if stage == 'observe':
            if state.get('event_id') == task['event_id']:
                raise ValueError('observe did not load a new event')
        elif state.get('event_id') != task['event_id']:
            raise ValueError('event changed during workflow')
        if stage == 'analyse':
            option_metrics(notebooks, task['event_id'])
        if stage == 'decide':
            receipt = read(event_dir(notebooks, task) / 'action.json')
            if receipt['event_id'] != task['event_id'] or receipt['observation_hash'] != task['observation_hash']:
                raise ValueError('action receipt mismatch')
        if stage == 'review':
            row = (notebooks / 'redline-guardian-keeps.tsv').read_text().splitlines()[-1].split('\t')
            if row[0] != task['event_id'] or row[-1] != '待判定':
                raise ValueError('review did not record this event')
        phase = {'observe': 'observed', 'analyse': 'analysed', 'decide': 'review_running', 'review': 'reviewed'}[stage]
        result = {'phase': phase, 'event_id': task['event_id'], 'session_id': task['session_id']}
        save(path, {**state, **result})
        return {'phase': phase, 'event_id': task['event_id']}
