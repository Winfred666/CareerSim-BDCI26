"""Read-only reconciliation with Jiuwen's durable tool-call history."""

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from career_sim_runner.paths import jiuwenswarm_data_dir


@lru_cache(maxsize=32)
def _calls(path: Path, modified_ns: int, size: int) -> tuple[dict, ...]:
    # Cache by file version, retaining no model prose or reasoning.
    content = path.read_text()
    records = (json.loads(line) for line in content.splitlines() if line.strip())
    if path.suffix == '.json':
        records = iter(json.loads(content))
    calls = []
    for record in records:
        if record.get('event_type') != 'chat.tool_call':
            continue
        tool = record.get('tool_call') or {}
        timestamp = record.get('timestamp')
        if not isinstance(timestamp, (int, float)):
            continue
        calls.append(dict(ts=datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
                          tool_call_id=tool.get('tool_call_id'),
                          role=record.get('member_name') or record.get('role'),
                          request_id=record.get('request_id')))
    return tuple(calls)


def history_calls(session_id: str) -> tuple[dict, ...] | None:
    if not session_id or Path(session_id).name != session_id:
        return None
    root = jiuwenswarm_data_dir() / 'agent' / 'sessions' / session_id
    path = root / 'history.jsonl'
    if not path.exists():
        path = root / 'history.json'
    try:
        stat = path.stat()
        return _calls(path, stat.st_mtime_ns, stat.st_size)
    except (OSError, ValueError, TypeError, OverflowError):
        # An in-progress history write cannot prove completeness.
        return None
