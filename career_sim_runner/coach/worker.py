"""Own one WebSocket for a complete supervised run, across CLI step calls."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import websockets

from career_sim_runner.coach.benchmark import refresh, render_events, runtime_dir
from career_sim_runner.coach.gate import Gate
from career_sim_runner.transcript import StreamCollector
from career_sim_runner.ws_client import build_chat_envelope


async def run(gate: Gate):
    state = gate.read()
    output = Path(state["log_dir"])
    benchmark_output = Path(state.get("benchmark_output_dir") or output)
    envelope = build_chat_envelope(state["prompt"], state["agent_session_id"], state["mode"])
    collector = StreamCollector(log_dir=benchmark_output, events_path=benchmark_output / "events.jsonl",
                                transcript_path=runtime_dir(benchmark_output) / "transcript.log",
                                agent_session_id=state["agent_session_id"], request_id=envelope["request_id"],
                                event_context=lambda: {"attempt": gate.read().get("active_attempt")})
    dirty = asyncio.Event()
    stopping = False

    def update_reports():
        refresh(benchmark_output, collector.events_path)
        render_events(collector.events_path, benchmark_output)

    async def report_changes():
        while True:
            await dirty.wait()
            dirty.clear()
            await asyncio.to_thread(update_reports)
            if stopping:
                return

    # Cumulative report rendering must not block socket reads or heartbeats.
    collector.on_event = lambda event: dirty.set()
    reporter = asyncio.create_task(report_changes())
    gate.update(worker_status="connecting", worker_pid=os.getpid(), stream_request_id=envelope["request_id"])
    try:
        # Jiuwen can spend longer than the WebSocket default pong deadline in a
        # blocking teammate/tool call. The per-event coach timeout still bounds
        # a stalled run, so keep the stream open until that controller decides.
        async with websockets.connect(
            state["ws_url"], max_size=None, open_timeout=10, ping_timeout=None,
        ) as ws:
            try:
                await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                pass
            await ws.send(
                json.dumps(
                    envelope,
                    ensure_ascii=False,
                )
            )
            gate.update(worker_status="running")
            while not gate.read()["revoked"]:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                frame = raw if isinstance(raw, dict) else json.loads(raw)
                if not collector.feed_frame(frame):
                    continue  # A foreign final frame must not stop this worker.
                # Make the human transcript readable while the same stream stays open.
                collector.finalize()
                gate.update(events_log=str(collector.events_path), transcript_log=str(collector.transcript_path))
                if frame.get("is_final") and frame.get("response_kind") in {"e2a.complete", "e2a.error"}:
                    break
    except Exception as exc:
        gate.update(worker_error=f"{type(exc).__name__}: {exc}")
    finally:
        collector.finalize()
        stopping = True
        dirty.set()
        await reporter
        assert collector.events_path is not None
        refresh(benchmark_output, collector.events_path, stopped=True)
        gate.update(worker_status="stopped", events_log=str(collector.events_path),
                    transcript_log=str(collector.transcript_path))


if __name__ == "__main__":
    asyncio.run(run(Gate(Path(sys.argv[1]))))
