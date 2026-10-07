"""Pinned Jiuwen server extension for durable tool-boundary rewinds."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

from career_sim_runner.coach.history import retained_history


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


async def rewind_before_tool(server, channel_id: str, session_id: str, params: dict) -> dict:
    """Idempotently stop, flush, trim, reset runtime and persist rebuilt context.

    The operation journal is controller evidence, never a model message. A
    failed persistence can retry after the target call has already been cut.
    """
    from jiuwenswarm.server.runtime.session import session_history as history
    from jiuwenswarm.agents.harness.common.session_ops_service import rewind_session_context

    tool_id = str(params["before_tool_call_id"])
    operation_id = str(params.get("operation_id") or "")
    if not operation_id:
        raise ValueError("tool-boundary rewind requires operation_id")
    # Cancel and await the producers before draining the history writer queue.
    tasks = []
    for task, stop in list(server._session_stream_tasks.get(session_id, {}).items()):
        if not task.done():
            stop.set()
            task.cancel()
            tasks.append(task)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 20)
    if params.get("reset_team"):
        from jiuwenswarm.agents.harness.team import get_team_manager
        await get_team_manager().cancel_session_runtime(session_id, reason="event rewind")
    await asyncio.wait_for(asyncio.to_thread(history._WRITE_QUEUE.join), 20)
    path = history.get_read_history_path(session_id)
    operation = path.parent / "rewinds" / (hashlib.sha256(operation_id.encode()).hexdigest() + ".json")
    if operation.exists():
        journal = json.loads(operation.read_text())
        if journal["tool_call_id"] != tool_id:
            raise RuntimeError("Rewind operation cannot change its tool boundary")
    else:
        original = history.load_history_records(session_id)
        retained = retained_history(original, tool_id)
        journal = {"tool_call_id": tool_id, "original": original, "retained": retained}
        atomic_json(operation, journal)
    if journal.get("complete"):
        if history.load_history_records(session_id) != journal["retained"]:
            raise RuntimeError("Session advanced after this rewind; refusing stale retry")
        return journal["result"]
    if params.get("reset_team") and not journal.get("team_reset"):
        from jiuwenswarm.agents.harness.team import get_team_manager
        if not await get_team_manager().delete_session_runtime(session_id, reason="event rewind"):
            raise RuntimeError("Team runtime reset failed")
        journal["team_reset"] = True
        atomic_json(operation, journal)
    history.write_history_records(session_id, journal["retained"])
    if history.load_history_records(session_id) != journal["retained"]:
        raise RuntimeError("Rewound history persistence verification failed")
    pair = await server._resolve_rewind_agent(channel_id or "default", session_id=session_id)
    if pair is None:
        raise RuntimeError("No Jiuwen agent available to rebuild context")
    deep_agent, _ = pair
    # Filesystem read coverage and raw text caches are process-wide in this
    # pinned harness. Clear them before reloading changed rules/notebooks.
    from openjiuwen.harness.tools import filesystem
    for name in ("_FILE_READ_REGISTRY", "_RAW_TEXT_REGISTRY"):
        cache = getattr(filesystem, name, None)
        if cache is not None:
            cache.clear()
    from openjiuwen.harness.prompts.sections import context as prompt_context
    with prompt_context._CONTEXT_FILE_CACHE_LOCK:
        prompt_context._CONTEXT_FILE_CACHE.clear()
    for rail in deep_agent.configured_rails():
        if type(rail).__name__ == "TaskPlanningRail":
            rail._todos_cache.pop(session_id, None)
            rail._tool_call_counts.pop(session_id, None)
            todo = rail._find_todo_tool()
            if todo is not None:
                await todo.save_todos(session_id, [])
                if await todo.load_todos(session_id):
                    raise RuntimeError("Jiuwen task memory reset failed")
    if not await rewind_session_context(deep_agent=deep_agent, session_id=session_id, turn_index=0):
        raise RuntimeError("Jiuwen context rebuild or persistence failed; session remains paused")
    result = {"session_id": session_id, "before_tool_call_id": tool_id,
              "rewind_context": True, "context_persisted": True, "history_persisted": True,
              "team_reset": bool(journal.get("team_reset")), "operation_id": operation_id,
              "remaining_records": len(journal["retained"]),
              "removed_records": len(journal["original"]) - len(journal["retained"])}
    journal.update(complete=True, result=result)
    atomic_json(operation, journal)
    return result
