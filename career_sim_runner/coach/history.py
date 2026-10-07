"""Exact event boundaries in Jiuwen's raw ledger (never inferred from turns)."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path


def read_history(data_dir: Path, session_id: str) -> list[dict]:
    root = data_dir / "agent" / "sessions" / session_id
    path = root / "history.jsonl"
    if path.exists():
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    path = root / "history.json"
    return json.loads(path.read_text()) if path.exists() else []


def call(record: dict) -> dict:
    return record.get("tool_call") or {}


def arguments(record: dict) -> dict:
    value = call(record).get("arguments", {})
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def is_tool(record: dict, name: str) -> bool:
    actual = call(record).get("name", "")
    return actual == name or actual.endswith(("-" + name, "_" + name, "." + name))


def observe_boundary(records: list[dict], locator: dict) -> dict:
    """Resolve a controller-issued snapshot reference to one original tool call."""
    tool_id = locator.get("tool_call_id")
    if not tool_id:
        path = locator.get("observe_json_path")
        matches = [r for r in records if r.get("event_type") == "chat.tool_result"
                   and path and path in str(r.get("result", ""))]
        ids = {r.get("tool_call_id") for r in matches}
        if len(ids) != 1 or None in ids:
            raise RuntimeError("Cannot locate the first observe in raw Jiuwen history")
        tool_id = ids.pop()
    matches = [(i, r) for i, r in enumerate(records)
               if r.get("event_type") == "chat.tool_call" and call(r).get("tool_call_id") == tool_id]
    if len(matches) != 1 or not is_tool(matches[0][1], "observe"):
        raise RuntimeError("Observe tool boundary is missing or ambiguous; refusing turn fallback")
    index, record = matches[0]
    return {**locator, "tool_call_id": tool_id, "record_index": index,
            "request_id": record.get("request_id", "")}


def pruned_history_boundary(records: list[dict], checkpoint: dict) -> dict:
    """Reset at the first retained observe only if every tool call postdates the target."""
    try:
        target = datetime.fromisoformat(checkpoint["captured_at"]).timestamp()
        calls = [(i, record) for i, record in enumerate(records)
                 if record.get("event_type") == "chat.tool_call"]
        if not calls or any(float(record["timestamp"]) <= target for _, record in calls):
            raise ValueError("target tool history may still be present")
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Cannot prove Jiuwen history predates the withdrawal target") from exc
    first = next(((i, record) for i, record in calls
                  if is_tool(record, "observe")
                  and arguments(record).get("session_id") == checkpoint["session_id"]), None)
    if first is None or any(is_tool(record, "take_action") for i, record in calls if i < first[0]):
        raise RuntimeError("No safe retained observe boundary for pruned Jiuwen history")
    boundary = observe_boundary(records, {"tool_call_id": call(first[1])["tool_call_id"]})
    boundary["pruned_history_recovery"] = True
    return boundary


def legacy_boundary(records: list[dict], checkpoint: dict, previous_at: str = "") -> dict:
    """Accept old checkpoints only when a unique action proves the boundary."""
    try:
        end = datetime.fromisoformat(checkpoint["captured_at"]).timestamp()
        start = datetime.fromisoformat(previous_at).timestamp() if previous_at else 0
    except (ValueError, KeyError):
        raise RuntimeError("Legacy checkpoint has no exact observe evidence") from None
    choice = checkpoint.get("decision", {}).get("choice", {})
    actions = [(i, r) for i, r in enumerate(records) if is_tool(r, "take_action")
               and start < r.get("timestamp", 0) <= end
               and arguments(r).get("session_id") == checkpoint["session_id"]
               and arguments(r).get("choice") == choice.get("number")
               and arguments(r).get("notes") == choice.get("notes")]
    if len(actions) != 1:
        raise RuntimeError("Legacy checkpoint action is missing or ambiguous; refusing turn fallback")
    index, _ = actions[0]
    previous = max((i for i, r in enumerate(records[:index]) if is_tool(r, "take_action")), default=-1)
    observations = [r for r in records[previous + 1:index] if is_tool(r, "observe")
                    and arguments(r).get("session_id") == checkpoint["session_id"]]
    if not observations:
        raise RuntimeError("Legacy checkpoint has no first observe evidence")
    return observe_boundary(records, {"tool_call_id": call(observations[0])["tool_call_id"]})


_RULE = re.compile(r"SKILL\.md|(?:^|[/\\\s])(?:stages?|roles)/|workflow\.md|stage\.py", re.I)
_INJECTED_RULE = re.compile(r"<installed_rule\b[^>]*>.*?</installed_rule>", re.S)

def _strip_installed_rules(value):
    if isinstance(value, str):
        return _INJECTED_RULE.sub("", value)
    if isinstance(value, list):
        return [_strip_installed_rules(item) for item in value]
    if isinstance(value, dict):
        return {key: _strip_installed_rules(item) for key, item in value.items()}
    return value


_TASK = re.compile(r"task|todo|team|swarm|message|delegate", re.I)


def carries_installed_rules(record: dict) -> bool:
    """Context helpers can return rule text without naming any stage in their call."""
    payload = json.dumps(record.get("result", ""), ensure_ascii=False)
    return "<installed_rule" in payload or (
        "translation_rules" in payload and "decision_policy" in payload
    )


def retained_history(records: list[dict], tool_call_id: str) -> list[dict]:
    """Cut before observe and remove stale instructions, summaries and orphan pairs.

    Prose and coordination summaries can paraphrase obsolete rules, so rebuild
    from the retained user requests and completed tool evidence. Parallel calls
    that finish after the boundary are removed together with their results.
    """
    boundary = observe_boundary(records, {"tool_call_id": tool_call_id})
    prefix = records[:boundary["record_index"]]
    calls = {call(r).get("tool_call_id"): r for r in prefix if r.get("event_type") == "chat.tool_call"}
    results = {r.get("tool_call_id") for r in prefix if r.get("event_type") == "chat.tool_result"}
    rule_results = {r.get("tool_call_id") for r in prefix
                    if r.get("event_type") == "chat.tool_result" and carries_installed_rules(r)}
    keep = {key for key, r in calls.items() if key in results
            and key not in rule_results
            and not _RULE.search(str(call(r).get("arguments", "")))
            and not _TASK.search(call(r).get("name", ""))}
    output = []
    for record in prefix:
        kind = record.get("event_type", "")
        if kind == "chat.tool_call" and call(record).get("tool_call_id") in keep:
            output.append(record)
        elif kind == "chat.tool_result" and record.get("tool_call_id") in keep:
            output.append(record)
        elif record.get("role") == "user" and not kind.startswith("context."):
            output.append(_strip_installed_rules(record))
    return output
