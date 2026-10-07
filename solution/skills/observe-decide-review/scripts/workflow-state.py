#!/usr/bin/env python3
"""Advance the synchronous workflow using the existing event-bound receipts."""
from pathlib import Path
import json
import runpy
import sys
import time

state = runpy.run_path(str(Path(__file__).with_name('translation-state.py')))
notes = state['NOTEBOOKS']


class DecisionContextError(ValueError):
    """A completed workflow whose decision packet could not be read."""


def create_new(session_id, workspace=None):
    from uuid import UUID
    root = state['ROOT']
    workspace = root.parent.parent if workspace is None else Path(workspace)
    if not workspace.is_absolute() or not workspace.is_dir():
        raise ValueError('workspace must be an existing absolute directory')
    if (workspace / 'skills' / root.name).resolve() != root:
        raise ValueError('workspace does not contain this installed skill')
    session_id = UUID(session_id).hex
    path = notes / 'workflow-state.json'
    with state['locked'](notes / '.workflow.lock'):
        previous = state['read'](path) if path.exists() else {}
        if previous.get('session_id') not in (None, session_id):
            raise ValueError('cannot replace an existing session')
        if previous.get('workspace') not in (None, str(workspace)):
            raise ValueError('cannot replace an existing workspace')
        documents = {p: p.read_text(encoding='utf-8').replace(
            '<K>', str(workspace)).replace('<session_id>', session_id)
            for p in root.rglob('*.md')}
        for document, content in documents.items():
            if content != document.read_text(encoding='utf-8'):
                temporary = document.with_suffix('.md.tmp')
                temporary.write_text(content, encoding='utf-8')
                temporary.replace(document)
        state['save'](path, {**previous, 'session_id': session_id, 'workspace': str(workspace)})
    return '已替换此后命令所需 <K> 和 <session_id>，请重读 SKILL.md ，并继续初始化第 2 步，不允许读其他文件'


def phase():
    path = notes / 'workflow-state.json'
    return state['read'](path).get('phase') if path.exists() else None


def missing_analyses():
    task = state['current_task']()
    directory = state['event_dir'](notes, task)
    return [metric for metric in state['GROUPS']
            if not (directory / metric / 'complete.json').exists()]


def ready_for_decide():
    if phase() == 'analyse_running':
        state['finish_stage']('analyse')
    if phase() == 'analysed':
        state['begin_stage']('decide')


def resume(observation_path=None):
    session = state['bound_session'](notes, required=False)
    current = phase()
    result = {'session_id': session, 'stage': 'observe'}
    if not session:
        return result
    if current in (None, 'reviewed', 'terminal'):
        return {**result, 'stage': 'terminal' if current == 'terminal' else 'observe'}
    task = state['current_task']()
    if (current == 'review_running'
            or (current == 'decide_running' and (state['event_dir'](notes, task) / 'action.json').exists())):
        return {**result, 'stage': 'review'}
    if current not in ('observed', 'analyse_running', 'analysed', 'decide_running'):
        raise ValueError(f'cannot resume phase: {current}')
    if observation_path is None:
        return {**result, 'stage': 'synchronize'}
    observation = state['read'](Path(observation_path))
    review = state['read'](notes / 'review-context.json')
    public = state['read'](state['event_dir'](notes, task) / 'public-event.json')
    event = {k: v for k, v in observation.get('current_event', {}).items()
             if k in ('title', 'description')}
    choices = [{k: c[k] for k in ('choice', 'action', 'description') if k in c}
               for c in observation.get('choices', [])]
    if (observation.get('current_state') != review['current_state']
            or event != public['current_event'] or choices != public['choices']):
        raise ValueError('resume observation does not match pending event')
    missing = missing_analyses()
    if missing:
        if current == 'observed':
            state['begin_stage']('analyse')
        dispatch = runpy.run_path(str(Path(__file__).with_name('dispatch.py')))
        members = dispatch['member_names'](task)
        return {**result, 'stage': 'dispatch', 'members': [members[metric] for metric in missing]}
    ready_for_decide()
    state['option_metrics']()
    return {**result, 'stage': 'decide'}


def wait(timeout_s=240):
    """Wait for this event and return its decision packet, including on retry."""
    deadline = time.monotonic() + timeout_s
    while True:
        current = phase()
        if current == 'terminal':
            return 'terminal'
        if current not in ('analyse_running', 'analysed', 'decide_running'):
            raise ValueError(f'wait is unavailable during phase: {current}')
        task = state['current_task']()
        workflow = state['read'](notes / 'workflow-state.json')
        if workflow.get('event_id') != task['event_id']:
            raise ValueError('wait event does not match current workflow')
        if (state['event_dir'](notes, task) / 'action.json').exists():
            raise ValueError('event already acted; resume review instead of waiting')
        if current == 'analyse_running':
            if not missing_analyses():
                ready_for_decide()
                return decision_context()
        else:
            ready_for_decide()
            return decision_context()
        if time.monotonic() >= deadline:
            missing = ','.join(missing_analyses()) if current == 'analyse_running' else str(current)
            if current == 'analyse_running':
                marker = state['event_dir'](notes, state['current_task']()) / '.wait-timeout.json'
                if not marker.exists():
                    state['save'](marker, {'missing': missing})
                    return 'waiting'
            raise ValueError('completion timeout: ' + missing)
        time.sleep(0.25)

def decision_context():
    try:
        context = runpy.run_path(str(Path(__file__).with_name('read-context.py')))
        return context['read_context']()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DecisionContextError(str(exc)) from exc


if __name__ == '__main__':
    try:
        if len(sys.argv) == 3 and sys.argv[1] == 'create_new':
            print(create_new(sys.argv[2]))
        elif len(sys.argv) == 4 and sys.argv[1] == 'create_new':
            print(create_new(sys.argv[3], sys.argv[2]))
        elif sys.argv[1:] == ['wait']:
            result = wait()
            if result == 'waiting':
                print('角色尚未完成，继续执行同一等待命令；不要重发消息。')
            elif isinstance(result, dict):
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(result)
        elif len(sys.argv) in (2, 3) and sys.argv[1] == 'resume':
            print(json.dumps(resume(sys.argv[2] if len(sys.argv) == 3 else None), ensure_ascii=False))
        else:
            raise ValueError('usage: workflow-state.py create_new <session_id> | wait | resume [observe_json_path]')
    except DecisionContextError as exc:
        print(f'context_error: {exc}', file=sys.stderr)
        sys.exit(2)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'workflow_error: {exc}', file=sys.stderr)
        sys.exit(2)
