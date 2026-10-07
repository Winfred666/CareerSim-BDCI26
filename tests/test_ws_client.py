"""Tests for headless JiuwenSwarm websocket helpers."""

import json
from pathlib import Path

import pytest

from career_sim_runner.models import InstallRecord
from career_sim_runner.skill_contract import SubmissionError
from career_sim_runner.ws_client import (
    POST_ACTION_GRACE_S,
    _build_envelope,
    build_play_prompt,
    cancel_agent_session,
    drive,
    drive_one_event,
    delete_agent_session,
    pause_agent_session,
    reload_agent_config,
    resolve_run_mode,
)


def test_post_action_grace_allows_async_reviewer() -> None:
    """Team Reviewer must have more than the old 30-second drain window."""
    assert POST_ACTION_GRACE_S >= 90


def _install_record(*, mode: str = "agent", instruction: str = "") -> InstallRecord:
    """Return an install record fixture for websocket tests."""
    return InstallRecord(
        submission_dir="/tmp/submission",
        submission_name="fixture-team",
        skill_dir="/tmp/skills",
        skill_name="submission-skills",
        manifest={
            "mode": mode,
            "instruction": instruction,
            "participant_skill_names": ["monthly-action-decision", "report-generation"],
        },
    )


def test_build_play_prompt() -> None:
    """Play prompt is now static; skills/instruction live in IDENTITY.md."""
    prompt = build_play_prompt()
    static_prompt_file = Path(__file__).parent.with_name("career_sim_runner") / "prompts" / "play_headless.md"
    assert prompt.strip() == static_prompt_file.read_text(encoding="utf-8").strip()


def test_resolve_run_mode_uses_manifest_value() -> None:
    """Supported manifest modes should flow through to the websocket payload."""
    assert resolve_run_mode(_install_record(mode="team")) == "team"


def test_resolve_run_mode_rejects_unsupported() -> None:
    """Unsupported manifest modes should raise SubmissionError."""
    with pytest.raises(SubmissionError):
        resolve_run_mode(_install_record(mode="unsupported-mode"))


def test_build_envelope_uses_selected_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Envelope generation should send the resolved JiuwenSwarm mode."""
    monkeypatch.setenv("MODEL_NAME", "gpt-5.6-terra")
    envelope = _build_envelope("prompt", "drive-session", "agent")
    assert envelope["params"]["mode"] == "agent"
    assert envelope["params"]["work_mode"] == "work"
    assert envelope["params"]["model_name"] == "deepseek-flash"


@pytest.mark.asyncio
async def test_reload_updates_running_service_model_environment(monkeypatch):
    sent = []

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def send(self, raw):
            sent.append(json.loads(raw))

        async def recv(self):
            if not sent:
                return "{}"
            return json.dumps({"request_id": sent[-1]["request_id"], "body": {"result": {"reloaded": True}}})

    monkeypatch.setattr("career_sim_runner.ws_client.websockets.connect", lambda *_args, **_kwargs: Socket())
    assert await reload_agent_config("ws://test")
    assert sent[0]["params"]["env"] == {"MODEL_NAME": "deepseek-flash"}


@pytest.mark.asyncio
async def test_pause_agent_session_targets_same_team_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Coach recovery parks, rather than replaces, the Jiuwen team session."""
    calls = []

    async def fake_request(ws_url, method, session_id, params, *, timeout_s):
        calls.append((ws_url, method, session_id, params, timeout_s))
        return {"success": True}

    monkeypatch.setattr("career_sim_runner.ws_client._session_request", fake_request)

    result = await pause_agent_session(
        "ws://coach", "same-agent", mode="team", timeout_s=7.0
    )

    assert result == {"success": True}
    assert calls == [
        (
            "ws://coach",
            "chat.interrupt",
            "same-agent",
            {"intent": "pause", "mode": "team"},
            7.0,
        )
    ]


