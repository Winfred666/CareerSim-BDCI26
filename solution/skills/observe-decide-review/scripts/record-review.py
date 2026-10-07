#!/usr/bin/env python3
"""Read the public session log and record the latest action against observe."""
import importlib.util
import json
from pathlib import Path
import re
import runpy
import sys

spec = importlib.util.spec_from_file_location("refresh", Path(__file__).with_name("refresh-context.py"))
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)


def latest_action(log_file, task, public):
    path = Path(log_file).resolve()
    if path.name != task['session_id'] + '.log':
        raise ValueError('log session mismatch')
    text = path.read_text(encoding='utf-8')
    header = text.splitlines()[0] if text else ''
    if not header.startswith('Log file: ') or Path(header[10:]).name != path.name:
        raise ValueError('log header session mismatch')
    entries = list(re.finditer(r'^\[([a-z_]+)\] ', text, re.MULTILINE))
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if entry[1] not in ('action', 'fixed_event'):
            continue
        message = text[entry.end():entries[index + 1].start() if index + 1 < len(entries) else len(text)].rstrip('\n')
        chosen = re.search(r'^Chose #(\d+): ', message, re.MULTILINE)
        if chosen is None:
            if entry[1] == 'fixed_event' and message.startswith('Month 1 | Initialization\n'):
                continue
            raise ValueError('latest decision has no choice')
        expected = f"Month {public['current_month']} | {public['current_event']['title']}"
        if not message.startswith(expected + '\n'):
            raise ValueError('latest decision does not match the observed event')
        choice = int(chosen[1])
        selected = next((c for c in task['choices'] if c['choice'] == choice), None)
        if selected is None:
            raise ValueError('choice is not in the observed menu')
        tail = message[chosen.end():]
        action = selected.get('action', '')
        if not action or not tail.startswith(action) or tail[len(action):len(action) + 1] not in ('', '\n'):
            raise ValueError('logged action does not match the observed choice')
        lines = tail[len(action):].strip('\n').splitlines()
        updates = {}
        if lines and not lines[0].startswith(('Energy cost:', 'Energy remaining:', 'Notes:')):
            fields = {name.lower(): metric for metric, name in refresh.METRICS.items()}
            for part in lines[0].split(','):
                delta = re.fullmatch(r'\s*([A-Za-z][A-Za-z0-9_]*)\s+([+-]\d+)\s*', part)
                if delta is None:
                    raise ValueError('invalid nominal effect in decision log')
                metric = fields.get(delta[1].lower())
                if metric is not None:
                    if metric in updates:
                        raise ValueError('duplicate nominal effect in decision log')
                    updates[metric] = int(delta[2])
        return updates, choice, index
    raise ValueError('session log has no decision')


def record_log(notebooks, log_file):
    api = runpy.run_path(str(Path(__file__).with_name('translation-state.py')))
    workflow_path = notebooks / 'workflow-state.json'
    workflow = api['read'](workflow_path)
    phase = workflow.get('phase')
    if phase not in ('decide_running', 'review_running', 'reviewed'):
        raise ValueError('review is only available after decide')
    task = api['current_task'](notebooks, workflow.get('event_id'))
    observation = api['read'](notebooks / refresh.REVIEW_CONTEXT)
    if (observation.get('event_key') != task['event_id']
            or observation.get('current_state', {}).get('session_id') != task['session_id']):
        raise ValueError('review context does not match the current event')
    directory = api['event_dir'](notebooks, task)
    updates, choice, index = latest_action(log_file, task, api['read'](directory / 'public-event.json'))
    cursor = {'file': str(Path(log_file).resolve()), 'entry': index, 'event_id': task['event_id']}
    previous = workflow.get('review_log')
    if previous and (previous['file'] != cursor['file']
                     or (previous['entry'] >= index and previous != cursor)):
        raise ValueError('decision log was already reviewed or replaced')
    receipt_path = directory / 'action.json'
    receipt = {'event_id': task['event_id'], 'observation_hash': task['observation_hash'],
               'choice': choice, 'updates': updates, 'log': cursor}
    if receipt_path.exists():
        saved = api['read'](receipt_path)
        if (any(saved.get(key) != receipt[key] for key in ('event_id', 'observation_hash', 'choice'))
                or any(saved[key] != receipt[key] for key in ('updates', 'log') if key in saved)):
            raise ValueError('conflicting action receipt')
    if phase == 'reviewed' and (previous != cursor or not receipt_path.exists()):
        raise ValueError('completed review does not match the decision log')
    api['save'](receipt_path, receipt)
    if phase == 'decide_running':
        api['begin_stage']('review', notebooks)
    result = record(notebooks, updates, choice)
    api['remember_action'](notebooks, task, choice)
    api['save'](workflow_path, {**api['read'](workflow_path), 'review_log': cursor})
    if phase != 'reviewed':
        api['finish_stage']('review', notebooks)
    return result


