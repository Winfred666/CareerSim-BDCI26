"""Durable attempt costs; token attribution uses WebSocket receipt windows."""

from __future__ import annotations

import csv
import json
import math
import re
import shutil
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from career_sim_runner.models import TokenUsage
from career_sim_runner.coach.log import write_career_log
from career_sim_runner.coach.stream_audit import history_calls
from scripts.read_events import build_markdown, DEFAULT_WIDTH, DEFAULT_FOLD_LINES


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def snapshot_solution(source: Path, output: Path) -> None:
    """Freeze the source once; runtime reloads preserve separate revisions."""
    output.mkdir(parents=True, exist_ok=True)
    target = output / "solution"
    if target.is_dir():
        return
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))


def allocate_snapshot(root: Path, source: Path, keyword: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}", keyword):
        raise ValueError("solution keyword must be 1–40 letters, digits, underscores or hyphens")
    base = root / f"{datetime.now().astimezone():%m%d%H%M}-{keyword}"
    output = base
    index = 2
    while True:
        try:
            output.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            output = base.with_name(f"{base.name}-{index}")
            index += 1
    snapshot_solution(source, output)
    return output


def runtime_dir(output: Path) -> Path:
    return output.parent / ".coach-runtime" / output.name


def _state_path(output: Path) -> Path:
    return output.parent / ".coach-runtime" / "benchmark.sqlite3"


def _open_state(output: Path):
    path = _state_path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.execute("CREATE TABLE IF NOT EXISTS benchmarks (output TEXT PRIMARY KEY, document TEXT NOT NULL)")
    return db


def read_ledger(output: Path) -> dict:
    if _state_path(output).is_file():
        with closing(sqlite3.connect(_state_path(output), timeout=30)) as db:
            row = db.execute("SELECT document FROM benchmarks WHERE output=?", (str(output.resolve()),)).fetchone()
            if row:
                return json.loads(row[0])
    # Read-only migration of old runs. Never emit these files again.
    legacy = output / "benchmark.json"
    return json.loads(legacy.read_text(encoding="utf-8")) if legacy.is_file() else {}


def _shorttitle(row: dict) -> str:
    event = (row.get("question") or {}).get("event") or (row.get("decision") or {}).get("event") or {}
    title = row.get("shorttitle") or ("重载" if row.get("phase") == "reload" else event.get("title"))
    return str(title or f"事件 {row.get('round', row['attempt'])}").replace("|", "／").replace("\n", " ")[:14]


def _ending_summary(data: dict) -> dict:
    # A rewind changes the live branch, not the fact that an ending was observed.
    # Use the latest verified ending, never the best score or a partial attempt.
    latest = next((r for r in reversed(data["attempts"])
                   if r.get("phase", "decision") == "decision"
                   and r.get("final_score") is not None), {})
    return {"final_score": latest.get("final_score"),
            "final_score_withdrawn": bool(latest.get("withdrawn_at")) if latest else None}


def _summarize(data: dict[str, Any]) -> None:
    rows = data["attempts"]
    known = [r for r in rows if r.get("token_usage") is not None]
    elapsed = [r["elapsed_s"] for r in rows if r.get("elapsed_s") is not None]
    corrections = data.setdefault("corrections", [])
    policy = [c for c in corrections if c["kind"] == "policy"]
    data["summary"] = {
        "attempts": len(rows),
        "successful_attempts": sum(r.get("action_success") is True and r.get("phase", "decision") == "decision" for r in rows),
        "elapsed_s": sum(elapsed),
        "reported_total_tokens": sum(r["token_usage"]["total_tokens"] for r in known) if known else None,
        "incomplete_usage_attempts": sum(r.get('usage_status') in {'partial_roles', 'partial_stream', 'unverified'} for r in rows),
        "coach_corrections": len(policy),
        "corrected_decisions": len({(c.get("game_session_id"), c.get("round")) for c in policy}),
        "infrastructure_interventions": sum(c["kind"] == "infrastructure" for c in corrections),
        "experimental_rewinds": sum(c["kind"] == "experiment" for c in corrections),
    }
    data["summary"].update(_ending_summary(data))
    time_total = 0.0
    token_total = 0
    has_usage = False
    for row in rows:
        time_total += row.get("elapsed_s") or 0
        token_total += (row.get("token_usage") or {}).get("total_tokens", 0)
        has_usage |= row.get("token_usage") is not None
        row["cumulative_elapsed_s"] = time_total
        row["cumulative_reported_tokens"] = token_total if has_usage else None


