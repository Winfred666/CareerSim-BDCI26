"""CLI-side controls for a persistent, tool-gated Jiuwen conversation."""

import asyncio
import os
import subprocess
import sys
import time

from pathlib import Path

from career_sim_runner.coach import benchmark
from career_sim_runner.coach.gate import Gate
from career_sim_runner.models import TokenUsage
from career_sim_runner.paths import repo_root
from career_sim_runner.ws_client import StepDriveResult


def launch(gate: Gate):
    # No inherited console/PTY: the live stream must outlive one coach CLI call.
    with (gate.path.with_suffix(".worker.log")).open("ab") as log:
        child = subprocess.Popen(
            [sys.executable, "-m", "career_sim_runner.coach.worker", str(gate.path)],
            cwd=repo_root(), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True,
        )
    gate.update(worker_pid=child.pid)


async def retire(gate: Gate):
    gate.revoke()
    # The worker observes revocation and closes its own connection. Never kill
    # a recycled PID or the shared Jiuwen service.
    for _ in range(100):
        if gate.read()["worker_status"] in {"new", "stopped"}:
            return
        await asyncio.sleep(0.1)
    raise RuntimeError("Old coach worker has not stopped; refusing to reload or restore")


async def _drive(gate: Gate, timeout_s: float) -> StepDriveResult:
    state = gate.read()
    if state["worker_status"] == "stopped":
        raise RuntimeError("Jiuwen conversation ended; inspect and explicitly reload to recover")
    pid = state.get("worker_pid")
    if state["worker_status"] != "new" and pid:
        try:
            os.kill(pid, 0)
        except ProcessLookupError as exc:
            raise RuntimeError("Coach worker disappeared; inspect before recovery") from exc
    previous = gate.grant()
    if state["worker_status"] == "new":
        launch(gate)
    deadline = asyncio.get_running_loop().time() + timeout_s
    reason = "timeout"
    while asyncio.get_running_loop().time() < deadline:
        state = gate.read()
        if state["sequence"] > previous:
            reason = "action_committed"
            break
        if state["revoked"] or state["worker_status"] == "stopped":
            reason = "agent_stopped_before_action"
            break
        await asyncio.sleep(0.05)
    # Race-safe timeout: close the unused permit under the same lock that
    # admits MCP calls. In-flight calls remain unacknowledged, never retried.
    with gate.transaction() as state:
        state["permit"] = False
        committed = state["sequence"] > previous
        result = state["result"] if committed else None
        snapshot = dict(state)
    output = Path(snapshot["log_dir"])
    return StepDriveResult(
        exit_code=0 if result and result.get("success") else 1,
        termination_reason=reason,
        session_id=snapshot["session_id"] or None,
        action_success=bool(result and result.get("success")), action_result=result,
        token_usage=TokenUsage(), transcript="",
        events_path=Path(snapshot.get("events_log") or output / "events-pending.jsonl"),
        transcript_path=Path(snapshot.get("transcript_log") or output / "transcript-pending.log"),
        new_game_seen=not bool(state.get("existing_game", False)),
    )


async def drive(gate: Gate, timeout_s: float) -> StepDriveResult:
    state = gate.read()
    output = Path(state.get("benchmark_output_dir") or state["log_dir"])
    attempt = benchmark.begin(output, game_session_id=state.get("session_id", ""),
                              agent_session_id=state.get("agent_session_id", ""),
                              **state.get("benchmark_context", {}))
    gate.update(active_attempt=attempt)
    started = time.monotonic()
    try:
        result = await _drive(gate, timeout_s)
    except BaseException as exc:
        benchmark.finish(output, attempt, elapsed_s=time.monotonic() - started,
                         action_success=False, termination_reason=type(exc).__name__, error=str(exc))
        current = gate.read()
        benchmark.refresh(output, Path(current.get("events_log") or output / "events-pending.jsonl"),
                          stopped=current.get("worker_status") == "stopped")
        raise
    elapsed = time.monotonic() - started
    benchmark.finish(output, attempt, stream_request_id=gate.read().get("stream_request_id"))
    benchmark.refresh(output, result.events_path, stopped=gate.read().get("worker_status") == "stopped")
    result.benchmark = benchmark.finish(output, attempt, elapsed_s=elapsed, drive_elapsed_s=elapsed,
                                        game_session_id=result.session_id,
                                        action_success=result.action_success,
                                        termination_reason=result.termination_reason)
    result.benchmark["report_path"] = str(output / "benchmark.md")
    if result.benchmark["token_usage"] is not None:
        result.token_usage = TokenUsage(**result.benchmark["token_usage"])
    return result