def record(notebooks, updates, choice=None):
    observation = json.loads((notebooks / refresh.REVIEW_CONTEXT).read_text(encoding="utf-8"))
    event_key = observation["event_key"]
    if not re.fullmatch(r"[0-9]{5}", event_key) or event_key == "00000":
        raise ValueError("invalid event key")
    current = observation["current_state"]
    session = json.loads((notebooks / "workflow-state.json").read_text())["session_id"]
    if not session or current.get("session_id") != session:
        raise ValueError("observation session mismatch")
    status = current["status"]
    if choice is not None and (type(choice) is not int or choice not in {
        item.get("choice") for item in observation.get("choices", [])
    }):
        raise ValueError("choice is not in the observed menu")
    level = refresh.level_number(status["level"])
    if not isinstance(updates, dict) or level is None:
        raise ValueError("invalid log delta")
    fields = {name.lower(): metric for metric, name in refresh.METRICS.items()}
    fields.update({metric.lower(): metric for metric in refresh.METRICS})
    changes = {}
    for key, value in updates.items():
        metric = fields.get(key.lower())
        if metric is None or metric in changes or type(value) is not int:
            raise ValueError("invalid or duplicate log metric")
        changes[metric] = value
    actual = {}
    for metric, field in refresh.METRICS.items():
        value = status[field]
        if type(value) is not int:
            raise ValueError("invalid public status")
        change = changes.get(metric, 0)
        if metric == "W" and change < 0:
            change = round(change * (1 + round((level / 4) ** 2)))
        actual[metric] = value + change
        if metric in "HD":
            actual[metric] = min(10, max(0, actual[metric]))
    # Keep the action-before row intact. A retry checks the pending row against it.
    path = notebooks / "redline-guardian-keeps.tsv"
    original, lines, populated = refresh.read_table(path, 3)
    last = refresh.cells(lines[populated[-1]])
    pending_retry = int(event_key) == int(last[0]) and last[2] == "待判定"
    if int(event_key) != int(last[0]) + 1 and not pending_retry:
        raise ValueError("review event is not the next event")
    base_state = refresh.cells(lines[populated[-2]])[1] if pending_retry else last[1]
    # Review owns only the six public action deltas. Observe owns feedback;
    # carry its R estimate unchanged instead of replaying old observation events.
    risk = refresh.risk_from_state(base_state)
    state = refresh.format_state(level, {**actual, "R": risk})
    row = f"{event_key}\t{state}\t待判定\n".encode()
    if choice is not None:
        runpy.run_path(str(Path(__file__).with_name('questionnaire-feedback.py')))['cache_review'](
            notebooks, changes, choice)
    if last == row.decode().rstrip().split("\t"):
        return "review_recorded " + event_key
    if pending_retry:
        raise ValueError("pending review differs from the original action")
    refresh.atomic_update(path, original, original + (b"" if original.endswith(b"\n") else b"\n") + row)
    return "review_recorded " + event_key


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("usage: record-review.py <log_file>")
        record_log(refresh.NOTEBOOKS, sys.argv[1])
        print("review_recorded")
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"review_record_error: {error}", file=sys.stderr)
        sys.exit(2)
