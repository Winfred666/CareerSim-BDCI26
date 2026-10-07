"""Cross-process, fail-closed permissions at the emulator execution boundary.

One private control database belongs to one Jiuwen conversation. It is not a
model tool or prompt. A cancelled/reloaded conversation can never resume it.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Gate:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def transaction(self):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM control WHERE id=1").fetchone()
            state = json.loads(row[0])
            yield state
            db.execute("UPDATE control SET value=? WHERE id=1", (json.dumps(state, ensure_ascii=False),))

    def create(self, session_id: str = "", **metadata):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Never reopen/reinitialize a previous gate or overwrite its evidence.
        with self.path.open("xb"):
            pass
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE control (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            state = dict(session_id=session_id, permit=False, busy=False, revoked=False,
                         sequence=0, acknowledged=0, result=None, worker_status="new", **metadata)
            db.execute("INSERT INTO control VALUES (1, ?)", (json.dumps(state),))

    def read(self):
        with sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=10) as db:
            return json.loads(db.execute("SELECT value FROM control WHERE id=1").fetchone()[0])

    def update(self, **values):
        with self.transaction() as state:
            state.update(values)

    def grant(self):
        with self.transaction() as state:
            if state.get("recovery_ready") is False:
                raise RuntimeError("Recovery has not been verified; execution remains paused")
            if state["revoked"] or state["busy"] or state["permit"]:
                raise RuntimeError("Gate is revoked or an execution is already outstanding")
            if state["sequence"] != state["acknowledged"]:
                raise RuntimeError("Previous action has not been checkpointed; inspect before continuing")
            state["permit"] = True
            return state["sequence"]

    def acknowledge(self):
        with self.transaction() as state:
            state["acknowledged"] = state["sequence"]

    def revoke(self):
        with self.transaction() as state:
            if state["busy"]:
                raise RuntimeError("An emulator call is executing; wait for its result before recovery")
            state.update(revoked=True, permit=False)

    async def enter(self, session_id: str, *, action: bool):
        while True:
            with self.transaction() as state:
                if state["revoked"]:
                    raise RuntimeError("This conversation has been retired")
                if state["session_id"] and state["session_id"] != session_id:
                    raise RuntimeError("Session does not belong to this conversation")
                if state["permit"] and not state["busy"]:
                    if (action and state.get("notebook_checkpoints")
                            and state.get("observe_sequence") != state["sequence"]):
                        raise RuntimeError("Current event has not been observed; call observe before take_action")
                    # Serialize observe against actions, including parallel MCP clients.
                    state["busy"] = True
                    if action:
                        state["permit"] = False
                    return
            await asyncio.sleep(0.05)

    def observed(self):
        self.update(busy=False)

    def completed(self, session_id: str, result: dict, choice: int, notes: str):
        with self.transaction() as state:
            state.update(busy=False, permit=False, session_id=session_id,
                         sequence=state["sequence"] + 1, result=result, choice=choice, notes=notes)


def gate_path(db_path: Path, agent_session_id: str) -> Path:
    # Hash the opaque identifier rather than accepting it as a filesystem path.
    import hashlib

    name = hashlib.sha256(agent_session_id.encode()).hexdigest()
    return db_path.parent / "coach_controls" / f"{name}.sqlite3"
