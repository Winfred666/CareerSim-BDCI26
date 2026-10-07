"""Local career-emulator-mcp adapter: transport snapshots and optional debugger gate.

The installed emulator and its rule values are unmodified. Rules are loaded
before the first action, including in a fresh process resuming an existing game.
Ordinary play has no gate.
Snapshots contain precisely the public observe response, never hidden state.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import aiosqlite

from fastmcp import FastMCP

from career_sim_runner.coach.gate import Gate
from career_sim_runner.coach.notebooks import capture_notebooks, runtime_skills_dir
from career_sim_runner.coach.seed import configure_sampling_seed


async def rewind_game_session(
    db_path: Path,
    log_dir: Path,
    session_id: str,
    payload: dict[str, Any] | None,
    logs: list[dict[str, str]],
    coach_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Restore one CareerSim checkpoint in place under the same session ID.

    This is a controller-only interface and is deliberately not registered as
    an MCP tool. The player can observe and act on a game, but only the Coach
    can rewind the persisted emulator session.
    """
    from career_emulator.storage import SessionStore, _close_session_logger
    from career_sim_runner.coach.log import rewrite_session_log, session_log_path

    if payload is not None:
        payload_session_id = str(payload.get("session_id") or "")
        if payload_session_id and payload_session_id != session_id:
            raise ValueError(
                f"Checkpoint belongs to {payload_session_id}, expected {session_id}."
            )

    store = SessionStore(db_path, log_dir)
    await store.initialize()
    async with aiosqlite.connect(db_path) as database:
        await database.execute("BEGIN IMMEDIATE")
        if payload is None:
            await database.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        else:
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            await database.execute(
                """
                INSERT INTO sessions (session_id, payload, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(session_id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (session_id, raw),
            )
        await database.execute("DELETE FROM logs WHERE session_id = ?", (session_id,))
        for entry in logs:
            await database.execute(
                "INSERT INTO logs (session_id, entry_type, message) VALUES (?, ?, ?)",
                (session_id, str(entry.get("entry_type", "system")), str(entry.get("message", ""))),
            )
        await database.commit()

    _close_session_logger(session_id)
    path = session_log_path(log_dir, session_id)
    if payload is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    else:
        rewrite_session_log(path, logs, coach_records or [])
    return {
        "session_id": session_id,
        "rewound": True,
        "same_session_id": True,
        "session_exists": payload is not None,
    }


class EmulatorAdapter:
    def __init__(self, engine, snapshots: Path, gate: Gate | None = None):
        self.engine, self.snapshots, self.gate = engine, snapshots, gate

    async def _prepare_engine(self):
        # CareerState's clamp table is process-local. The upstream engine only
        # loads it on the first monthly settlement; a restarted MCP server can
        # otherwise apply several uncapped actions before that settlement.
        # Use the engine's own cached loader, without changing rules or state.
        await self.engine._get_promotion_applicator()
        if self.gate:
            await configure_sampling_seed(self.engine, self.gate.read().get("random_seed", ""))

    def _checkpoint_notebooks(self):
        if self.gate is None:
            return
        state = self.gate.read()
        if not state.get("notebook_checkpoints") or state.get("notebooks_sequence") == state["sequence"]:
            return
        # Called inside the execution boundary: the previous review has
        # finished, and this event has not delivered new observation feedback.
        root = runtime_skills_dir(state["agent_session_id"])
        self.gate.update(before_notebooks=capture_notebooks(root), notebooks_sequence=state["sequence"])

    async def new_game(self):
        if self.gate:
            # Reserve new_game too: simultaneous requests must not create two games.
            with self.gate.transaction() as state:
                if state["revoked"] or state["session_id"] or state["busy"] or not state["permit"]:
                    raise RuntimeError("A new game is not available in this conversation")
                state["busy"] = True
        try:
            await self._prepare_engine()
            result = (await self.engine.new_game()).to_dict()
            if self.gate:
                self.gate.update(session_id=result["session_id"], busy=False)
            return result
        except BaseException:
            if self.gate:
                self.gate.update(busy=False, revoked=True, permit=False)
            raise

    async def observe(self, session_id: str):
        if self.gate:
            await self.gate.enter(session_id, action=False)
        try:
            self._checkpoint_notebooks()
            if self.gate and self.gate.read().get("observe_sequence") != self.gate.read()["sequence"]:
                # Capture before observe consumes one-shot feedback or fills a
                # queue; enrollment already has a stable game ID at this point.
                store = getattr(self.engine, "store", None)
                if store is not None:
                    session = await store.load_session(session_id)
                    logs = await store.load_logs(session_id)
                    self.gate.update(before_payload=session.to_dict() if session else None,
                                     before_logs=[{"entry_type": entry.entry_type, "message": entry.message}
                                                  for entry in logs])
            await self._prepare_engine()
            result = (await self.engine.observe(session_id)).to_mcp_dict()
            self.snapshots.mkdir(parents=True, exist_ok=True)
            path = self.snapshots / f"{uuid.uuid4().hex}.json"
            with path.open("x", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=False, separators=(",", ":"))
            if self.gate:
                with self.gate.transaction() as state:
                    if state.get("observe_sequence") != state["sequence"]:
                        state.update(observe_sequence=state["sequence"],
                                     observe_boundary={"observe_json_path": str(path.resolve())})
            # Add a reference; preserve every original field including one-shot events.
            return {**result, "observe_json_path": str(path.resolve())}
        finally:
            if self.gate:
                self.gate.observed()

    async def take_action(self, session_id: str, choice: int, notes: str):
        if self.gate:
            await self.gate.enter(session_id, action=True)
        try:
            self._checkpoint_notebooks()
            await self._prepare_engine()
            result = (await self.engine.take_action(session_id, choice, notes)).to_dict()
        except BaseException:
            if self.gate:
                # Unknown commit status: never silently retry or reopen the gate.
                self.gate.update(busy=False, revoked=True, permit=False)
            raise
        if self.gate:
            self.gate.completed(session_id, result, choice, notes)
        return result


def make_server(adapter: EmulatorAdapter):
    server = FastMCP("Career Emulator")
    server.tool(name="new_game", description="Start a new Career Emulator session.")(adapter.new_game)
    server.tool(name="observe", description="Observe current state and choices; observe_json_path references the same public JSON.")(adapter.observe)
    server.tool(name="take_action", description="Apply one choice to a Career Emulator session.")(adapter.take_action)

    @server.tool
    async def check_latest_logs(session_id: str, count: int = 10):
        """Return the last log entries for a Career Emulator session."""
        return await adapter.engine.latest_logs(session_id, count)

    @server.tool
    async def show_employee_handbook() -> str:
        """Show the employee handbook as Markdown."""
        return await adapter.engine.enrollment_handbook()

    return server


def main():
    from career_emulator.game import get_default_engine

    gate = os.environ.get("CAREER_COACH_GATE", "")
    snapshots = Path(os.environ["CAREER_OBSERVE_SNAPSHOTS"])
    make_server(EmulatorAdapter(get_default_engine(), snapshots, Gate(Path(gate)) if gate else None)).run()


if __name__ == "__main__":
    main()
