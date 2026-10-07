#!/usr/bin/env python3
"""Print native team calls for this event; never invoke the platform itself.

The saved batch records an issued plan, not successful native execution. The
Leader must finish every returned call before waiting, and stop on any failure.
A repeated call for that event is only for resending unfinished questionnaires.
"""
import json
from pathlib import Path
import runpy
import sys


state = runpy.run_path(str(Path(__file__).with_name('translation-state.py')))
notes = state['NOTEBOOKS']


def member_names(task):
    # The observation script owns event numbering, including skipped events.
    number = int(task['event_id'])
    return {metric: f'{metric.lower()}-{number}' for metric in state['GROUPS']}


def batch(task):
    return {key: task[key] for key in ('session_id', 'event_id', 'observation_hash')} | {
        'members': member_names(task)}


def unfinished(task):
    directory = state['event_dir'](notes, task)
    missing = []
    for metric in state['GROUPS']:
        path = directory / metric / 'complete.json'
        if not path.exists():
            missing.append(metric)
            continue
        receipt = state['read'](path)
        if (not isinstance(receipt, dict)
                or receipt.get('observation_hash') != task['observation_hash']
                or receipt.get('script_hash') != task['scripts'][metric]):
            raise ValueError(f'analysis receipt mismatch: {metric}')
    return missing


def call(tool, **arguments):
    return {'tool': tool, 'arguments': arguments}


def commands():
    with state['locked'](notes / '.dispatch.lock'):
        task = state['current_task'](notes)
        workflow = state['read'](notes / 'workflow-state.json')
        if (workflow.get('phase') != 'analyse_running'
                or workflow.get('event_id') != task['event_id']):
            raise ValueError('dispatch is only available during the current analysis stage')
        if all(isinstance(choice.get('status_updates'), dict) for choice in task['choices']):
            raise ValueError('official deltas require direct decide, not dispatch')
        current = batch(task)
        path = notes / 'dispatch-state.json'
        previous = state['read'](path) if path.exists() else None
        if previous is not None:
            if previous['session_id'] != task['session_id']:
                raise ValueError('dispatch session mismatch')
            archived = state['read'](state['event_dir'](notes, previous) / 'task.json')
            if previous != batch(archived):
                raise ValueError('saved dispatch batch does not match its event')
            if previous['event_id'] == task['event_id']:
                if previous != current:
                    raise ValueError('dispatch observation changed within the event')
            elif int(previous['event_id']) >= int(task['event_id']):
                raise ValueError('cannot dispatch an earlier event')
            elif unfinished(archived):
                raise ValueError('cannot retire an unfinished translator batch')

        missing = unfinished(task)
        if not missing:
            return {'commands': []}
        result = []
        if previous != current:
            # Keep new members alive before requesting any old shutdown, so the
            # pinned runtime never sees every non-leader member shut down.
            for metric, name in current['members'].items():
                role = state['ROOT'] / 'stages' / f'translate_{metric}.md'
                result.append(call('spawn_teammate', member_name=name,
                                   display_name=metric, desc='', prompt=(
                                       f'仅在收到 Leader 的“{name}” DM 后，严格读取并遵守 {role}，不偏离。')))
            if previous is not None:
                result.extend(call('shutdown_member', member_name=previous['members'][metric],
                                   force=False) for metric in state['GROUPS'])
            # A retry must reuse this batch, never allocate names or repeat spawn.
            state['save'](path, current)
        result.extend(call('send_message', to=current['members'][metric],
                           content=current['members'][metric]) for metric in missing)
        return {'commands': result}


if __name__ == '__main__':
    try:
        if sys.argv[1:]:
            raise ValueError('usage: dispatch.py (no arguments)')
        print(json.dumps(commands(), ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'dispatch_error: {exc}', file=sys.stderr)
        sys.exit(2)
