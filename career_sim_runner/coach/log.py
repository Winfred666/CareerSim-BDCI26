"""Structured coach evidence appended to the emulator's session log.

The coach controller is intentionally outside the Career Emulator MCP server,
but it still needs one durable, human-auditable source of truth.  This module
adds JSON records to the same ``<session_id>.log`` file that the emulator uses.
The records contain the complete question, selected option, consequence,
state-after snapshot, and next question.  Reads are tail based so inspection
does not load an unbounded game log into memory.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

COACH_STEP_PREFIX = "[coach_step] "
DEFAULT_TAIL_BYTES = 512 * 1024


def write_career_log(output: Path, ledger: dict[str, Any]) -> None:
    """Render all attempts, including withdrawn branches, as readable state evidence."""
    lines = ["CareerSim 状态日志", "已撤回的记录仅为历史证据，不代表当前游戏进度。", ""]

    def fields(value: Any, indent: str = "  ") -> None:
        if isinstance(value, dict) and value:
            for key, item in value.items():
                if isinstance(item, (dict, list)) and item:
                    lines.append(f"{indent}{key}:")
                    fields(item, indent + "  ")
                else:
                    lines.append(f"{indent}{key}: {item}")
        elif isinstance(value, list) and value:
            for index, item in enumerate(value, 1):
                lines.append(f"{indent}[{index}]")
                fields(item, indent + "  ")
        else:
            lines.append(f"{indent}{value}")

    for row in ledger.get("attempts", []):
        evidence = row.get("career_state") or {}
        status = "已撤回" if row.get("withdrawn_at") else ("已提交" if evidence else "未记录状态提交")
        lines.extend([
            f"=== 尝试 {row['attempt']} | 回合 {row.get('round', '未知')} | {status} ===",
            f"时间: {row.get('started_at', '未知')} | 阶段: {row.get('phase', 'decision')}",
            f"游戏会话: {row.get('game_session_id', '未知')}",
        ])
        if row.get("withdrawn_at"):
            lines.append(f"撤回时间: {row['withdrawn_at']}")
        if evidence:
            for key, label in (("question", "当前题目与选项"), ("choice", "实际选择"),
                               ("transition", "状态变化（before → after）"),
                               ("state_after", "行动后完整状态"), ("next_question", "下一题目")):
                lines.append(label + ":")
                fields(evidence.get(key, "未知"))
        else:
            lines.append(f"运行结果: {row.get('termination_reason') or '等待状态记录'}")
        lines.append("")
    for correction in ledger.get("corrections", []):
        lines.append(f"纠正记录 [{correction.get('created_at')}] 回合 {correction.get('round')}: {correction.get('reason')}")
    target = output / "career.log"
    temporary = target.with_suffix(".log.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(target)


def session_log_path(log_dir: Path, session_id: str) -> Path:
    """Return the emulator log path for a game session."""
    return log_dir / f"{session_id}.log"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_coach_step_log(
    log_dir: Path,
    session_id: str,
    *,
    round_number: int,
    question: dict[str, Any],
    decision: dict[str, Any],
    action_result: dict[str, Any] | None,
    consequence: dict[str, Any],
    state_after: dict[str, Any],
    next_question: dict[str, Any],
) -> Path:
    """Append one complete coach decision record to the emulator log."""
    log_path = session_log_path(log_dir, session_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "round": round_number,
        "created_at": _utc_now(),
        "question": question,
        "decision": decision,
        "action_result": action_result or {},
        "consequence": consequence,
        "state_after": state_after,
        "next_question": next_question,
    }
    with log_path.open("a", encoding="utf-8") as handle:
        if log_path.stat().st_size == 0:
            handle.write(f"Log file: {log_path.resolve()}\n")
        handle.write(COACH_STEP_PREFIX)
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
        handle.write("\n")
    return log_path


def _tail_lines(log_path: Path, max_bytes: int) -> tuple[list[str], bool]:
    """Read only the tail of *log_path* and report whether it was truncated."""
    if max_bytes <= 0 or not log_path.is_file():
        return [], False
    with log_path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        offset = max(0, size - max_bytes)
        handle.seek(offset)
        raw = handle.read()
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    truncated = offset > 0
    # The first line may begin in the middle of a JSON record or emulator log
    # entry.  Drop it rather than exposing a misleading partial line.
    if truncated and lines:
        lines = lines[1:]
    return lines, truncated


def read_session_log_tail(log_path: Path, *, max_bytes: int = DEFAULT_TAIL_BYTES) -> dict[str, Any]:
    """Return recent raw log lines without reading the entire file."""
    lines, truncated = _tail_lines(log_path, max_bytes)
    return {"lines": lines, "truncated": truncated, "max_bytes": max_bytes}


def read_coach_step_logs(
    log_path: Path,
    *,
    start_round: int | None = None,
    end_round: int | None = None,
    max_bytes: int = DEFAULT_TAIL_BYTES,
) -> list[dict[str, Any]]:
    """Parse coach records from a tail of the unified emulator log."""
    lines, _truncated = _tail_lines(log_path, max_bytes)
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.startswith(COACH_STEP_PREFIX):
            continue
        try:
            record = json.loads(line[len(COACH_STEP_PREFIX) :])
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        round_number = record.get("round")
        if isinstance(round_number, bool) or not isinstance(round_number, int):
            continue
        if start_round is not None and round_number < start_round:
            continue
        if end_round is not None and round_number > end_round:
            continue
        records.append(record)
    return records


def rewrite_session_log(
    log_path: Path,
    entries: Iterable[dict[str, Any]],
    coach_records: Iterable[dict[str, Any]] = (),
) -> None:
    """Rewrite emulator entries while retaining coach evidence records.

    Withdrawal restores the emulator's structured log table and therefore
    rewrites the plain-text log.  Retained coach records are appended after the
    restored emulator entries so they remain available to ``inspect``.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"Log file: {log_path.resolve()}"]
    for entry in entries:
        lines.append(f"[{entry.get('entry_type', 'system')}] {entry.get('message', '')}")
    for record in coach_records:
        lines.append(COACH_STEP_PREFIX + json.dumps(record, ensure_ascii=False, sort_keys=True))
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