def _write_markdown(output: Path, data: dict) -> None:
    rows = data["attempts"]
    lines = [f"# {data.get('report_title') or output.name}", "",
             f"Jiuwen Player 全角色；Coach 纠正：{data['summary']['coach_corrections']} 次。", "",
             "| 事件 shorttitle | 标志 token | 累计 token | Agent 新增上下文 | Agent 累计上下文 | 本轮用时 | 累计用时 |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    if data.get('context_mode') == 'isolated-events':
        first = data.get('isolated_from_round', 1)
        lines[4:4] = [f"评测模式：isolated-events，从第 {first} 轮起；此前为 persistent。", ""]
    if data['summary'].get('incomplete_usage_attempts'):
        lines[4:4] = ["存在用量不完整或未经历史核验的事件；累计 token 仅为已采集合计。", ""]
    for row in rows:
        tokens = (row.get("token_usage") or {}).get("total_tokens")
        total = row.get("cumulative_reported_tokens")
        amount = f"{tokens / 1_000_000:.3f} M" if tokens is not None else "—"
        cumulative = f"{total / 1_000_000:.3f} M" if total is not None else "—"
        leader_delta = row.get("leader_context_delta_tokens")
        leader_context = row.get("leader_context_tokens")
        if leader_delta is None:
            context_delta = "—"
        else:
            context_delta_m = leader_delta / 1_000_000
            context_delta = f"{0.0 if round(context_delta_m, 3) == 0 else context_delta_m:.3f} M"
        context_total = f"{leader_context / 1_000_000:.3f} M" if leader_context is not None else "—"
        seconds = row.get("elapsed_s")
        duration = f"{seconds:.1f} s" if seconds is not None else "—"
        lines.append(f"| {_shorttitle(row)} | {amount} | {cumulative} | {context_delta} | {context_total} | {duration} | {row['cumulative_elapsed_s']:.1f} s |")
    for row in rows:
        if not row.get("by_role") and not row.get('stream_audit_issues'):
            continue
        lines.extend(["", f"### 事件〈{_shorttitle(row)}〉：各角色用量", ""])
        if row.get('usage_status') in {'partial_roles', 'partial_stream'}:
            lines.extend(["用量不完整：仅列已采集用量，合计为已知下限。", ""])
        if row.get('stream_audit_issues'):
            lines.extend(["流校验：" + '；'.join(row['stream_audit_issues']), ""])
        elif row.get('usage_status') == 'unverified':
            lines.extend(["完整性未核验：持久化会话历史尚不可用。", ""])
        lines.extend(["| 角色 | 输入 (M) | 输出 (M) | 合计 (M) |", "| --- | ---: | ---: | ---: |"])
        for role, usage in row.get("by_role", {}).items():
            label = str(role).replace("|", "／").replace("\n", " ")
            numbers = " | ".join(f"{usage[key] / 1_000_000:.6f}" for key in ("input_tokens", "output_tokens", "total_tokens"))
            lines.append(f"| {label} | {numbers} |")
        for role in row.get('roles_without_reported_usage', []):
            label = str(role).replace('|', '／').replace('\n', ' ')
            lines.append(f"| {label} | — | — | — |")
    temporary = output / ".benchmark.md.tmp"
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(output / "benchmark.md")


COMPARISON_FIELDS = ["solution_run", "random_seed", "attempts", "total_tokens_M",
                     "elapsed_s", "coach_corrections", "final_score", "final_score_withdrawn"]


def _comparison_row(output: Path, data: dict) -> dict:
    summary = data["summary"]
    total = summary.get("reported_total_tokens")
    return {"solution_run": output.name, "random_seed": data.get("random_seed", ""),
            "attempts": summary["attempts"],
            "total_tokens_M": f"{total / 1_000_000:.6f}" if total is not None else "",
            "elapsed_s": f"{summary['elapsed_s']:.3f}",
            "coach_corrections": summary["coach_corrections"],
            "final_score": summary.get("final_score"),
            "final_score_withdrawn": summary.get("final_score_withdrawn")}


def _read_comparison(root: Path) -> list[dict]:
    path = root / "comparison.csv"
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [{key: row.get(key, "") for key in COMPARISON_FIELDS}
                for row in csv.DictReader(handle) if row.get("solution_run")]


def _write_comparison(root: Path, rows: list[dict]) -> list[dict]:
    rows = sorted(rows, key=lambda row: row["solution_run"])
    temporary = root / ".comparison.csv.tmp"
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COMPARISON_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(root / "comparison.csv")
    return rows


def _update_comparison(root: Path, output: Path, data: dict) -> list[dict]:
    """Update only this run; comparison.csv is the ranking source of truth."""
    current = _comparison_row(output, data)
    rows = [row for row in _read_comparison(root)
            if row["solution_run"] != current["solution_run"]]
    rows.append(current)
    return _write_comparison(root, rows)


@contextmanager
def ledger(output: Path):
    """Store restart-safe accounting privately and publish the two summaries."""
    output.mkdir(parents=True, exist_ok=True)
    db = _open_state(output)
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT document FROM benchmarks WHERE output=?", (str(output.resolve()),)).fetchone()
            data = json.loads(row[0]) if row else read_ledger(output) or (
                _import_markdown(output) if (output / "benchmark.md").is_file()
                else {"version": 2, "attempts": [], "corrections": []})
            data["token_source"] = {"producer": "jiuwen_game_player", "includes": "all reporting roles", "excludes": "external coach tokens"}
            yield data
            _summarize(data)
            db.execute("INSERT OR REPLACE INTO benchmarks VALUES (?, ?)", (str(output.resolve()), json.dumps(data, ensure_ascii=False)))
            # Publish under the same transaction lock so concurrent workers
            # cannot replace a newer report or comparison with stale data.
            _write_markdown(output, data)
            write_career_log(output, data)
            _update_comparison(output.parent, output, data)
    finally:
        db.close()


def begin(output: Path, **metadata) -> int:
    with ledger(output) as data:
        attempt = len(data["attempts"]) + 1
        data["attempts"].append({"attempt": attempt, "started_at": now(), "elapsed_s": None,
                                 "token_usage": None, "usage_status": "unavailable", **metadata})
    return attempt


def finish(output: Path, attempt: int, **metadata) -> dict:
    with ledger(output) as data:
        row = data["attempts"][attempt - 1]
        if "elapsed_s" in metadata:
            metadata["elapsed_s"] = max(row.get("elapsed_s") or 0, metadata["elapsed_s"])
        row.update(metadata)
    return dict(row)


def refresh(output: Path, events: Path, *, stopped: bool = False) -> None:
    """Recompute from immutable usage evidence, so repeated refreshes never double count."""
    if not read_ledger(output):
        return
    context_roles = {"leader"}
    try:
        manifest = json.loads((output / "solution/manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    if isinstance(manifest, dict) and manifest.get("mode") == "agent":
        context_roles.update({"agent", "unknown"})
    with ledger(output) as data:
        rows = data["attempts"]
        totals = [TokenUsage() for _ in rows]
        counts = [0 for _ in rows]
        roles: list[dict[str, dict[str, int]]] = [{} for _ in rows]
        observed_roles: list[set[str]] = [set() for _ in rows]
        leader_context = [None for _ in rows]
        activity = [None for _ in rows]
        collected_calls: list[set[str]] = [set() for _ in rows]
        issues: list[list[str]] = [[] for _ in rows]
        sources = data.setdefault("event_logs", [])
        if events.is_file() and str(events) not in sources:
            sources.append(str(events))
        for source in sources:
            if not Path(source).is_file():
                continue
            with Path(source).open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # A live writer may not have completed the last line.
                    stamp = event.get("ts", "")
                    idx = next((i for i in range(len(rows) - 1, -1, -1) if rows[i].get("started_at") and rows[i]["started_at"] <= stamp), None)
                    if event.get('agent_session_id'):
                        idx = next((i for i in range(len(rows) - 1, -1, -1)
                                    if rows[i].get('agent_session_id') == event['agent_session_id']
                                    and rows[i].get('started_at', '\uffff') <= stamp
                                    and (not event.get('attempt') or rows[i]['attempt'] == event['attempt'])), None)
                    if idx is None:
                        continue
                    expected = rows[idx].get('stream_request_id')
                    if event.get('kind') == 'stream_mismatch' or (expected and event.get('request_id') != expected):
                        if '会话请求不匹配' not in issues[idx]:
                            issues[idx].append('会话请求不匹配')
                        continue
                    node = event.get("payload") or {}
                    actor = event.get("actor") or node.get("member_name") or node.get("role")
                    if actor and actor != "unknown":
                        observed_roles[idx].add(actor)
                    activity[idx] = max(activity[idx] or stamp, stamp)
                    if event.get('kind') == 'tool_call':
                        tool_id = (node.get('tool_call') or {}).get('tool_call_id')
                        if tool_id:
                            collected_calls[idx].add(tool_id)
                    if event.get("kind") != "usage":
                        continue
                    usage = event.get("usage") or {}
                    bucket = totals[idx].by_model.setdefault(event.get("model") or "unknown", {})
                    for key in ("input_tokens", "output_tokens", "total_tokens", "total_cost"):
                        value = usage.get(key, 0) or 0
                        setattr(totals[idx], key, getattr(totals[idx], key) + value)
                        bucket[key] = bucket.get(key, 0) + value
                    role = event.get("actor") or "unknown"
                    if role in context_roles and isinstance(usage.get("input_tokens"), int):
                        # Jiuwen reports the exact input context on every model
                        # call. The last primary-agent call is its current
                        # context size; do not sum repeated prompts.
                        leader_context[idx] = usage["input_tokens"]
                    role_usage = roles[idx].setdefault(role, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
                    for key in role_usage:
                        role_usage[key] += usage.get(key, 0) or 0
                    counts[idx] += 1
        histories = {session: history_calls(session) for session in {r.get('agent_session_id', '') for r in rows} if session}
        # History timestamps precede receipt timestamps. A call received across
        # an attempt boundary is not a lost call; compare within its session.
        session_calls: dict[str, set[str]] = {}
        for i, row in enumerate(rows):
            session_calls.setdefault(row.get('agent_session_id', ''), set()).update(collected_calls[i])
        for i, row in enumerate(rows):
            calls = histories.get(row.get('agent_session_id'))
            if calls is None and row.get('history_sealed'):
                # Explicit session deletion must not erase the audit performed
                # after its producer stopped. Keep its verified/partial result.
                observed_roles[i].update(row.get('observed_roles') or [])
                issues[i].extend(row.get('stream_audit_issues') or [])
                continue
            row['history_audit_status'] = 'verified' if calls is not None else 'unavailable'
            start = row.get('started_at', '\uffff')
            end = rows[i + 1].get('started_at', '\uffff') if i + 1 < len(rows) else '\uffff'
            missing = []
            for call in calls or ():
                if not start <= call['ts'] < end:
                    continue
                role = call['role']
                if role and role not in {'unknown', 'assistant', 'teammate'}:
                    observed_roles[i].add(role)
                if call['tool_call_id'] and call['tool_call_id'] not in session_calls[row.get('agent_session_id', '')]:
                    missing.append(call['tool_call_id'])
                if row.get('stream_request_id') and call['request_id'] != row['stream_request_id']:
                    if '会话请求不匹配' not in issues[i]:
                        issues[i].append('会话请求不匹配')
            row['missing_history_tool_call_ids'] = missing
            if missing:
                issues[i].append(f'持久化历史中 {len(missing)} 条工具调用未进入采集流')
            row['stream_audit_issues'] = issues[i]
        previous_leader_context = None
        for i, row in enumerate(rows):
            if not row.get("started_at"):
                continue  # Historical Markdown imports have no receipt window.
            row["token_usage"] = totals[i].to_dict() if counts[i] else None
            row["usage_reports"] = counts[i]
            row["by_role"] = roles[i]
            row["observed_roles"] = sorted(observed_roles[i])
            row["roles_without_reported_usage"] = sorted(observed_roles[i] - roles[i].keys())
            row["leader_context_tokens"] = leader_context[i]
            row["leader_context_delta_tokens"] = (
                leader_context[i] - previous_leader_context
                if leader_context[i] is not None and previous_leader_context is not None
                else leader_context[i]
            )
            if leader_context[i] is not None:
                previous_leader_context = leader_context[i]
            if activity[i] and row.get("phase", "decision") == "decision":
                row["last_activity_at"] = activity[i]
                wall_s = (datetime.fromisoformat(activity[i]) - datetime.fromisoformat(row["started_at"])).total_seconds()
                row["elapsed_s"] = max(row.get("elapsed_s") or 0, wall_s)
                row["post_action_elapsed_s"] = max(0, row["elapsed_s"] - (row.get("drive_elapsed_s") or row["elapsed_s"]))
            row["usage_status"] = ("reported" if stopped or i < len(rows) - 1 else "provisional") if counts[i] else "unavailable"
            if counts[i] and row["roles_without_reported_usage"]:
                row["usage_status"] = "partial_roles"
            elif counts[i] and row.get('agent_session_id') and row['history_audit_status'] == 'unavailable':
                row['usage_status'] = 'unverified'
            if issues[i]:
                row['usage_status'] = 'partial_stream'
        data["events_log"] = str(events)
        data["updated_at"] = now()



def seal_agent_history(output: Path, events: Path, agent_session_id: str) -> None:
    """Retain the stopped stream's audit in the existing private ledger before deletion."""
    refresh(output, events, stopped=True)
    with ledger(output) as document:
        for row in document["attempts"]:
            if row.get("agent_session_id") == agent_session_id:
                row["history_sealed"] = True


def render_events(events: Path, output: Path) -> None:
    if events.is_file():
        output.mkdir(parents=True, exist_ok=True)
        target = output / "events.md"
        temp = target.with_suffix(".md.tmp")
        temp.write_text(build_markdown(events, DEFAULT_WIDTH, DEFAULT_FOLD_LINES), encoding="utf-8")
        temp.replace(target)


def mark_withdrawn(output: Path, game_session_id: str, round_number: int) -> None:
    if not read_ledger(output):
        return
    with ledger(output) as data:
        for row in data["attempts"]:
            if row.get("game_session_id") == game_session_id and row.get("round", 0) >= round_number:
                row.setdefault("withdrawn_at", now())


def record_correction(output: Path, *, game_session_id: str, round_number: int,
                      reason: str, kind: str = "policy", source: str = "manual", operation_id: str = "") -> dict:
    if kind not in {"policy", "infrastructure", "experiment"}:
        raise ValueError("Unknown correction kind")
    with ledger(output) as data:
        corrections = data.setdefault("corrections", [])
        if operation_id:
            existing = next((r for r in corrections if r.get("operation_id") == operation_id), None)
            if existing is not None:
                return existing
        entry = {"id": len(corrections) + 1, "created_at": now(), "game_session_id": game_session_id,
                 "round": round_number, "kind": kind, "reason": reason, "source": source,
                 "attempt_ids": [r["attempt"] for r in data["attempts"]
                                 if r.get("game_session_id") == game_session_id and r.get("round") == round_number]}
        if operation_id:
            entry["operation_id"] = operation_id
        corrections.append(entry)
    return entry


def _import_markdown(output: Path) -> dict:
    """Import an existing curated report without recreating public JSON/CSV."""
    text = (output / "benchmark.md").read_text(encoding="utf-8")
    rows: list[dict[str, Any]] = []
    current_role_row: dict[str, Any] | None = None
    role_index = 0
    for line in text.splitlines():
        if line.startswith("### 事件"):
            current_role_row = rows[role_index] if role_index < len(rows) else None
            role_index += 1
            continue
        if not line.startswith("| ") or line.startswith("| ---"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) in {5, 7} and cells[1].endswith(" M"):
            elapsed_index = 3 if len(cells) == 5 else 5
            if not cells[elapsed_index].endswith(" s"):
                continue
            total = round(float(cells[1][:-2]) * 1_000_000)
            rows.append({"attempt": len(rows) + 1, "round": len(rows) + 1, "shorttitle": cells[0],
                         "phase": "decision", "action_success": True,
                         "elapsed_s": float(cells[elapsed_index][:-2]), "token_usage": {"total_tokens": total}, "by_role": {}})
            if len(cells) == 7:
                rows[-1]["leader_context_delta_tokens"] = (
                    round(float(cells[3][:-2]) * 1_000_000) if cells[3].endswith(" M") else None)
                rows[-1]["leader_context_tokens"] = (
                    round(float(cells[4][:-2]) * 1_000_000) if cells[4].endswith(" M") else None)
        elif len(cells) == 4 and current_role_row is not None:
            try:
                counts = [round(float(c) * 1_000_000) for c in cells[1:]]
            except ValueError:
                continue
            current_role_row["by_role"][cells[0]] = dict(zip(("input_tokens", "output_tokens", "total_tokens"), counts))
    for row in rows:
        if row["by_role"]:
            row["token_usage"] = {k: sum(v[k] for v in row["by_role"].values())
                                  for k in ("input_tokens", "output_tokens", "total_tokens")}
    match = re.search(r"Coach 纠正：[ *]*(\d+)", text)
    count = int(match[1]) if match else 0
    data = {"version": 2, "attempts": rows, "corrections": [
        {"id": i + 1, "kind": "policy", "reason": "Imported from existing report"} for i in range(count)]}
    _summarize(data)
    return data


def compare(root: Path) -> list[dict]:
    """Refresh only CSV-retained runs; a deleted ranking row stays deleted."""
    root.mkdir(parents=True, exist_ok=True)
    previous = _read_comparison(root)
    names = ({row["solution_run"] for row in previous} if (root / "comparison.csv").is_file()
             else {path.parent.name for path in root.glob("*/benchmark.md")})
    old_by_name = {row["solution_run"]: row for row in previous}
    rows = []
    for name in sorted(names):
        output = root / name
        if not (output / "benchmark.md").is_file():
            if name in old_by_name:
                rows.append(old_by_name[name])
            continue
        data = read_ledger(output) or _import_markdown(output)
        # Older summaries cleared scores on withdraw; recover from attempt evidence.
        if any(r.get("final_score") is not None for r in data["attempts"]):
            data["summary"].update(_ending_summary(data))
        old = old_by_name.get(name, {})
        data.setdefault("random_seed", old.get("random_seed", ""))
        if old.get("elapsed_s"):
            data["summary"]["elapsed_s"] = float(old["elapsed_s"])
        rows.append(_comparison_row(output, data))
    return _write_comparison(root, rows)


def terminal_score(observation: dict) -> float | None:
    """Use the emulator's quantitative ending score only at a verified ending."""
    score = observation.get("ending_score")
    if not isinstance(score, dict):
        return None
    flags = (observation.get("current_state") or {}).get("simulation_flags") or {}
    complete = score.get("outcome") == "completed" and score.get("completed") is True and score.get("survival_months", 0) >= 48
    eliminated = score.get("outcome") == "eliminated" and (flags.get("failed") is True or flags.get("alive") is False)
    value = score.get("quantitative_score")
    if (complete or eliminated) and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return None
