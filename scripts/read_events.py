#!/usr/bin/env python3
"""Turn a Jiuwen events JSONL file into a readable Markdown chat stream.

Usage:
    python scripts/read_events.py path/to/events-20260913T162929Z.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any


DEFAULT_WIDTH = 100
DEFAULT_FOLD_LINES = 14


def _display_width(text: str) -> int:
    """Return an approximate terminal-cell width for Unicode text."""
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def _wrap_line(line: str, width: int) -> list[str]:
    """Wrap one line by display width, preferring whitespace boundaries."""
    line = line.expandtabs(4).rstrip()
    if not line:
        return [""]

    indent = line[: len(line) - len(line.lstrip())]
    continuation_indent = indent[: min(len(indent), width // 4)]
    remaining = line
    wrapped: list[str] = []

    while _display_width(remaining) > width:
        used = 0
        limit = 0
        last_space = -1
        for index, char in enumerate(remaining):
            char_width = 0 if unicodedata.combining(char) else (
                2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
            )
            if used + char_width > width:
                break
            used += char_width
            limit = index + 1
            if char.isspace():
                last_space = index

        if limit == 0:
            limit = 1
        if last_space >= max(1, limit // 2):
            limit = last_space + 1

        wrapped.append(remaining[:limit].rstrip())
        remaining = continuation_indent + remaining[limit:].lstrip()

    wrapped.append(remaining)
    return wrapped


def _wrap_text(text: str, width: int) -> list[str]:
    """Wrap all physical lines while preserving intentional blank lines."""
    source_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    wrapped: list[str] = []
    for line in source_lines:
        wrapped.extend(_wrap_line(line, width))
    while wrapped and not wrapped[-1]:
        wrapped.pop()
    return wrapped or [""]


def _decode_json_strings(value: Any, depth: int = 0) -> Any:
    """Decode nested JSON strings commonly found in tool arguments/results."""
    if depth > 5:
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return _decode_json_strings(json.loads(stripped), depth + 1)
            except json.JSONDecodeError:
                return value
        return value
    if isinstance(value, dict):
        return {key: _decode_json_strings(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_json_strings(item, depth + 1) for item in value]
    return value


def _as_text(value: Any) -> str:
    """Render event content as readable plain text."""
    value = _decode_json_strings(value)
    if isinstance(value, str):
        text = value.strip()
        # Some Jiuwen tool adapters put a Python-style repr in ``result``.
        # Expand its two most useful escapes even when the repr was truncated
        # and therefore cannot be parsed safely.
        if "\\n" in text and "\n" not in text:
            text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "    ")
        return text
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False)


def _event_node(record: dict[str, Any]) -> dict[str, Any]:
    """Unwrap the duplicate delta envelope used by Jiuwen streams."""
    payload = record.get("payload", {})
    if not isinstance(payload, dict):
        return {}
    delta = payload.get("delta")
    if isinstance(delta, dict) and delta.get("event_type"):
        return delta
    return payload


def _actor(node: dict[str, Any]) -> str:
    """Return the human-facing role name for an event node."""
    if node.get("role") == "leader":
        return "Leader"
    member_name = node.get("member_name")
    if member_name:
        return str(member_name)
    if node.get("role"):
        return str(node["role"])
    return "System"


def _parse_arguments(tool_call: dict[str, Any]) -> Any:
    arguments = tool_call.get("arguments", {})
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
    return arguments


def _logical_key(record: dict[str, Any], node: dict[str, Any]) -> tuple[str, str] | None:
    """Identify duplicate delta/full records for the same logical event."""
    kind = str(record.get("kind", "event"))
    if kind == "tool_call":
        tool_call = node.get("tool_call")
        if isinstance(tool_call, dict) and tool_call.get("tool_call_id"):
            return kind, str(tool_call["tool_call_id"])
    elif kind == "tool_result" and node.get("tool_call_id"):
        return kind, str(node["tool_call_id"])
    return None


def _event_view(record: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return actor, recipient, optional summary, and body for one event."""
    kind = str(record.get("kind", "event"))
    node = _event_node(record)
    actor = _actor(node)

    if kind == "tool_call":
        tool_call = node.get("tool_call")
        if not isinstance(tool_call, dict):
            return actor, "unknown tool", "", _as_text(node)
        name = str(tool_call.get("name", "unknown tool"))
        arguments = _parse_arguments(tool_call)
        summary = str(tool_call.get("display_name", "") or "")
        if name == "send_message" and isinstance(arguments, dict):
            recipient = str(arguments.get("to", "unknown recipient"))
            body = str(arguments.get("content", "") or "")
            summary = str(arguments.get("summary", "") or summary)
            return actor, recipient, summary, body
        return actor, name, summary, _as_text(arguments)

    if kind == "tool_result":
        tool_name = str(node.get("tool_name", "tool"))
        value = node.get("raw_output")
        if value is None:
            value = node.get("result", node)
        return tool_name, actor, "", _as_text(value)

    if kind in {"usage", "usage_summary"}:
        return "Usage", str(record.get("actor") or actor), "", _as_text(record.get("usage", node))
    if kind == "frame_final":
        return "System", actor, "stream completed", _as_text(record)
    return actor, kind, "", _as_text(node)


