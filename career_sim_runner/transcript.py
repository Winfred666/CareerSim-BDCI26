"""Reassemble JiuwenSwarm WebSocket streams into readable logs."""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from career_sim_runner.models import TokenUsage

EventCallback = Callable[[dict[str, Any]], None]


def _walk(obj: Any) -> list[dict[str, Any]]:
    """Walk nested JSON-like data and collect dictionaries."""
    found: list[dict[str, Any]] = []
    stack: list[Any] = [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            found.append(node)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def _extract_text(frame: dict[str, Any]) -> str:
    """Extract visible text-like payload fragments from one frame.

    E2A ``e2a.chunk`` frames with ``delta_kind=reasoning`` are skipped so
    reasoning content is not written into the transcript.
    """
    for node in _walk(frame):
        if node.get("delta_kind") == "reasoning":
            continue
        for key in ("text", "content", "delta"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value
    body = frame.get("body")
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        return _extract_text(body)
    return ""


@dataclass
class StreamCollector:
    """Structured collector for one agent session."""

    log_dir: Path
    on_event: EventCallback | None = None
    totals: TokenUsage = field(default_factory=TokenUsage)
    transcript: str = ""
    events_path: Path | None = None
    transcript_path: Path | None = None
    agent_session_id: str | None = None
    request_id: str | None = None
    event_context: Callable[[], dict[str, Any]] | None = None
    _text_buffer: str = ""
    _event_count: int = 0
    _incremental_usage_seen: bool = False
    _usage_ids_seen: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        """Initialize log file paths."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.events_path = self.events_path or self.log_dir / f"events-{stamp}.jsonl"
        self.transcript_path = self.transcript_path or self.log_dir / f"transcript-{stamp}.log"
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        if self.events_path.is_file():
            with self.events_path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                        self._event_count = max(self._event_count, int(record.get("seq", 0)))
                        usage_id = str(record.get("usage_id") or "")
                        if usage_id:
                            self._usage_ids_seen.add(usage_id)
                    except (ValueError, TypeError):
                        continue
            # A crashed writer may leave a partial last line. Preserve it as
            # evidence, but ensure a new record starts on its own line.
            if self.events_path.stat().st_size:
                with self.events_path.open("rb+") as handle:
                    handle.seek(-1, 2)
                    if handle.read(1) != b"\n":
                        handle.write(b"\n")

    def _append_event(self, kind: str, payload: dict[str, Any]) -> None:
        """Write one structured event."""
        self._event_count += 1
        record = {
            "seq": self._event_count,
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            **payload,
        }
        if self.agent_session_id:
            record["agent_session_id"] = self.agent_session_id
        if self.request_id:
            record["request_id"] = self.request_id
        if self.event_context:
            record.update(self.event_context())
        assert self.events_path is not None
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        if self.on_event is not None:
            self.on_event(record)

    def _absorb_usage(self, usage: dict[str, Any], model: str) -> None:
        """Add one usage block into running totals."""
        bucket = self.totals.by_model.setdefault(
            model,
            {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "total_cost": 0.0},
        )
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            value = int(usage.get(key, 0) or 0)
            setattr(self.totals, key, getattr(self.totals, key) + value)
            bucket[key] += value
        cost = float(usage.get("total_cost") or 0)
        self.totals.total_cost += cost
        bucket["total_cost"] += cost

    def _flush_text(self, force: bool = False) -> None:
        """Flush buffered text into the transcript file."""
        if not self._text_buffer:
            return
        if not force and "\n" not in self._text_buffer and len(self._text_buffer) < 120:
            return
        chunk = self._text_buffer
        self._text_buffer = ""
        self.transcript += chunk
        assert self.transcript_path is not None
        with self.transcript_path.open("a", encoding="utf-8") as handle:
            handle.write(chunk)

    def feed_frame(self, frame: dict[str, Any]) -> bool:
        """Ingest one decoded WebSocket frame."""
        if self.request_id:
            incoming = frame.get("request_id")
            session = frame.get("session_id")
            if incoming != self.request_id or (session and session != self.agent_session_id):
                self._append_event("stream_mismatch", {
                    "received_request_id": incoming,
                    "received_session_id": session,
                })
                return False
        kind = str(frame.get("response_kind") or "")
        status = str(frame.get("status") or "")

        for node in _walk(frame):
            event_type = node.get("event_type")
            # E2A envelopes repeat event_type on the wrapper; only consume
            # the inner logical event, or every model call is counted twice.
            if isinstance(node.get("delta"), dict) and node["delta"].get("event_type") == event_type:
                continue
            if event_type in {"chat.llm_usage", "chat.usage_metadata", "chat.usage_summary"}:
                incremental = event_type != "chat.usage_summary"
                metadata = node.get("metadata") or node
                usage = metadata.get("usage_metadata") if incremental else node.get("usage")
                if not isinstance(usage, dict):
                    continue
                usage_id = str(metadata.get("usage_id") or node.get("usage_id") or "")
                if usage_id and usage_id in self._usage_ids_seen:
                    continue
                model = str(usage.get("model_name") or node.get("model") or "unknown")
                actor = str(
                    node.get("member_name") or node.get("role")
                    or metadata.get("member_name") or metadata.get("role")
                    or "unknown"
                )
                # Full-conversation summaries are evidence, never an additional
                # charge on top of individual calls.
                if incremental or not self._incremental_usage_seen:
                    self._absorb_usage(usage, model)
                if incremental:
                    self._incremental_usage_seen = True
                if usage_id:
                    self._usage_ids_seen.add(usage_id)
                self._append_event("usage" if incremental else "usage_summary", {
                    "model": model, "actor": actor, "usage": usage,
                    "usage_id": usage_id or None,
                    "run_id": metadata.get("run_id") or node.get("run_id"),
                    "source": event_type, "payload": node,
                })
            elif event_type in {"chat.tool_call", "chat.tool_result"}:
                self._flush_text(force=True)
                self._append_event(str(event_type).replace("chat.", ""), {"payload": node})

        text = _extract_text(frame)
        if text:
            self._text_buffer += text

        if frame.get("is_final") or kind in {"e2a.complete", "e2a.error"}:
            self._flush_text(force=True)
            self._append_event(
                "frame_final",
                {"response_kind": kind, "status": status, "is_final": bool(frame.get("is_final"))},
            )
        elif "\n" in self._text_buffer:
            self._flush_text(force=True)
        return True

    def finalize(self) -> None:
        """Flush remaining buffered text."""
        self._flush_text(force=True)
