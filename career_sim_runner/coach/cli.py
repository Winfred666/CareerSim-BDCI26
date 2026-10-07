#!/usr/bin/env python3
"""Coach-only controls for interactive or uninterrupted CareerSim runs.

This module is intentionally not registered with the MCP server. JiuwenSwarm
cannot see these commands as tools; a coach invokes the module separately.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import shutil
import sys
import time
import uuid
from argparse import Namespace
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from career_sim_runner.coach import benchmark
from career_sim_runner.coach.benchmark import allocate_snapshot, snapshot_solution
from career_sim_runner.coach.driver import drive as drive_gated
from career_sim_runner.coach.driver import retire as retire_gated
from career_sim_runner.coach.history import read_history, observe_boundary, legacy_boundary, pruned_history_boundary
from career_sim_runner.jiuwen_rewind import atomic_json
from career_sim_runner.coach.notebooks import verify_notebooks
from career_sim_runner.coach.gate import Gate, gate_path
from career_sim_runner.coach.notebooks import (
    NotebookSnapshot, align_notebook_snapshot, capture_notebooks, restore_notebooks, runtime_skills_dir,
)
from career_sim_runner.coach.log import (
    COACH_STEP_PREFIX,
    append_coach_step_log,
    read_coach_step_logs,
    read_session_log_tail,
    session_log_path,
)
from career_sim_runner.coach.seed import DEFAULT_BENCHMARK_SEED, configure_sampling_seed
from career_sim_runner.constants import DEFAULT_TIMEOUT_S, REPO_ROOT
from career_sim_runner.db import read_session_payload
from career_sim_runner.headless_play import (
    load_last_drive_session_id,
    new_drive_session_id,
    store_last_drive_session_id,
)
from career_sim_runner.install import install_submission, load_active_install
from career_sim_runner.emulator_adapter import rewind_game_session
from career_sim_runner.paths import (
    default_db_path,
    default_emulator_log_dir,
    default_output_root,
    ensure_runtime_dirs,
    jiuwenswarm_data_dir,
    timestamped_output_dir,
)
from career_sim_runner.setup import ensure_instance_configured, resolve_instance_ws_url
from career_sim_runner.ws_client import (
    cancel_agent_session,
    pause_agent_session,
    reload_agent_config,
    resolve_run_mode,
    rewind_agent_session,
)


STATE_FILENAME = "coach_state.json"
HISTORY_DIRNAME = "coach_checkpoints"
LEGACY_HISTORY_DIRNAME = "coach_history"
SESSION_REGISTRY_FILENAME = "coach_sessions.json"
CONTEXT_HISTORY_MAX_BYTES = 8 * 1024 * 1024
RETRYABLE_STEP_TERMINATIONS: frozenset[str] = frozenset()  # Recovery is explicit, never a hidden fresh conversation.


def _player_session_fields(session_id: str) -> dict[str, str]:
    """Expose the Jiuwen player session name with a legacy compatibility key."""
    return {
        "jiuwen_player_session_id": session_id,
        # Keep existing integrations working while they migrate.
        "session_id": session_id,
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_hash(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _only_observation_consumed(expected: dict[str, Any], current: dict[str, Any]) -> bool:
    """Allow observe to consume one-shot text without treating it as a new action."""
    if not isinstance(expected, dict) or not isinstance(current, dict):
        return False
    before, after = copy.deepcopy(expected), copy.deepcopy(current)
    consumed = False
    if before.get("pending_observe_warning") and after.get("pending_observe_warning") == "":
        before["pending_observe_warning"] = ""
        consumed = True
    before_buffer = (before.get("career") or {}).get("event_buffer")
    after_buffer = (after.get("career") or {}).get("event_buffer")
    if isinstance(before_buffer, dict) and isinstance(after_buffer, dict):
        if "mercy" in before_buffer and "mercy" not in after_buffer:
            before_buffer.pop("mercy")
            consumed = True
    return consumed and before == after


def _event_seed(session_id: str, payload: dict[str, Any], random_seed: str = "") -> str:
    """Return Career Emulator's deterministic monthly event sampling seed."""
    career = payload.get("career")
    if not isinstance(career, dict):
        raise RuntimeError("Game payload has no career state; cannot derive event seed.")
    month = career.get("current_month")
    if not isinstance(month, int):
        raise RuntimeError("Game payload has no current month; cannot derive event seed.")
    # career-emulator seeds its monthly event sampler with this exact value.
    return f"{random_seed or session_id}:{month}"


def _state_path(db_path: Path) -> Path:
    return db_path.parent / STATE_FILENAME


def _registry_path(db_path: Path) -> Path:
    return db_path.parent / SESSION_REGISTRY_FILENAME


def _history_path(db_path: Path, session_id: str) -> Path:
    return db_path.parent / HISTORY_DIRNAME / f"{session_id}.json"


def _coach_output_dir() -> Path:
    """Return a collision-resistant directory for one coach request."""
    stamp = datetime.now().astimezone().strftime("%m%d%H%M")
    output = default_output_root() / "coach" / f"{stamp}-solution"
    if output.exists():
        output = output.with_name(f"{output.name}-{uuid.uuid4().hex[:4]}")
    return output


def _load_state(db_path: Path) -> dict[str, str]:
    path = _state_path(db_path)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(key): str(value) for key, value in payload.items() if value is not None}


def _save_state(db_path: Path, **values: str) -> None:
    path = _state_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _load_state(db_path)
    for key, value in values.items():
        if value:
            payload[key] = value
        else:
            payload.pop(key, None)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _coach_emulator_log_dir(db_path: Path) -> Path:
    """Return this coach game's stable, participant-facing log directory."""
    configured = _load_state(db_path).get("emulator_log_dir", "").strip()
    return Path(configured).expanduser().resolve() if configured else default_emulator_log_dir()


def _ensure_coach_emulator_log_dir(
    db_path: Path,
    *,
    game_session_id: str = "",
    fresh: bool = False,
) -> tuple[Path, bool]:
    """Allocate one timestamped submission output directory per coach game.

    Existing games are migrated by copying their legacy cumulative log.  The
    old file is retained so this path change never destroys debugging evidence.
    """
    current = _load_state(db_path).get("emulator_log_dir", "").strip()
    if current and not fresh:
        return Path(current).expanduser().resolve(), False

    install_record = load_active_install()
    if install_record is None:
        return default_emulator_log_dir(), False
    log_dir = timestamped_output_dir(install_record.submission_name)
    if log_dir.exists():
        log_dir = log_dir.with_name(f"{log_dir.name}-{uuid.uuid4().hex[:8]}")
    log_dir.mkdir(parents=True, exist_ok=True)

    if game_session_id:
        legacy_log = session_log_path(default_emulator_log_dir(), game_session_id)
        migrated_log = session_log_path(log_dir, game_session_id)
        if legacy_log.is_file() and not migrated_log.exists():
            shutil.copy2(legacy_log, migrated_log)

    _save_state(db_path, emulator_log_dir=str(log_dir))
    return log_dir, True


def _load_registry(db_path: Path) -> dict[str, dict[str, str]]:
    path = _registry_path(db_path)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): {str(field): str(value) for field, value in value.items()}
        for key, value in payload.items()
        if isinstance(value, dict)
    }


def _save_registry(db_path: Path, registry: dict[str, dict[str, str]]) -> None:
    path = _registry_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _register_coach_session(
    db_path: Path,
    session_id: str,
    game_session_id: str,
    agent_session_id: str,
    seed: str,
    context_bootstrap: bool = False,
) -> None:
    registry = _load_registry(db_path)
    registry[session_id] = {
        "game_session_id": game_session_id,
        "agent_session_id": agent_session_id,
        "seed": seed,
        "context_bootstrap": "1" if context_bootstrap else "0",
        "updated_at": _utc_now(),
    }
    _save_registry(db_path, registry)


def _replace_coach_session_id(db_path: Path, old_id: str, new_id: str, **values: str) -> None:
    registry = _load_registry(db_path)
    record = registry.pop(old_id, {})
    record.update({key: value for key, value in values.items() if value})
    record["updated_at"] = _utc_now()
    registry[new_id] = record
    _save_registry(db_path, registry)


@dataclass
class Checkpoint:
    """One reversible coach step."""

    session_id: str
    captured_at: str
    before_payload: dict[str, Any] | None
    before_logs: list[dict[str, str]]
    after_hash: str
    drive_session_id: str
    decision: dict[str, Any] = field(default_factory=dict)
    state_transition: dict[str, Any] = field(default_factory=dict)
    seed: str = ""
    # Legacy diagnostic only; withdrawal uses the first observe tool boundary.
    jiuwen_turn_index: int = 0
    observe_boundary: dict[str, Any] | None = None
    # Incremental state change from ``before_payload`` to the post-action
    # payload.  Older JSONL checkpoints omit this and remain readable.
    payload_delta: list[dict[str, Any]] = field(default_factory=list)
    before_notebooks: NotebookSnapshot | None = field(default_factory=dict)