def _render_body(body: str, width: int, fold_lines: int) -> list[str]:
    lines = _wrap_text(body, width)
    longest_fence = max((len(run) for line in lines for run in re.findall(r"`+", line)), default=0)
    fence = "`" * max(3, longest_fence + 1)
    if len(lines) > fold_lines:
        return [
            f"<details><summary>... ({len(lines)} 行文本)</summary>",
            "",
            f"{fence}text",
            *lines,
            fence,
            "",
            "</details>",
        ]
    return [f"{fence}text", *lines, fence]


def build_markdown(events_path: Path, width: int, fold_lines: int) -> str:
    """Build a deduplicated, chat-like Markdown stream."""
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    invalid_lines: list[int] = []

    with events_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                invalid_lines.append(line_number)
                continue
            if not isinstance(record, dict):
                invalid_lines.append(line_number)
                continue
            node = _event_node(record)
            key = _logical_key(record, node)
            if key is not None and key in seen:
                continue
            if key is not None:
                seen.add(key)
            records.append(record)

    roles: Counter[str] = Counter()
    for record in records:
        node = _event_node(record)
        actor = _actor(node)
        if actor != "System":
            roles[actor] += 1

    lines = [
        "# Jiuwen Swarm event chat",
        "",
        f"- Source: `{events_path.name}`",
        f"- Logical events: {len(records)} (duplicate stream envelopes removed)",
        f"- Roles: {', '.join(roles) if roles else 'none recorded'}",
        f"- Layout: Unicode display width {width}; bodies over {fold_lines} lines are folded",
    ]
    if invalid_lines:
        lines.append(f"- Skipped malformed JSONL lines: {', '.join(map(str, invalid_lines))}")
    lines.extend(
        [
            "",
            "> This view can only show data present in the structured events log. Hidden reasoning and",
            "> unrecorded plain assistant text cannot be reconstructed.",
            "",
        ]
    )

    for record in records:
        seq = record.get("seq", "?")
        timestamp = str(record.get("ts", ""))
        actor, recipient, summary, body = _event_view(record)
        lines.extend(
            [
                f"## #{seq} · {timestamp}",
                "",
                f"**{actor} → {recipient}**",
                "",
            ]
        )
        if summary:
            lines.extend(_render_body(summary, width, fold_lines))
            lines.append("")
        lines.extend(_render_body(body, width, fold_lines))
        lines.extend(["", "---", ""])

    return "\n".join(lines).rstrip() + "\n"


def _default_output_path(events_path: Path) -> Path:
    name = events_path.name
    if name.startswith("events-") and name.endswith(".jsonl"):
        name = f"chat-{name[len('events-') : -len('.jsonl')]}.md"
    else:
        name = f"{events_path.stem}-chat.md"
    return events_path.with_name(name)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("events", type=Path, help="input events-*.jsonl file")
    parser.add_argument("-o", "--output", type=Path, help="output Markdown path")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH, help="maximum text display width (default: 100)")
    parser.add_argument(
        "--fold-lines",
        type=int,
        default=DEFAULT_FOLD_LINES,
        help="fold bodies longer than this many wrapped lines (default: 14)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.events.is_file():
        raise SystemExit(f"events log not found: {args.events}")
    if args.width < 40:
        raise SystemExit("--width must be at least 40")
    if args.fold_lines < 1:
        raise SystemExit("--fold-lines must be positive")

    output_path = args.output or _default_output_path(args.events)
    output_path.write_text(
        build_markdown(args.events, width=args.width, fold_lines=args.fold_lines),
        encoding="utf-8",
    )
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