@pytest.mark.asyncio
async def test_cancel_agent_session_stops_withdrawn_team_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Withdrawal cancels work that will be discarded with the team runtime."""
    calls = []

    async def fake_request(ws_url, method, session_id, params, *, timeout_s):
        calls.append((ws_url, method, session_id, params, timeout_s))
        return {"success": True}

    monkeypatch.setattr("career_sim_runner.ws_client._session_request", fake_request)

    result = await cancel_agent_session(
        "ws://coach", "same-agent", mode="team", timeout_s=7.0
    )

    assert result == {"success": True}
    assert calls == [
        (
            "ws://coach",
            "chat.interrupt",
            "same-agent",
            {"intent": "cancel", "mode": "team"},
            7.0,
        )
    ]


@pytest.mark.asyncio
async def test_delete_agent_session_clears_same_team_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Withdraw can clear persisted team state without changing the player id."""
    calls = []

    async def fake_request(ws_url, method, session_id, params, *, timeout_s):
        calls.append((ws_url, method, session_id, params, timeout_s))
        return {"session_id": session_id}

    monkeypatch.setattr("career_sim_runner.ws_client._session_request", fake_request)

    result = await delete_agent_session("ws://coach", "same-agent", timeout_s=7.0)

    assert result == {"session_id": "same-agent"}
    assert calls == [
        (
            "ws://coach",
            "session.delete",
            "same-agent",
            {"session_id": "same-agent"},
            7.0,
        )
    ]


@pytest.mark.asyncio
async def test_drive_one_event_stops_after_first_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The coach drive drains one successful action without sending continuation."""

    class FakeSocket:
        def __init__(self) -> None:
            self.frames = [
                json.dumps({"hello": True}),
                {
                    "event_type": "chat.tool_result",
                    "tool_name": "mcp_career-emulator_take_action",
                    "tool_call_id": "action-1",
                    "raw_output": {"result": json.dumps({"success": True})},
                },
                {
                    "response_kind": "e2a.complete",
                    "status": "succeeded",
                    "is_final": True,
                },
            ]
            self.sent: list[str] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send(self, message: str) -> None:
            self.sent.append(message)

        async def recv(self):
            frame = self.frames.pop(0)
            return frame if isinstance(frame, str) else json.dumps(frame)

    socket = FakeSocket()

    def connect(*_args, **_kwargs):
        return socket

    monkeypatch.setattr("career_sim_runner.ws_client.websockets.connect", connect)
    with pytest.raises(RuntimeError, match="execution gate"):
        await drive_one_event("ws://coach", "one step", "drive-1", "agent", 2, tmp_path)
    assert socket.sent == []


@pytest.mark.asyncio
async def test_month_limit_preserves_review_and_bounds_continuations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop on a confirmed month boundary after draining the review and final frame."""

    def observe(month: int) -> dict:
        return {
            "event_type": "chat.tool_result",
            "tool_name": "mcp_career-emulator_observe",
            "raw_output": {
                "result": json.dumps(
                    {
                        "current_state": {
                            "session_id": "game-1",
                            "time": {"current_month": month, "current_year": 1},
                        }
                    }
                )
            },
        }

    final = {"response_kind": "e2a.complete", "status": "succeeded", "is_final": True}

    class FakeSocket:
        def __init__(self) -> None:
            self.frames = [
                {"hello": True},
                observe(1),
                {"body": {"text": "MONTH_TEST_COMPLETE"}},
                final,
                observe(2),
                {
                    "event_type": "chat.tool_result",
                    "tool_name": "send_message",
                    "role": "event-reviewer",
                    "result": "结果复核：unchanged",
                },
                final,
            ]
            self.sent: list[str] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send(self, message: str) -> None:
            self.sent.append(message)

        async def recv(self):
            return json.dumps(self.frames.pop(0))

    socket = FakeSocket()
    monkeypatch.setattr("career_sim_runner.ws_client.websockets.connect", lambda *_a, **_k: socket)
    result = await drive("ws://test", "play", "drive-test", "team", 2, tmp_path, stop_after_month=1)
    assert result.exit_code == 0
    assert result.termination_reason == "month_limit"
    assert len(socket.sent) == 2
    assert all("第 1 月" in json.loads(message)["params"]["content"] for message in socket.sent)
    assert socket.frames == []
    assert "event-reviewer" in result.events_path.read_text(encoding="utf-8")