def _payload_delta(before: Any, after: Any, path: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Return a compact, replayable patch for two JSON-compatible payloads."""
    if isinstance(before, dict) and isinstance(after, dict):
        ops: list[dict[str, Any]] = []
        for key in sorted(set(before) | set(after)):
            child = path + (str(key),)
            if key not in after:
                ops.append({"op": "remove", "path": list(child)})
            elif key not in before:
                ops.append({"op": "set", "path": list(child), "value": after[key]})
            else:
                ops.extend(_payload_delta(before[key], after[key], child))
        return ops
    if before != after:
        return [{"op": "set", "path": list(path), "value": after}]
    return []


def _apply_payload_delta(payload: Any, delta: list[dict[str, Any]]) -> Any:
    """Apply a delta generated by :func:`_payload_delta`."""
    import copy

    result = copy.deepcopy(payload)
    for operation in delta:
        path = [str(part) for part in operation.get("path", [])]
        if not path:
            result = copy.deepcopy(operation.get("value"))
            continue
        parent = result
        for part in path[:-1]:
            if not isinstance(parent, dict):
                break
            parent = parent.setdefault(part, {})
        else:
            if not isinstance(parent, dict):
                continue
            leaf = path[-1]
            if operation.get("op") == "remove":
                parent.pop(leaf, None)
            else:
                parent[leaf] = copy.deepcopy(operation.get("value"))
    return result


class CheckpointStore:
    """Single-file incremental checkpoint ledger with arbitrary withdrawal."""

    def __init__(self, db_path: Path, session_id: str) -> None:
        self.path = _history_path(db_path, session_id)
        self.legacy_path: Path | None
        legacy = db_path.parent / LEGACY_HISTORY_DIRNAME / f"{session_id}.jsonl"
        if not self.path.exists() and legacy.exists():
            # Read existing runs in place; the next write migrates them to the
            # single-file checkpoint directory.
            self.legacy_path = legacy
        else:
            self.legacy_path = None

    def _document(self) -> dict[str, Any]:
        source = self.path if self.path.is_file() else self.legacy_path
        if source is None or not source.is_file():
            return {"version": 2, "base_payload": None, "entries": []}
        text = source.read_text(encoding="utf-8")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            entries = []
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    entries.append(item)
            base = entries[0].get("before_payload") if entries else None
            return {"version": 1, "base_payload": base, "entries": entries}
        if isinstance(parsed, dict) and isinstance(parsed.get("entries"), list):
            return parsed
        return {"version": 2, "base_payload": None, "entries": []}

    def _save_document(self, document: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.path)
        if self.legacy_path and self.legacy_path.exists():
            self.legacy_path.unlink()

    def append(self, checkpoint: Checkpoint) -> None:
        document = self._document()
        entries = list(document.get("entries") or [])
        item = asdict(checkpoint)
        if entries:
            previous_notebooks = None
            for previous in entries:
                previous_notebooks = (
                    _apply_payload_delta(previous_notebooks, previous["notebooks_delta"])
                    if "notebooks_delta" in previous else previous.get("before_notebooks")
                )
            item["notebooks_delta"] = _payload_delta(previous_notebooks, checkpoint.before_notebooks)
            item.pop("before_notebooks")
        if not entries:
            document["base_payload"] = checkpoint.before_payload
        # Keep the first snapshot as the reconstruction base. Subsequent
        # entries carry only the state shift when available.
        if entries and checkpoint.payload_delta:
            item.pop("before_payload", None)
        document["version"] = 2
        entries.append(item)
        document["entries"] = entries
        self._save_document(document)

    def records(self) -> list[dict[str, Any]]:
        """Return checkpoint records, materializing each pre-action payload."""
        document = self._document()
        records: list[dict[str, Any]] = []
        payload = document.get("base_payload")
        notebooks = None
        for raw in document.get("entries") or []:
            if not isinstance(raw, dict):
                continue
            record = dict(raw)
            if "notebooks_delta" in record:
                notebooks = _apply_payload_delta(notebooks, record["notebooks_delta"])
            else:
                notebooks = record.get("before_notebooks")
            record["before_notebooks"] = notebooks
            if "before_payload" not in record:
                record["before_payload"] = payload
            records.append(record)
            if record.get("payload_delta"):
                payload = _apply_payload_delta(payload, record["payload_delta"])
        return records

    def recent(self, count: int = 5) -> list[dict[str, Any]]:
        """Return the most recent raw checkpoint records for coach inspection."""
        if count <= 0:
            return []
        return self.records()[-count:]

    def pop(self) -> Checkpoint | None:
        return self.withdraw()

    def withdraw(self, round_number: int | None = None) -> Checkpoint | None:
        """Remove and return a checkpoint, defaulting to the newest one."""
        records = self.records()
        if not records:
            return None
        index = len(records) - 1 if round_number is None else round_number - 1
        if index < 0 or index >= len(records):
            raise ValueError(f"checkpoint round must be between 1 and {len(records)}")
        record = records[index]
        document = self._document()
        document["entries"] = (document.get("entries") or [])[:index]
        if document["entries"]:
            document["version"] = 2
            self._save_document(document)
        elif self.path.exists():
            self.path.unlink()
        elif self.legacy_path and self.legacy_path.exists():
            self.legacy_path.unlink()
        return Checkpoint(
            session_id=str(record["session_id"]),
            captured_at=str(record["captured_at"]),
            before_payload=record.get("before_payload"),
            before_logs=[dict(item) for item in record.get("before_logs", [])],
            after_hash=str(record["after_hash"]),
            drive_session_id=str(record.get("drive_session_id", "")),
            decision=dict(record.get("decision", {})),
            state_transition=dict(record.get("state_transition", {})),
            seed=str(record.get("seed", "")),
            jiuwen_turn_index=int(record.get("jiuwen_turn_index", 0) or 0),
            payload_delta=list(record.get("payload_delta", [])),
            before_notebooks=record.get("before_notebooks"),
            observe_boundary=record.get("observe_boundary"),
        )

    def rebind_drive_session(self, drive_session_id: str) -> None:
        """Point retained checkpoints at the newly reloaded agent session."""
        document = self._document()
        entries = document.get("entries") or []
        if not entries:
            return
        for record in entries:
            if isinstance(record, dict):
                record["drive_session_id"] = drive_session_id
        self._save_document(document)


def _resolve_solution_dir(requested: str | None) -> Path:
    """Resolve the solution to install during a context reload."""
    path = Path(requested).expanduser().resolve() if requested and requested.strip() else REPO_ROOT / "solution"
    if not path.is_dir() or not (path / "manifest.json").is_file():
        raise RuntimeError(f"Solution directory with manifest.json not found: {path}")
    return path


async def _session_logs(db_path: Path, session_id: str) -> list[dict[str, str]]:
    from career_emulator.storage import SessionStore

    store = SessionStore(db_path, _coach_emulator_log_dir(db_path))
    return [entry.to_dict() for entry in await store.load_logs(session_id)]


async def _peek(db_path: Path, session_id: str, log_count: int = 10) -> dict[str, Any]:
    """Read the coach-visible state without using or changing the MCP server."""
    from career_emulator.game import GameEngine
    from career_emulator.storage import SessionStore

    class PreviewStore(SessionStore):
        async def save_session(self, session):
            pass

        async def append_log(self, entry):
            pass

    engine = GameEngine(db_path=db_path)
    # The MCP process is configured with the benchmark-owned log directory;
    # mirror it here so an observation that emits a warning remains reversible.
    # Observe can clear feedback, complete nodes and write ending scores.
    # Preview those changes on its loaded object without persisting any of them.
    engine.store = PreviewStore(db_path, _coach_emulator_log_dir(db_path))
    await configure_sampling_seed(engine, _load_state(db_path).get("random_seed", ""))
    observation = await engine.observe(session_id)
    logs = await engine.latest_logs(session_id, log_count)
    return {
        "session_id": session_id,
        "observation": observation.to_dict(),
        "logs": logs,
    }


def _value_diff(before: Any, after: Any) -> Any:
    """Return a compact recursive diff for a coach-visible state."""
    if isinstance(before, dict) and isinstance(after, dict):
        changed: dict[str, Any] = {}
        for key in sorted(set(before) | set(after)):
            diff = _value_diff(before.get(key), after.get(key))
            if diff is not None:
                changed[key] = diff
        return changed or None
    if before != after:
        return {"before": before, "after": after}
    return None


def _state_transition(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Describe the exact visible state migration caused by one choice."""
    return {
        "before": before,
        "after": after,
        "changed": _value_diff(before, after) or {},
    }


def _question_from_observation(observation: dict[str, Any] | None) -> dict[str, Any]:
    """Build the complete coach-facing question from an observe payload."""
    observation = observation if isinstance(observation, dict) else {}
    current_state = observation.get("current_state")
    time_info = current_state.get("time", {}) if isinstance(current_state, dict) else {}
    return {
        "time": dict(time_info) if isinstance(time_info, dict) else {},
        "event": observation.get("current_event"),
        "choices": observation.get("choices") or [],
        "warning": observation.get("warning", ""),
        "ending_score": observation.get("ending_score"),
    }


def _decision_from_events(events_path: Path, before_status: dict[str, Any]) -> dict[str, Any]:
    """Recover the question and selected option from one coach event log."""
    from career_sim_runner.replay.parse import parse_events_log

    try:
        _session_id, turns, _misc = parse_events_log(events_path)
    except (OSError, ValueError, TypeError):
        turns = []
    turn = turns[-1] if turns else None
    observation = before_status.get("observation", {})
    event = observation.get("current_event") or {}
    choices = observation.get("choices") or []
    if not event and turn is not None:
        event = {
            "title": turn.observe.event_title,
            "description": turn.observe.event_description,
        }
    if not choices and turn is not None:
        choices = [
            {"choice": option.choice, "action": option.action, "description": option.description}
            for option in turn.observe.choices
        ]
    if turn is not None:
        event = event or {
            "title": turn.observe.event_title,
            "description": turn.observe.event_description,
        }
    choice_number = turn.choice if turn is not None else None
    selected = next(
        (choice for choice in choices if isinstance(choice, dict) and choice.get("choice") == choice_number),
        {},
    )
    return {
        "event": dict(event) if isinstance(event, dict) else {},
        "question": {
            "time": (observation.get("current_state") or {}).get("time") or ({
                "current_month": turn.observe.month,
                "current_quarter": turn.observe.quarter,
                "current_year": turn.observe.year,
            }
            if turn is not None
            else {}),
            "event": dict(event) if isinstance(event, dict) else {},
            "choices": choices,
            "ending_score": turn.observe.ending_score if turn is not None else None,
        },
        "choice": {
            "number": choice_number,
            "action": str(selected.get("action", turn.choice_action if turn else "")),
            "notes": str(turn.notes if turn else ""),
        },
    }


def _inspection_payload(
    *,
    session_id: str,
    game_session_id: str,
    agent_session_id: str,
    seed: str,
    observed: dict[str, Any],
    checkpoints: list[dict[str, Any]],
    recent_count: int,
    coach_records: list[dict[str, Any]] | None = None,
    log_file: Path | None = None,
    log_truncated: bool = False,
    selected_records: list[dict[str, Any]] | None = None,
    raw_log_lines: list[str] | None = None,
) -> dict[str, Any]:
    """Build the compact evidence view used by the coach command."""
    observation = observed.get("observation") or {}
    question = _question_from_observation(observation)
    # The live emulator observation is authoritative.  A step process can be
    # interrupted after Jiuwen commits an action but before the structured
    # coach record is appended; in that case the last ``next_question`` in the
    # log describes the previous event and must not mask the current state.
    live_event = question.get("event")
    live_choices = question.get("choices")
    if coach_records and not (isinstance(live_event, dict) and live_event.get("event_id") and live_choices):
        question = dict(coach_records[-1].get("next_question") or question)
    recent_decisions: list[dict[str, Any]] = []
    if coach_records:
        records_to_show = (
            selected_records
            if selected_records is not None
            else (coach_records[-recent_count:] if recent_count > 0 else [])
        )
        for record in records_to_show:
            raw_decision = record.get("decision") or {}
            display_decision = (
                {key: value for key, value in raw_decision.items() if key != "question"}
                if isinstance(raw_decision, dict)
                else {}
            )
            recent_decisions.append(
                {
                    "round": record.get("round"),
                    "captured_at": record.get("created_at", ""),
                    "question": record.get("question") or {},
                    "decision": display_decision,
                    "consequence": record.get("consequence") or {},
                    "state_after": record.get("state_after") or {},
                    "action_result": record.get("action_result") or {},
                }
            )
    else:
        for index, checkpoint in enumerate(checkpoints[-recent_count:] if recent_count > 0 else [], start=1):
            transition = checkpoint.get("state_transition") or {}
            raw_decision = checkpoint.get("decision") or {}
            display_decision = (
                {key: value for key, value in raw_decision.items() if key != "question"}
                if isinstance(raw_decision, dict)
                else {}
            )
            recent_decisions.append(
                {
                    "round": index,
                    "captured_at": checkpoint.get("captured_at", ""),
                    "question": raw_decision.get("question") if isinstance(raw_decision, dict) else {},
                    "decision": display_decision,
                    "consequence": {"changed": transition.get("changed") or {}},
                    "state_after": transition.get("after") or {},
                }
            )
    logs = raw_log_lines if raw_log_lines is not None else (observed.get("logs") or {}).get("entries", [])
    if raw_log_lines is not None:
        logs = [line for line in logs if not line.startswith(COACH_STEP_PREFIX)]
    payload = {
        "game_session_id": game_session_id,
        "agent_session_id": agent_session_id,
        "seed": seed,
        "current_question": question,
        "recent_decisions": recent_decisions,
        "recent_events": logs[-recent_count:] if recent_count > 0 else [],
        "withdraw_available": bool(checkpoints),
        "hint": "若决策信息量不够，可传入 -i -j 查看当前轮-i 到当前轮-j 的决策结果。",
    }
    if log_file is not None:
        payload["log_file"] = str(log_file)
        payload["log_truncated"] = log_truncated
    payload.update(_player_session_fields(session_id))
    return payload


async def _replace_session(
    db_path: Path,
    session_id: str,
    payload: dict[str, Any] | None,
    logs: list[dict[str, str]],
    coach_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper for the CareerSim controller rewind API."""
    return await rewind_game_session(
        db_path,
        _coach_emulator_log_dir(db_path),
        session_id,
        payload,
        logs,
        coach_records,
    )


async def _delete_session(db_path: Path, session_id: str) -> None:
    """Remove a game accidentally opened by a step prompt."""
    from career_emulator.storage import SessionStore

    store = SessionStore(db_path, _coach_emulator_log_dir(db_path))
    await store.delete_session(session_id)


def _context_history_records(db_path: Path, game_session_id: str) -> list[dict[str, Any]]:
    """Load all retained coach decisions for a fresh Jiuwen context."""
    log_path = session_log_path(_coach_emulator_log_dir(db_path), game_session_id)
    records = read_coach_step_logs(log_path, max_bytes=CONTEXT_HISTORY_MAX_BYTES)
    if records:
        return records

    # Older runs may have checkpoints but no structured coach log. Use the
    # checkpoint evidence as a best-effort context reconstruction.
    reconstructed: list[dict[str, Any]] = []
    for round_number, checkpoint in enumerate(CheckpointStore(db_path, game_session_id).records(), start=1):
        decision = checkpoint.get("decision") or {}
        transition = checkpoint.get("state_transition") or {}
        reconstructed.append(
            {
                "round": round_number,
                "created_at": checkpoint.get("captured_at", ""),
                "question": decision.get("question") or {"event": decision.get("event", {})},
                "decision": decision,
                "action_result": {},
                "consequence": transition,
                "state_after": transition.get("after") or {},
                "next_question": {},
            }
        )
    return reconstructed


def _rewrite_session_ids(value: Any, old_id: str, new_id: str) -> Any:
    """Rewrite an emulator session id everywhere in JSON-compatible context."""
    if not old_id or old_id == new_id:
        return value
    if isinstance(value, str):
        return value.replace(old_id, new_id)
    if isinstance(value, list):
        return [_rewrite_session_ids(item, old_id, new_id) for item in value]
    if isinstance(value, dict):
        return {
            str(_rewrite_session_ids(key, old_id, new_id)): _rewrite_session_ids(item, old_id, new_id)
            for key, item in value.items()
        }
    return value


async def _current_jiuwen_turn(ws_url: str, agent_session_id: str) -> int:
    """Count Jiuwen user turns from this instance's canonical history ledger.

    Jiuwen 0.2.4b3 declares ``history.list_turns`` but does not dispatch that
    RPC. Its ``session.rewind`` implementation counts the same ``role=user``
    records in history.jsonl, so read those records directly and read-only.
    ``ws_url`` stays in the signature to keep the checkpoint caller symmetric
    with the rewind RPC and easy to replace when Jiuwen exposes the method.
    """
    del ws_url
    session_dir = jiuwenswarm_data_dir() / "agent" / "sessions" / agent_session_id
    for filename in ("history.jsonl", "history.json"):
        path = session_dir / filename
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
            if filename.endswith(".jsonl"):
                records = [json.loads(line) for line in text.splitlines() if line.strip()]
            else:
                records = json.loads(text)
        except (OSError, json.JSONDecodeError, TypeError):
            return 0
        if not isinstance(records, list):
            return 0
        return sum(1 for record in records if isinstance(record, dict) and record.get("role") == "user")
    return 0


def _runtime_skills_dir(agent_session_id: str) -> Path | None:
    """Resolve the mutable skill workspace used by this Jiuwen session."""
    return runtime_skills_dir(agent_session_id, data_dir=jiuwenswarm_data_dir())


def _bind_installed_game_session(skill_root: Path, game_session_id: str) -> list[Path]:
    """Restore the host-owned game identity after a solution reinstall.

    Notebook snapshots have already been restored. Bind notebooks that
    explicitly declare ``session_id`` to the game owned by this controller.
    """
    rebound: list[Path] = []
    for path in sorted(skill_root.glob("*/notebooks/session.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or "session_id" not in payload:
            continue
        if payload["session_id"] == game_session_id:
            continue
        payload["session_id"] = game_session_id
        path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        rebound.append(path)
    return rebound


def _copy_keeps_notebooks(agent_session_id: str, output_dir: Path) -> list[Path]:
    """Export live keeps and measured caps for coach debugging, excluding translations."""
    skills_dir = _runtime_skills_dir(agent_session_id)
    if skills_dir is None:
        return []

    copied: list[Path] = []
    for notebooks_dir in sorted(skills_dir.glob("*/notebooks")):
        for source in sorted(notebooks_dir.rglob("*")):
            relative = source.relative_to(notebooks_dir)
            if (
                not source.is_file()
                or not (source.stem.endswith("keeps") or relative.as_posix() == "stat-cap.tsv")
                or source.stem == "translation-keeps"
                or "translation-keeps" in relative.parts
            ):
                continue
            target = output_dir / "notebooks" / notebooks_dir.parent.name / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(target)
    return copied


def _format_context_history(records: list[dict[str, Any]]) -> str:
    """Render retained decisions as explicit context for a new agent session."""
    if not records:
        return "【已保留的历史决策】\n没有可用的历史 coach 记录。请以当前 observe 结果为准。\n"

    entries: list[str] = []
    for record in records:
        entries.append(
            json.dumps(
                {
                    "round": record.get("round"),
                    "question": record.get("question") or {},
                    "decision": record.get("decision") or {},
                    "action_result": record.get("action_result") or {},
                    "consequence": record.get("consequence") or {},
                    "state_after": record.get("state_after") or {},
                    "next_question": record.get("next_question") or {},
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return (
        "【已保留且仍然有效的历史决策】\n"
        "下面是 withdraw 后仍保留的完整 coach 决策记录，按事件顺序排列。\n"
        "这些记录只用于恢复判断背景，不要重复执行其中任何 take_action。\n"
        "已被 withdraw 的事件不在此列表中；当前游戏状态和当前题目以 observe 为准。\n" + "\n".join(entries) + "\n"
    )


def _merge_system_feedback(*values: str) -> str:
    """Join distinct one-shot feedback fragments without rewriting them."""
    fragments: list[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in fragments:
            fragments.append(cleaned)
    return "\n\n".join(fragments)


def _step_prompt(
    session_id: str,
    seed: str = "",
    history_records: list[dict[str, Any]] | None = None,
    *,
    team_mode: bool = False,
    system_feedback: str = "",
) -> str:
    """Bootstrap only; later steps release a tool gate without sending messages."""
    return f"按已安装 solution 继续游戏，session_id={session_id}。"


def _start_prompt(*, team_mode: bool = False) -> str:
    return "按已安装 solution 开始并完成一局 CareerSim。"


def _resolve_mode() -> str:
    record = load_active_install()
    if record is None:
        return "agent"
    return resolve_run_mode(record)


async def _resolve_context(db_path: Path, requested: str | None) -> dict[str, str]:
    """Resolve a Jiuwen player session id to game and runtime ids."""
    from career_emulator.storage import SessionStore

    await SessionStore(db_path, _coach_emulator_log_dir(db_path)).initialize()
    session_id = (requested or "").strip()
    if not session_id:
        raise RuntimeError(
            "--jiuwen-player-session-id is required; use the jiuwen_player_session_id returned by start or reload."
        )
    registry = _load_registry(db_path)
    record = registry.get(session_id)
    if record and record.get("game_session_id") and record.get("agent_session_id"):
        return {
            "session_id": session_id,
            "game_session_id": record["game_session_id"],
            "agent_session_id": record["agent_session_id"],
            "seed": record.get("seed", ""),
            "context_bootstrap": record.get("context_bootstrap", "0"),
        }

    # Accept an explicit Career Emulator id for old coach state/checkpoints;
    # new runs always return and document the coach/agent id above.
    if await read_session_payload(db_path, session_id):
        state = _load_state(db_path)
        return {
            "session_id": session_id,
            "game_session_id": session_id,
            "agent_session_id": state.get("agent_session_id") or load_last_drive_session_id() or "",
            "seed": state.get("seed", ""),
            "context_bootstrap": "0",
        }
    raise RuntimeError(f"Unknown Jiuwen player session id: {session_id}")


def _resolve_agent_session(db_path: Path, requested: str | None, *, fresh: bool = False) -> str:
    if requested and requested.strip():
        return requested.strip()
    if fresh:
        return new_drive_session_id()
    state = _load_state(db_path)
    return state.get("agent_session_id") or load_last_drive_session_id() or new_drive_session_id()


async def _run_step(args: argparse.Namespace, *, start: bool = False) -> dict[str, Any]:
    command_deadline = time.monotonic() + args.timeout_s
    ensure_runtime_dirs()
    db_path = Path(args.db).resolve()
    run_mode = args.mode or _resolve_mode()
    drive_session_id = _resolve_agent_session(db_path, args.agent_session_id, fresh=True) if start else ""
    coach_session_id = drive_session_id
    before_status: dict[str, Any] = {}
    if start:
        before_payload = None
        before_logs: list[dict[str, str]] = []
        prompt = _start_prompt(team_mode=run_mode == "team")
        keyword = getattr(args, "solution_keyword", "") or _load_state(db_path).get("solution_keyword", "solution")
        install = load_active_install()
        output_dir = allocate_snapshot(default_output_root() / "coach",
                                       Path(install.submission_dir) if install else REPO_ROOT / "solution",
                                       keyword) if keyword else _coach_output_dir()
        random_seed = str(getattr(args, "random_seed", DEFAULT_BENCHMARK_SEED))
        _save_state(db_path, solution_keyword=keyword, solution_snapshot=str(output_dir / "solution"), random_seed=random_seed, context_mode="persistent")
        with benchmark.ledger(output_dir) as document:
            document["random_seed"] = random_seed
            document["seed_scope"] = "monthly event sampling; identical datasets and eligibility required; model outputs are not deterministic"

        emulator_log_dir, _ = _ensure_coach_emulator_log_dir(db_path, fresh=True)
    else:
        context = await _resolve_context(db_path, args.session_id)
        coach_session_id = args.session_id
        game_session_id = context["game_session_id"]
        emulator_log_dir, log_dir_changed = _ensure_coach_emulator_log_dir(
            db_path,
            game_session_id=game_session_id,
        )
        drive_session_id = context["agent_session_id"]
        # Inspection is a preview; the player consumes its own observe feedback.
        before_status = await _peek(db_path, game_session_id, log_count=1)
        before_payload = await read_session_payload(db_path, game_session_id)
        if not before_payload:
            raise RuntimeError(f"Unknown game session: {game_session_id}")
        before_logs = await _session_logs(db_path, game_session_id)
        seed = _event_seed(game_session_id, before_payload, _load_state(db_path).get("random_seed", ""))
        requested_seed = str(getattr(args, "seed", "") or "").strip()
        stored_seed = context.get("seed", "")
        expected_seed = requested_seed or stored_seed
        if expected_seed and expected_seed != seed:
            raise RuntimeError(f"Seed mismatch: expected {expected_seed}, current game state is {seed}.")
        prompt = _step_prompt(game_session_id)
        output_dir = Path(_load_state(db_path).get("benchmark_output_dir") or _coach_output_dir())

    _require_no_pending_withdraw(db_path, drive_session_id)
    control = Gate(gate_path(db_path, drive_session_id))
    if start and control.path.exists():
        raise RuntimeError("start requires a fresh conversation id; this one already has a gate")
    if not control.path.exists():
        if not start and context.get("context_bootstrap") != "1":
            raise RuntimeError("Legacy conversation has no execution gate; explicitly reload before stepping")
        control.create(
            "" if start else game_session_id, agent_session_id=drive_session_id,
            ws_url=args.ws_url or resolve_instance_ws_url(), mode=run_mode,
            prompt=prompt, log_dir=str(output_dir), existing_game=not start,
            random_seed=_load_state(db_path).get("random_seed", ""),
        )
        ensure_instance_configured(log_dir=emulator_log_dir, coach_gate=control.path, db_path=db_path)
        if not await reload_agent_config(args.ws_url or resolve_instance_ws_url()):
            control.revoke()
            raise RuntimeError("JiuwenSwarm rejected agent.reload_config.")
    elif not start and getattr(args, "resume_stopped", False):
        with control.transaction() as gate_state:
            if gate_state["worker_status"] == "stopped":
                if (gate_state["revoked"] or gate_state["busy"] or gate_state["permit"]
                        or gate_state["sequence"] != gate_state["acknowledged"]
                        or gate_state["session_id"] != game_session_id
                        or gate_state.get("agent_session_id") != drive_session_id):
                    raise RuntimeError("Stopped Player gate is not safe to resume")
                gate_state.update(worker_status="new", worker_pid=None,
                                  worker_error=None, prompt=prompt)
    store_last_drive_session_id(drive_session_id)
    actual_output = Path(_load_state(db_path).get("benchmark_output_dir") or control.read()["log_dir"])
    if start:
        actual_output = output_dir
    _save_state(db_path, benchmark_output_dir=str(actual_output))
    control.update(benchmark_output_dir=str(actual_output), notebook_checkpoints=True)
    if not (actual_output / "solution").is_dir():
        install = load_active_install()
        source = Path(install.submission_dir) if install else REPO_ROOT / "solution"
        snapshot_solution(source, actual_output)
    control.update(benchmark_context={
        "round": 1 if start else len(CheckpointStore(db_path, game_session_id).records()) + 1,
        "seed": "enrollment" if start else seed,
        "question": _question_from_observation(before_status.get("observation")),
        "phase": "decision",
        "solution_snapshot": _load_state(db_path).get("solution_snapshot", str(actual_output / "solution")),
    })
    result = await drive_gated(control, min(args.timeout_s, max(0.001, command_deadline - time.monotonic())))
    if not result.action_success:
        if result.termination_reason == "new_game_forbidden" and result.session_id:
            await _delete_session(db_path, result.session_id)
        live_inspection = None
        if not start:
            try:
                live_inspection = (await _inspect(args)).get("inspection")
            except (RuntimeError, OSError, ValueError):
                live_inspection = None
        return {
            "ok": False,
            **_player_session_fields(coach_session_id),
            "agent_session_id": drive_session_id,
            "drive_session_id": drive_session_id,
            "game_session_id": result.session_id,
            "termination_reason": result.termination_reason,
            "action_result": result.action_result,
            "attempts": 1,
            "error": control.read().get("worker_error"),
            "inspection": live_inspection,
            "benchmark": result.benchmark,
            "events_log": str(result.events_path),
            "transcript_log": str(result.transcript_path),
        }
    if start and not result.new_game_seen:
        return {
            "ok": False,
            **_player_session_fields(drive_session_id),
            "drive_session_id": drive_session_id,
            "game_session_id": result.session_id,
            "termination_reason": "new_game_missing",
            "action_result": result.action_result,
            "benchmark": result.benchmark,
            "events_log": str(result.events_path),
            "transcript_log": str(result.transcript_path),
        }

    resolved_game_session_id = result.session_id if start else game_session_id
    if not resolved_game_session_id:
        raise RuntimeError("The agent completed an action but no game session could be identified.")
    game_session_id = resolved_game_session_id
    if not start and result.session_id and result.session_id != game_session_id:
        raise RuntimeError(f"Agent acted on {result.session_id}, expected {game_session_id}.")

    # Preview the post-action observation without consuming feedback or writing
    # ending scores. The checkpoint hashes the actual committed game payload.
    status = await _peek(db_path, game_session_id)
    after_payload = await read_session_payload(db_path, game_session_id)
    if not after_payload:
        raise RuntimeError(f"Game session disappeared after action: {game_session_id}")
    decision = _decision_from_events(result.events_path, before_status)
    # The MCP gate is authoritative even before the WS tool-result frame arrives.
    committed = control.read()
    before_payload = committed.get("before_payload", before_payload)
    before_logs = committed.get("before_logs", before_logs)
    number = committed["choice"]
    selected: dict[str, Any] = next((c for c in (before_status.get("observation") or {}).get("choices", [])
                                     if c.get("choice") == number), {})
    decision["choice"] = {"number": number, "action": selected.get("action", ""),
                          "notes": committed["notes"]}
    before_state = (before_status.get("observation") or {}).get("current_state", {})
    after_state = (status.get("observation") or {}).get("current_state", {})
    transition = _state_transition(
        before_state if isinstance(before_state, dict) else {},
        after_state if isinstance(after_state, dict) else {},
    )
    checkpoint_seed = _event_seed(game_session_id, before_payload, _load_state(db_path).get("random_seed", "")) if before_payload else "enrollment"
    next_seed = _event_seed(game_session_id, after_payload, _load_state(db_path).get("random_seed", ""))
    jiuwen_turn_index = await _current_jiuwen_turn(
        args.ws_url or resolve_instance_ws_url(), drive_session_id
    )
    boundary = committed.get("observe_boundary")
    if boundary:
        try:
            boundary = observe_boundary(read_history(jiuwenswarm_data_dir(), drive_session_id), boundary)
        except (OSError, ValueError, RuntimeError):
            # The unique snapshot reference survives a delayed history writer.
            # Withdrawal must resolve it exactly after stopping the producer.
            pass
    CheckpointStore(db_path, game_session_id).append(
        Checkpoint(
            session_id=game_session_id,
            captured_at=_utc_now(),
            before_payload=before_payload,
            before_logs=before_logs,
            after_hash=_json_hash(after_payload),
            drive_session_id=drive_session_id,
            decision=decision,
            state_transition=transition,
            seed=checkpoint_seed,
            jiuwen_turn_index=jiuwen_turn_index,
            payload_delta=_payload_delta(before_payload, after_payload),
            before_notebooks=committed.get("before_notebooks"),
            observe_boundary=boundary,
        )
    )
    checkpoint_store = CheckpointStore(db_path, game_session_id)
    round_number = len(checkpoint_store.records())
    question_before = _question_from_observation(before_status.get("observation"))
    decision_question = decision.get("question") or {}
    if isinstance(decision_question, dict):
        for key in ("time", "event", "choices", "ending_score"):
            if not question_before.get(key) and decision_question.get(key):
                question_before[key] = decision_question[key]
    question_after = _question_from_observation(status.get("observation"))
    state_after_snapshot = dict(after_state) if isinstance(after_state, dict) else {}
    ending_score = (status.get("observation") or {}).get("ending_score")
    if ending_score is not None:
        state_after_snapshot["ending_score"] = ending_score
    decision["benchmark"] = result.benchmark
    log_path = append_coach_step_log(
        _coach_emulator_log_dir(db_path),
        game_session_id,
        round_number=round_number,
        question=question_before,
        decision=decision,
        action_result=result.action_result,
        consequence=transition,
        state_after=state_after_snapshot,
        next_question=question_after,
    )
    _register_coach_session(
        db_path,
        session_id=coach_session_id,
        game_session_id=game_session_id,
        agent_session_id=drive_session_id,
        seed=next_seed,
        context_bootstrap=False,
    )
    next_system_feedback = str((status.get("observation") or {}).get("warning") or "")
    _save_state(
        db_path,
        game_session_id=game_session_id,
        agent_session_id=drive_session_id,
        seed=next_seed,
        pending_system_feedback=next_system_feedback,
    )
    inspection = _inspection_payload(
        session_id=coach_session_id,
        game_session_id=game_session_id,
        agent_session_id=drive_session_id,
        seed=next_seed,
        observed=status,
        checkpoints=checkpoint_store.recent(),
        recent_count=5,
        coach_records=read_coach_step_logs(log_path),
        log_file=log_path,
    )
    if result.benchmark.get("attempt"):
        benchmark.finish(actual_output, result.benchmark["attempt"],
                         round=round_number,
                         career_state={"question": question_before,
                                       "choice": decision.get("choice") or {},
                                       "transition": transition,
                                       "state_after": state_after_snapshot,
                                       "next_question": question_after},
                         shorttitle=(decision.get("event") or {}).get("title", f"事件 {round_number}"),
                         final_score=benchmark.terminal_score(status.get("observation") or {}))
    copied_notebooks = _copy_keeps_notebooks(drive_session_id, actual_output)
    control.acknowledge()
    return {
        "ok": True,
        **_player_session_fields(coach_session_id),
        "game_session_id": game_session_id,
        "agent_session_id": drive_session_id,
        "drive_session_id": drive_session_id,
        "seed": next_seed,
        "previous_seed": checkpoint_seed,
        "action_result": result.action_result,
        "pause_boundary": "next_observe_or_take_action",
        "post_action_work": "read-only teammate work may still be running; inspect the live transcript",
        "decision": decision,
        "state_transition": transition,
        "inspection": inspection,
        "benchmark": result.benchmark,
        "debug_notebooks": [str(path) for path in copied_notebooks],
        "events_log": str(result.events_path),
        "transcript_log": str(result.transcript_path),
    }


def _verified_terminal_outcome(observation: dict[str, Any]) -> str | None:
    """Return the authoritative terminal outcome, never inferred from agent prose."""
    ending_score = observation.get("ending_score")
    if not isinstance(ending_score, dict):
        return None
    current_state = observation.get("current_state") or {}
    flags = current_state.get("simulation_flags") or {}
    if (
        ending_score.get("outcome") == "completed"
        and ending_score.get("completed") is True
        and ending_score.get("survival_months", 0) >= 48
    ):
        return "completed"
    if ending_score.get("outcome") == "eliminated" and (
        flags.get("failed") is True or flags.get("alive") is False
    ):
        return "eliminated"
    return None


def _auto_postmortem_result(
    result: dict[str, Any],
    *,
    started: float,
    events_completed: int,
    termination_reason: str,
    terminal_outcome: str | None = None,
    observation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Annotate the final single-command result for post-run analysis."""
    payload = dict(result)
    payload.update(
        {
            "mode": "auto-postmortem",
            "events_completed": events_completed,
            "elapsed_s": time.monotonic() - started,
            "termination_reason": termination_reason,
            "terminal": terminal_outcome is not None,
            "terminal_outcome": terminal_outcome,
            "postmortem_ready": True,
        }
    )
    if observation is not None:
        payload["ending_score"] = observation.get("ending_score")
    return payload


async def _run_auto_postmortem(args: argparse.Namespace) -> dict[str, Any]:
    """Run uninterrupted to a verified ending, then return once for postmortem review."""
    started = time.monotonic()
    db_path = Path(args.db).resolve()
    fresh_events = getattr(args, "fresh_events", False)
    if fresh_events and (args.mode or _resolve_mode()) != "team":
        raise ValueError("--fresh-events requires a team solution")
    # Auto-postmortem observes the player's own lifetime, without coach deadlines.
    args.timeout_s = float("inf")
    events_completed = 0
    if args.session_id:
        context = await _resolve_context(db_path, args.session_id)
        status = await _peek(db_path, context["game_session_id"])
        observation = status.get("observation") or {}
        terminal_outcome = _verified_terminal_outcome(observation)
        if terminal_outcome is not None:
            inspection = await _inspect(args)
            return _auto_postmortem_result(
                {
                    "ok": True,
                    **_player_session_fields(args.session_id),
                    "game_session_id": context["game_session_id"],
                    "agent_session_id": context["agent_session_id"],
                    "seed": context.get("seed", ""),
                    "inspection": inspection.get("inspection"),
                },
                started=started,
                events_completed=0,
                termination_reason=f"terminal_{terminal_outcome}",
                terminal_outcome=terminal_outcome,
                observation=observation,
            )
        result = await _run_step(args)
    else:
        result = await _run_step(args, start=True)

    while True:
        if not result.get("ok"):
            return _auto_postmortem_result(
                result,
                started=started,
                events_completed=events_completed,
                termination_reason=str(result.get("termination_reason") or "step_failed"),
            )

        events_completed += 1
        game_session_id = str(result.get("game_session_id") or "")
        if not game_session_id:
            failed = dict(result)
            failed.update({"ok": False, "error": "Auto-postmortem step returned no game_session_id."})
            return _auto_postmortem_result(
                failed,
                started=started,
                events_completed=events_completed,
                termination_reason="missing_game_session_id",
            )

        status = await _peek(db_path, game_session_id)
        observation = status.get("observation") or {}
        terminal_outcome = _verified_terminal_outcome(observation)
        if terminal_outcome is not None:
            return _auto_postmortem_result(
                result,
                started=started,
                events_completed=events_completed,
                termination_reason=f"terminal_{terminal_outcome}",
                terminal_outcome=terminal_outcome,
                observation=observation,
            )

        step_args = argparse.Namespace(**vars(args))
        step_args.session_id = str(result["jiuwen_player_session_id"])
        step_args.seed = str(result["seed"])
        # Enrollment precedes team construction. Freeze after the first normal
        # event; later events each use a disposable first-observe conversation.
        if fresh_events and (args.session_id or events_completed >= 2):
            from career_sim_runner.coach.fresh_loop import next_event

            try:
                await next_event(db_path, step_args.session_id,
                                 args.ws_url or resolve_instance_ws_url())
            except (RuntimeError, OSError, ValueError) as exc:
                failed = {**result, "ok": False, "error": str(exc)}
                return _auto_postmortem_result(
                    failed, started=started, events_completed=events_completed,
                    termination_reason="context_rotation_failed")
        result = await _run_step(step_args)


async def _withdraw(args: argparse.Namespace) -> dict[str, Any]:
    db_path = Path(args.db).resolve()
    context = await _resolve_context(db_path, args.session_id)
    player = context["agent_session_id"]
    game = context["game_session_id"]
    control = Gate(gate_path(db_path, player))
    ws_url = getattr(args, "ws_url", "") or resolve_instance_ws_url()
    timeout = getattr(args, "reload_timeout_s", 30.0)
    store = CheckpointStore(db_path, game)
    transaction_path = _withdraw_transaction_path(db_path, player)
    transaction = json.loads(transaction_path.read_text()) if transaction_path.exists() else {}
    requested = getattr(args, "checkpoint_round", None)
    if transaction and not transaction.get("complete"):
        if requested and requested != transaction["round"]:
            raise RuntimeError("A withdrawal is pending; finish the same checkpoint before selecting another")
    else:
        records = store.records()
        target = requested or len(records)
        if not records:
            raise RuntimeError(f"No coach checkpoint exists for {game}.")
        if not 1 <= target <= len(records):
            raise ValueError(f"checkpoint round must be between 1 and {len(records)}")
        selected = records[target - 1]
        if selected.get("before_notebooks") is None:
            raise RuntimeError("Checkpoint has no runtime notebook snapshot; cannot safely withdraw")
        current = await read_session_payload(db_path, game)
        latest = records[-1]
        replayed_latest = _apply_payload_delta(
            latest.get("before_payload"), latest.get("payload_delta") or []
        )
        current_hash = _json_hash(current) if current else ""
        if not current or (current_hash not in {latest["after_hash"], _json_hash(replayed_latest)}
                           and not _only_observation_consumed(replayed_latest, current)):
            raise RuntimeError("Game state changed after the checkpoint; refusing to overwrite it.")
        transaction = {"round": target, "checkpoint": selected, "game": game,
                       "operation_id": uuid.uuid4().hex, "latest_hash": current_hash,
                       "previous_at": records[target - 2]["captured_at"] if target > 1 else "",
                       "team": control.read().get("mode") == "team" if control.path.exists() else _resolve_mode() == "team"}
        atomic_json(transaction_path, transaction)
    selected = transaction["checkpoint"]
    target = transaction["round"]
    notebooks = align_notebook_snapshot(selected["before_notebooks"], _runtime_skills_dir(player))
    # Revoke the tool permission before cancelling producers. Server rewind also
    # awaits stream completion and drains the durable history writer queue.
    if control.path.exists():
        control.revoke()
        await cancel_agent_session(ws_url, player, mode="team" if transaction["team"] else "agent", timeout_s=timeout)
        await retire_gated(control)
        control.update(reload_notebooks=notebooks)
    if "boundary" not in transaction:
        history = read_history(jiuwenswarm_data_dir(), player)
        locator = selected.get("observe_boundary")
        try:
            transaction["boundary"] = (observe_boundary(history, locator) if locator
                                       else legacy_boundary(history, selected, transaction["previous_at"]))
        except RuntimeError:
            try:
                transaction["boundary"] = legacy_boundary(history, selected, transaction["previous_at"])
            except RuntimeError:
                transaction["boundary"] = pruned_history_boundary(history, selected)
        atomic_json(transaction_path, transaction)
    if "rewind" not in transaction:
        result = await rewind_agent_session(
            ws_url, player, before_tool_call_id=transaction["boundary"]["tool_call_id"],
            operation_id=transaction["operation_id"], reset_team=transaction["team"], timeout_s=timeout,
        )
        if any(result.get(key) is not True for key in ("rewind_context", "context_persisted", "history_persisted")):
            raise RuntimeError("Jiuwen rewind context and persistence were not verified")
        if transaction["team"] and result.get("team_reset") is not True:
            raise RuntimeError("Jiuwen team reset was not verified")
        transaction["rewind"] = result
        atomic_json(transaction_path, transaction)
    runtime = _runtime_skills_dir(player)
    restored_notebooks = restore_notebooks(runtime, notebooks)
    verify_notebooks(runtime, notebooks)
    log_path = session_log_path(_coach_emulator_log_dir(db_path), game)
    retained = [r for r in read_coach_step_logs(log_path) if r.get("round", 0) < target]
    current = await read_session_payload(db_path, game)
    expected = selected.get("before_payload")
    if _json_hash(current) not in {transaction["latest_hash"], _json_hash(expected)}:
        raise RuntimeError("Game changed during pending withdrawal")
    emulator = await _replace_session(db_path, game, expected, selected.get("before_logs", []), retained)
    if (emulator.get("same_session_id") is not True or emulator.get("session_id") != game
            or await read_session_payload(db_path, game) != expected):
        raise RuntimeError("CareerSim rewind verification failed")
    seed = selected.get("seed") or (_event_seed(game, expected, _load_state(db_path).get("random_seed", "")) if expected else "")
    _save_state(db_path, game_session_id=game if expected else "", agent_session_id=player, seed=seed)
    registry = _load_registry(db_path)
    registry.setdefault(args.session_id, {}).update(
        game_session_id=game, agent_session_id=player, seed=seed, context_bootstrap="1",
        jiuwen_rewound="1", context_source_game_session_id=game,
    )
    if transaction["team"]:
        registry[args.session_id]["jiuwen_team_reset"] = "1"
    _save_registry(db_path, registry)
    reloaded = {}
    if expected is not None and not getattr(args, "defer_reload", False):
        reloaded = await _reload_solution(Namespace(
            db=args.db, session_id=args.session_id, solution=getattr(args, "solution", ""),
            agent_session_id=player, ws_url=ws_url, reload_timeout_s=timeout, withdraw_recovery=True,
        ))
        if reloaded.get("reloaded") is not True or reloaded.get("agent_session_id") != player:
            raise RuntimeError("Withdrawal reload was not verified or changed Player identity")
    # Only consume checkpoints after all required phases have succeeded. A
    # crash after truncation can still finish from the durable transaction.
    if len(store.records()) >= target:
        store.withdraw(target)
    output = _load_state(db_path).get("benchmark_output_dir")
    if output:
        benchmark.mark_withdrawn(Path(output), game, target)
        if not transaction.get("correction_recorded"):
            benchmark.record_correction(Path(output), game_session_id=game, round_number=target,
                                        reason=getattr(args, "reason", "") or "Coach withdrew a committed decision",
                                        kind=getattr(args, "correction_kind", "policy"), source="withdraw",
                                        operation_id=transaction["operation_id"])
            transaction["correction_recorded"] = True
    observed = await _peek(db_path, game) if expected is not None else None
    if observed:
        _save_state(db_path, pending_system_feedback=str((observed.get("observation") or {}).get("warning") or ""))
    transaction["complete"] = True
    atomic_json(transaction_path, transaction)
    inspection = _inspection_payload(
        session_id=args.session_id, game_session_id=game, agent_session_id=player, seed=seed,
        observed=observed, checkpoints=store.recent(), recent_count=5, coach_records=retained, log_file=log_path,
    ) if observed else None
    return {"ok": True, **reloaded, **_player_session_fields(args.session_id), "agent_session_id": player,
            "withdrawn": True, "withdrawn_round": target, "remaining_checkpoints": target - 1,
            "game_session_id": game, "restored_from": selected["captured_at"], "seed_id": seed, "seed": seed,
            "withdrawn_decision": selected.get("decision", {}),
            "withdrawn_question": selected.get("decision", {}).get("event", {}),
            "withdrawn_option": selected.get("decision", {}).get("choice", {}), "withdrawn_record": selected,
            "state_transition": selected.get("state_transition", {}), "inspection": inspection,
            "reload_after_withdraw": bool(reloaded), "jiuwen_rewind": transaction["rewind"],
            "observe_boundary": transaction["boundary"], "emulator_rewind": emulator,
            "restored_notebooks": [str(p) for p in restored_notebooks]}


def _withdraw_transaction_path(db_path: Path, player: str) -> Path:
    return gate_path(db_path, player).with_suffix(".withdraw.json")


def _require_no_pending_withdraw(db_path: Path, player: str) -> None:
    path = _withdraw_transaction_path(db_path, player)
    if path.exists() and not json.loads(path.read_text()).get("complete"):
        raise RuntimeError("Withdrawal is pending; retry withdraw to finish recovery before stepping or reloading")



def _select_inspection_records(
    records: list[dict[str, Any]],
    *,
    start_round: int | None,
    end_round: int | None,
) -> list[dict[str, Any]]:
    """Apply the coach's inclusive 1-based ``-i``/``-j`` range."""
    if start_round is None and end_round is None:
        return records[-1:] if records else []
    if start_round is not None and start_round <= 0:
        raise RuntimeError("-i/--from-round must be a positive round number.")
    if end_round is not None and end_round <= 0:
        raise RuntimeError("-j/--to-round must be a positive round number.")
    lower = start_round or 1
    upper = end_round or len(records)
    if lower > upper:
        raise RuntimeError("-i/--from-round must not be greater than -j/--to-round.")
    return [record for record in records if lower <= record.get("round", 0) <= upper]


def _checkpoint_evidence(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adapt pre-enrichment checkpoints to the unified inspection shape."""
    evidence: list[dict[str, Any]] = []
    for round_number, checkpoint in enumerate(records, start=1):
        transition = checkpoint.get("state_transition") or {}
        decision = checkpoint.get("decision") or {}
        evidence.append(
            {
                "round": round_number,
                "created_at": checkpoint.get("captured_at", ""),
                "question": decision.get("question") or {"event": decision.get("event", {})},
                "decision": decision,
                "action_result": {},
                "consequence": transition,
                "state_after": transition.get("after") or {},
                "next_question": {},
            }
        )
    return evidence


async def _inspect(args: argparse.Namespace) -> dict[str, Any]:
    """Return current question plus recent decision evidence for the coach."""
    db_path = Path(args.db).resolve()
    context = await _resolve_context(db_path, args.session_id)
    game_session_id = context["game_session_id"]
    log_path = session_log_path(_coach_emulator_log_dir(db_path), game_session_id)
    coach_records = read_coach_step_logs(log_path)
    checkpoints = CheckpointStore(db_path, game_session_id).records()
    evidence_records = coach_records or _checkpoint_evidence(checkpoints)
    selected_records = _select_inspection_records(
        evidence_records,
        start_round=getattr(args, "from_round", None),
        end_round=getattr(args, "to_round", None),
    )
    recent_count = len(selected_records) if selected_records else (1 if not evidence_records else 0)
    # Always read the emulator for inspect.  Structured coach logs are useful
    # history, but they can lag behind a committed action when a long Jiuwen
    # stream is interrupted; inspect must report the live event and status.
    observed = await _peek(db_path, game_session_id, log_count=max(recent_count, 1))
    log_tail = read_session_log_tail(log_path)
    current_payload = await read_session_payload(db_path, game_session_id)
    current_seed = context.get("seed", "")
    if not current_seed and current_payload:
        current_seed = _event_seed(game_session_id, current_payload, _load_state(db_path).get("random_seed", ""))
    return {
        "ok": True,
        **_player_session_fields(args.session_id),
        "game_session_id": game_session_id,
        "agent_session_id": context["agent_session_id"],
        "seed": current_seed,
        "inspection": _inspection_payload(
            session_id=args.session_id,
            game_session_id=game_session_id,
            agent_session_id=context["agent_session_id"],
            seed=current_seed,
            observed=observed,
            checkpoints=checkpoints,
            recent_count=recent_count,
            coach_records=evidence_records or None,
            selected_records=selected_records,
            log_file=log_path,
            log_truncated=bool(log_tail.get("truncated")),
            raw_log_lines=log_tail.get("lines", []),
        ),
    }


def _installed_rules_prompt(skill_dir: Path | None) -> str:
    """Deliver current rules after rewind instead of relying on voluntary rereads."""
    if skill_dir is None:
        return ""
    paths = sorted(path for path in skill_dir.rglob("*.md") if
                   path.name == "SKILL.md" or "stages" in path.relative_to(skill_dir).parts
                   or "roles" in path.relative_to(skill_dir).parts)
    if not paths:
        raise RuntimeError("Rewind reload has no installed skill rules to deliver")
    return "\n以下为本次已安装规则的完整当前版本，按这些内容执行：\n" + "\n".join(
        f"\n<installed_rule path={json.dumps(str(path), ensure_ascii=False)}>\n"
        f"{path.read_text(encoding='utf-8')}\n</installed_rule>" for path in paths
    )


async def _reload_solution_impl(args: argparse.Namespace) -> dict[str, Any]:
    """Hot-reload skills while preserving the active Jiuwen context by default."""
    ensure_runtime_dirs()
    db_path = Path(args.db).resolve()
    context = await _resolve_context(db_path, args.session_id)
    ws_url = args.ws_url or resolve_instance_ws_url()
    requested_agent_session_id = str(getattr(args, "agent_session_id", "") or "").strip()
    if not getattr(args, "withdraw_recovery", False):
        _require_no_pending_withdraw(db_path, context["agent_session_id"])
    registry_record = _load_registry(db_path).get(args.session_id, {})
    reuse_rewound_session = registry_record.get("jiuwen_rewound") == "1"
    reset_rewound_team = registry_record.get("jiuwen_team_reset") == "1"
    if reuse_rewound_session and requested_agent_session_id not in {"", context["agent_session_id"]}:
        raise RuntimeError(
            "A rewound Jiuwen session must keep its original agent_session_id; "
            "recover missing teammates inside that session instead."
        )
    # A reload is a configuration change, not a conversation reset. Preserve
    # the Leader, its team, pending stage and short-term context unless the
    # caller explicitly asks for a disaster-recovery replacement session.
    new_agent_session_id = requested_agent_session_id or context["agent_session_id"]
    reused_agent_session = new_agent_session_id == context["agent_session_id"]
    old_control = Gate(gate_path(db_path, context["agent_session_id"]))
    if old_control.path.exists():
        old_state = old_control.read()
        if (
            not reset_rewound_team
            and old_state.get("mode") == "team"
            and old_state.get("worker_status") != "new"
        ):
            # Team follow-ups publish through the original stream waiter. Park
            # the runtime before replacing that waiter, then resume the same
            # session after the refreshed configuration has been installed.
            await pause_agent_session(
                ws_url,
                context["agent_session_id"],
                mode="team",
                timeout_s=getattr(args, "reload_timeout_s", 30.0),
            )
        await retire_gated(old_control)
    game_session_id = context["game_session_id"]
    current_payload = await read_session_payload(db_path, game_session_id)
    if not current_payload:
        raise RuntimeError(f"Unknown game session: {game_session_id}")
    seed = _event_seed(game_session_id, current_payload, _load_state(db_path).get("random_seed", ""))
    solution_dir = _resolve_solution_dir(args.solution)
    runtime_root = _runtime_skills_dir(context["agent_session_id"])
    # Keep the snapshot in the retired gate until installation succeeds so a
    # failed reload can retry without capturing partially installed defaults.
    notebook_snapshot = old_control.read().get("reload_notebooks") if old_control.path.exists() else None
    if notebook_snapshot is None:
        notebook_snapshot = capture_notebooks(runtime_root)
        if old_control.path.exists():
            old_control.update(reload_notebooks=notebook_snapshot)
    source_game_session_id = str(
        registry_record.get("context_source_game_session_id")
        or registry_record.get("game_session_id")
        or game_session_id
    )
    history_records = _rewrite_session_ids(
        _context_history_records(db_path, source_game_session_id),
        source_game_session_id,
        game_session_id,
    )
    pending_path = _withdraw_transaction_path(db_path, context["agent_session_id"])
    if pending_path.exists():
        pending = json.loads(pending_path.read_text())
        if not pending.get("complete"):
            history_records = [r for r in history_records if r.get("round", 0) < pending["round"]]
    output_dir = Path(_load_state(db_path).get("benchmark_output_dir") or _coach_output_dir())
    output_dir.mkdir(parents=True, exist_ok=True)
    history_dir = benchmark.runtime_dir(output_dir)
    history_dir.mkdir(parents=True, exist_ok=True)
    history_file = history_dir / f"previous-context-{new_agent_session_id}.json"
    history_document = json.dumps({
        "description": "这是重新加载前的历史上下文，仅供参考；当前状态以observe为准，历史动作不得重放。",
        "game_session_id": game_session_id, "records": history_records,
    }, ensure_ascii=False, indent=2)
    if source_game_session_id != game_session_id and source_game_session_id in history_document:
        raise RuntimeError("Old game session id remains in rebuilt Jiuwen context.")
    history_file.write_text(history_document, encoding="utf-8")
    revision_dir = history_dir / "revisions" / new_agent_session_id
    if revision_dir.exists():
        revision_dir = revision_dir.with_name(f"{new_agent_session_id}-{uuid.uuid4().hex[:8]}")
    snapshot_solution(solution_dir, revision_dir)
    _save_state(db_path, solution_snapshot=str(revision_dir / "solution"))
    install_record = install_submission(solution_dir)
    notebook_snapshot = align_notebook_snapshot(notebook_snapshot, Path(install_record.skill_dir))
    restored_notebooks = (
        restore_notebooks(Path(install_record.skill_dir), notebook_snapshot)
        if install_record.skill_dir else []
    )
    rebound_session_notebooks = (
        _bind_installed_game_session(Path(install_record.skill_dir), game_session_id)
        if install_record.skill_dir and not reuse_rewound_session
        else []
    )
    verify_notebooks(Path(install_record.skill_dir) if install_record.skill_dir else None, notebook_snapshot)
    run_mode = resolve_run_mode(install_record)
    reload_prompt = _step_prompt(game_session_id)
    if reuse_rewound_session:
        reload_prompt += (
            "\n已回滚到本事件起点，笔记同步恢复。重读已安装 solution，"
            "从 observe 开始完整一轮；按恢复的笔记继续事件编号，重新生成本轮翻译和动作状态，"
            "不复用撤回事件的上下文。当前游戏状态是唯一事实。"
            "不得创建或切换 agent_session_id。"
        )
        if reset_rewound_team and run_mode == "team":
            reload_prompt += "旧团队运行时及其任务记忆已清空。"
        reload_prompt += _installed_rules_prompt(
            Path(install_record.skill_dir) if install_record.skill_dir else None
        )
    elif reused_agent_session and run_mode == "team":
        reload_prompt += (
            "\n这是 reload 后对同一 Player 会话的继续；保留原 host/Leader 和现有团队状态。"
            "使用已重新安装的 solution 从当前阶段继续，不重放历史动作。"
            "不得创建或切换 agent_session_id。"
        )
    elif run_mode == "team":
        reload_prompt += "\n这是显式替换后的新 Player 会话，请按已安装 solution 继续。"
    if not reused_agent_session:
        installed_root = Path(install_record.skill_dir).resolve()
        installed_names = install_record.manifest.get("participant_skill_names", [])
        installed_paths = [str(installed_root / name) for name in installed_names]
        reload_prompt += (
            f"\n本会话的已安装 skill 目录是 {', '.join(installed_paths)}。"
            "只读取本角色文档，执行其中列出的脚本；不读取源码、其他角色文档或 coach/runner 文件。"
            "路径或脚本报错即停，不搜索、不自行调试。"
            "仓库中的 solution/ 是提交源，不是本局运行记录。"
            "从已安装 skill 的 SKILL.md 开始，事件号以其 notebooks 的脚本返回值为准。"
        )
    # Rebuilt history remains coach evidence. Player resumes from its own
    # conversation and restored notebooks without reading private coach files.
    control = Gate(gate_path(db_path, new_agent_session_id))
    if control.path.exists():
        control.path.unlink()
    control.create(game_session_id, agent_session_id=new_agent_session_id, ws_url=ws_url,
                   mode=run_mode, existing_game=True, log_dir=str(output_dir),
                   benchmark_output_dir=str(output_dir), random_seed=_load_state(db_path).get("random_seed", ""),
                   prompt=reload_prompt, reload_notebooks=notebook_snapshot, recovery_ready=False)
    emulator_log_dir, _ = _ensure_coach_emulator_log_dir(
        db_path,
        game_session_id=game_session_id,
    )
    ensure_instance_configured(log_dir=emulator_log_dir, coach_gate=control.path, db_path=db_path)
    if not await reload_agent_config(ws_url, timeout_s=args.reload_timeout_s):
        raise RuntimeError("JiuwenSwarm rejected agent.reload_config.")
    CheckpointStore(db_path, game_session_id).rebind_drive_session(new_agent_session_id)
    _replace_coach_session_id(
        db_path,
        old_id=args.session_id,
        new_id=new_agent_session_id,
        game_session_id=game_session_id,
        agent_session_id=new_agent_session_id,
        seed=seed,
        context_bootstrap="1",
    )
    registry = _load_registry(db_path)
    if new_agent_session_id in registry:
        registry[new_agent_session_id].pop("jiuwen_rewound", None)
        registry[new_agent_session_id].pop("jiuwen_team_reset", None)
        registry[new_agent_session_id].pop("rewind_turn_index", None)
        registry[new_agent_session_id].pop("context_source_game_session_id", None)
        _save_registry(db_path, registry)
    _save_state(
        db_path,
        game_session_id=game_session_id,
        agent_session_id=new_agent_session_id,
        seed=seed,
        context_mode="persistent",
    )
    store_last_drive_session_id(new_agent_session_id)
    # This snapshot is a retry journal, not a permanent reload baseline.
    control.update(recovery_ready=True, reload_notebooks=None)
    return {
        "ok": True,
        "reloaded": True,
        "reused_agent_session": reused_agent_session,
        "reused_rewound_jiuwen_session": reuse_rewound_session,
        "reset_rewound_team": reset_rewound_team,
        "previous_jiuwen_player_session_id": args.session_id,
        # Compatibility alias for callers of older coach.py versions.
        "previous_session_id": args.session_id,
        **_player_session_fields(new_agent_session_id),
        "game_session_id": game_session_id,
        "agent_session_id": new_agent_session_id,
        "seed": seed,
        "context_history_rounds": len(history_records),
        "context_history_file": str(history_file),
        "solution_dir": str(solution_dir),
        "submission_name": install_record.submission_name,
        "rebound_session_notebooks": [str(path) for path in rebound_session_notebooks],
        "restored_notebooks": [str(path) for path in restored_notebooks],
        "next": "run coach.py step to release one execution permit for the persistent conversation",
    }


async def _reload_solution(args: argparse.Namespace) -> dict[str, Any]:
    """Resume the cost clock for reload; never erase withdrawn attempts."""
    db_path = Path(args.db).resolve()
    output = Path(_load_state(db_path).get("benchmark_output_dir") or _coach_output_dir())
    source = _resolve_solution_dir(args.solution)
    snapshot_solution(source, output)
    _save_state(db_path, benchmark_output_dir=str(output))
    context = await _resolve_context(db_path, args.session_id)
    attempt = benchmark.begin(output, phase="reload", agent_session_id=args.session_id,
                              game_session_id=context["game_session_id"],
                              round=len(CheckpointStore(db_path, context["game_session_id"]).records()) + 1)
    started = time.monotonic()
    try:
        result = await _reload_solution_impl(args)
    except BaseException as exc:
        benchmark.finish(output, attempt, elapsed_s=time.monotonic() - started,
                         action_success=False, termination_reason=type(exc).__name__, error=str(exc))
        raise
    result["benchmark"] = benchmark.finish(output, attempt, elapsed_s=time.monotonic() - started,
                                          action_success=True, termination_reason="reloaded")
    result["benchmark_output_dir"] = str(output)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Coach-only CareerSim checkpointed controls")
    parser.add_argument("--db", default=str(default_db_path()), help="Career Emulator SQLite database")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_db_override(subparser: argparse.ArgumentParser) -> None:
        # Accept both ``coach.py --db PATH step`` and the more natural
        # ``coach.py step --db PATH`` without changing the default.
        subparser.add_argument("--db", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    subparsers.add_parser("compare", help="Compare all coach benchmarks and write comparison.csv")
    correction = subparsers.add_parser("correction", help="Record a coach correction without rewinding")
    correction.add_argument("--round", dest="round_number", type=int, required=True)
    correction.add_argument("--reason", required=True)
    correction.add_argument("--kind", choices=("policy", "infrastructure", "experiment"), default="policy")

    snapshot = subparsers.add_parser("snapshot", help="Archive a solution without advancing the game")
    snapshot.add_argument("--solution", default=str(REPO_ROOT / "solution"))
    snapshot.add_argument("--solution-keyword", required=True)
    snapshot.add_argument("--activate", action="store_true", help="Use this archive for subsequent coach attempts")

    start = subparsers.add_parser("start", help="Start a fresh game and send the first event to Jiuwen")
    add_db_override(start)
    start.add_argument("--random-seed", default=DEFAULT_BENCHMARK_SEED, help="Fixed monthly sampling seed; empty string uses native random session IDs")
    start.add_argument("--solution-keyword", default="", help="Archive label, e.g. mostroles")
    start.add_argument("--agent-session-id", default="", help="Reuse an existing Jiuwen conversation id")
    start.add_argument("--ws-url", default="")
    start.add_argument("--mode", choices=("agent", "team"), default="")
    start.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)

    auto_postmortem = subparsers.add_parser(
        "auto-postmortem",
        help="Run without coach intervention until a verified ending, then return once for analysis",
    )
    add_db_override(auto_postmortem)
    auto_postmortem.add_argument(
        "--jiuwen-player-session-id",
        "--session-id",
        dest="session_id",
        default="",
        help="Resume this player session; omit to start a fresh game",
    )
    auto_postmortem.add_argument("--random-seed", default=DEFAULT_BENCHMARK_SEED)
    auto_postmortem.add_argument("--solution-keyword", default="")
    auto_postmortem.add_argument("--agent-session-id", default="")
    auto_postmortem.add_argument("--ws-url", default="")
    auto_postmortem.add_argument("--mode", choices=("agent", "team"), default="")
    auto_postmortem.add_argument("--seed", default="", help="Expected seed when resuming an existing session")
    auto_postmortem.add_argument("--fresh-events", action="store_true",
                                help="Use a first-observe session per event after initial team setup")
    auto_postmortem.set_defaults(timeout_s=float("inf"))

    step = subparsers.add_parser("step", help="Send exactly one event to the current game")
    add_db_override(step)
    step.add_argument(
        "--jiuwen-player-session-id",
        "--session-id",
        dest="session_id",
        required=True,
        help="Jiuwen player session id returned by start or reload (legacy alias: --session-id)",
    )
    step.add_argument("--ws-url", default="")
    step.add_argument("--mode", choices=("agent", "team"), default="")
    step.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    step.add_argument("--seed", default="", help="Expected event seed, e.g. <game_session_id>:<month>")
    step.add_argument("--resume-stopped", action="store_true",
                      help="Resume a stopped Player stream in its existing execution gate")

    reload = subparsers.add_parser("reload", help="Resume a stopped Player stream in the same game")
    add_db_override(reload)
    reload.add_argument("--jiuwen-player-session-id", "--session-id", dest="session_id", required=True)
    reload.add_argument("--solution", default="", help="Installed solution directory to restore")
    reload.add_argument("--agent-session-id", default="", help="Optional replacement Jiuwen conversation id")
    reload.add_argument("--ws-url", default="")
    reload.add_argument("--reload-timeout-s", type=float, default=30.0)

    inspect = subparsers.add_parser(
        "inspect",
        help="Show the current question and recent decisions, consequences, and event logs",
    )
    add_db_override(inspect)
    inspect.add_argument(
        "--jiuwen-player-session-id",
        "--session-id",
        dest="session_id",
        required=True,
        help="Jiuwen player session id returned by start or reload (legacy alias: --session-id)",
    )
    inspect.add_argument(
        "-i",
        "--from-round",
        dest="from_round",
        type=int,
        default=None,
        help="First inclusive decision round to disclose (1-based)",
    )
    inspect.add_argument(
        "-j",
        "--to-round",
        dest="to_round",
        type=int,
        default=None,
        help="Last inclusive decision round to disclose (1-based)",
    )

    withdraw = subparsers.add_parser("withdraw", help="Withdraw the latest or a selected coach-controlled event")
    add_db_override(withdraw)
    withdraw.add_argument(
        "--jiuwen-player-session-id",
        "--session-id",
        dest="session_id",
        required=True,
        help="Jiuwen player session id returned by start or reload (legacy alias: --session-id)",
    )
    withdraw.add_argument(
        "--checkpoint-round",
        type=int,
        default=0,
        help="Restore the state before this 1-based checkpoint round; defaults to the latest round.",
    )
    withdraw.add_argument("--reason", default="", help="Reason for correcting this decision")
    withdraw.add_argument("--correction-kind", choices=("policy", "infrastructure", "experiment"), default="policy")
    withdraw.add_argument("--defer-reload", action="store_true", help="Stop after withdrawal; reload explicitly after editing")
    withdraw.add_argument("--solution", default="", help="Solution directory to install during restore reload")
    withdraw.add_argument(
        "--agent-session-id", default="", help="Optional explicit replacement Jiuwen conversation id"
    )
    withdraw.add_argument("--ws-url", default="")
    withdraw.add_argument("--reload-timeout-s", type=float, default=30.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "compare":
            payload = {"ok": True, "runs": benchmark.compare(default_output_root() / "coach")}
        elif args.command == "correction":
            state = _load_state(Path(args.db).resolve())
            if not state.get("benchmark_output_dir"):
                raise RuntimeError("No active benchmark")
            payload = {"ok": True, "correction": benchmark.record_correction(
                Path(state["benchmark_output_dir"]), game_session_id=state.get("game_session_id", ""),
                round_number=args.round_number, reason=args.reason, kind=args.kind)}
        elif args.command == "snapshot":
            output = allocate_snapshot(default_output_root() / "coach", Path(args.solution).resolve(), args.solution_keyword)
            if args.activate:
                _save_state(Path(args.db).resolve(), benchmark_output_dir=str(output),
                            solution_keyword=args.solution_keyword, solution_snapshot=str(output / "solution"))
            payload = {"ok": True, "output_dir": str(output), "solution_dir": str(output / "solution")}
        elif args.command == "start":
            payload = asyncio.run(_run_step(args, start=True))
        elif args.command == "auto-postmortem":
            payload = asyncio.run(_run_auto_postmortem(args))
        elif args.command == "step":
            payload = asyncio.run(_run_step(args))
        elif args.command == "reload":
            payload = asyncio.run(_reload_solution(args))
        elif args.command == "inspect":
            payload = asyncio.run(_inspect(args))
        elif args.command == "withdraw":
            payload = asyncio.run(_withdraw(args))
        else:  # pragma: no cover - argparse enforces this
            raise RuntimeError(f"Unknown command: {args.command}")
    except (RuntimeError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
