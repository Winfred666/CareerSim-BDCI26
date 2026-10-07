"""Notebook recovery at actual event boundaries, with isolated runtime trees."""

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from career_sim_runner.coach.cli import Checkpoint, CheckpointStore
from career_sim_runner.coach.gate import Gate
from career_sim_runner.coach.notebooks import capture_notebooks, restore_notebooks
from career_sim_runner.emulator_adapter import EmulatorAdapter


def write(root, name, content):
    path = root / "observe-decide-review" / "notebooks" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_restore_rewinds_all_notebooks_and_removes_future_files(tmp_path):
    root = tmp_path / "runtime"
    for name, content in {
        "redline-guardian-keeps.tsv": b"00004\tH5\tpending\n",
        "other-evidence-keeps.tsv": b"00003\tR3\n",
        "promotion-signal-researcher-keeps.tsv": b"00004\tok\n",
        "event-translator-dictionary.tsv": b"learned at event 4\n",
        "stat-cap.tsv": b"S5\n",
        "session.json": b'{"session_id":"same-game"}\n',
        "nested/evidence.bin": b"\x00\xff",
    }.items():
        write(root, name, content)
    snapshot = capture_notebooks(root)
    skill_doc = root / "observe-decide-review" / "SKILL.md"
    skill_doc.write_text("new stage policy")
    write(root, "redline-guardian-keeps.tsv", b"00006\tH1\n")
    write(root, "event-translator-dictionary.tsv", b"future learning\n")
    write(root, "future/00005.md", b"future record")
    (root / "observe-decide-review/notebooks/stat-cap.tsv").unlink()
    restore_notebooks(root, snapshot)
    assert capture_notebooks(root) == snapshot
    assert skill_doc.read_text() == "new stage policy"
    assert not (root / "observe-decide-review/notebooks/future").exists()


def test_restore_verification_allows_removed_empty_builtin_but_keeps_data_strict(tmp_path):
    from career_sim_runner.coach.notebooks import verify_notebooks
    write(tmp_path, 'session.json', b'game')
    snapshot = capture_notebooks(tmp_path) | {'skill-creator': {}}
    verify_notebooks(tmp_path, snapshot)
    with pytest.raises(RuntimeError, match='missing-skill'):
        verify_notebooks(tmp_path, {'missing-skill': {'state.json': 'eA=='}})
    future = tmp_path / 'skill-creator/notebooks/future.tsv'
    future.parent.mkdir(parents=True)
    future.write_text('future')
    with pytest.raises(RuntimeError, match='skill-creator'):
        verify_notebooks(tmp_path, snapshot)


def test_native_team_link_captures_and_restores_installed_notebooks(tmp_path, monkeypatch):
    from career_sim_runner.coach import notebooks

    installed = tmp_path / "installed" / "skills"
    runtime = tmp_path / "team" / "skills"
    runtime.mkdir(parents=True)
    write(installed, ".workflow.lock", b"locked")
    (runtime / "observe-decide-review").symlink_to(installed / "observe-decide-review")
    monkeypatch.setattr(notebooks, "load_active_install", lambda: SimpleNamespace(skill_dir=str(installed)))

    snapshot = capture_notebooks(runtime)
    write(installed, "workflow-state.json", b"later")
    restore_notebooks(runtime, snapshot)

    assert capture_notebooks(runtime) == snapshot
    assert not (installed / "observe-decide-review/notebooks/workflow-state.json").exists()


def test_native_team_link_cannot_read_unrelated_directory(tmp_path, monkeypatch):
    from career_sim_runner.coach import notebooks

    installed = tmp_path / "installed" / "skills"
    unrelated = tmp_path / "unrelated" / "observe-decide-review"
    runtime = tmp_path / "team" / "skills"
    runtime.mkdir(parents=True)
    (unrelated / "notebooks").mkdir(parents=True)
    (runtime / "observe-decide-review").symlink_to(unrelated)
    monkeypatch.setattr(notebooks, "load_active_install", lambda: SimpleNamespace(skill_dir=str(installed)))

    with pytest.raises(ValueError, match="Skill escapes runtime skill directory"):
        capture_notebooks(runtime)
    with pytest.raises(ValueError, match="Skill escapes runtime skill directory"):
        restore_notebooks(runtime, {"observe-decide-review": {"session.json": "eA=="}})


def test_native_team_link_cannot_read_notebook_link_outside_skill(tmp_path, monkeypatch):
    from career_sim_runner.coach import notebooks

    installed = tmp_path / "installed" / "skills"
    runtime = tmp_path / "team" / "skills"
    runtime.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("private")
    notebook = installed / "observe-decide-review" / "notebooks"
    notebook.mkdir(parents=True)
    (notebook / "outside.json").symlink_to(outside)
    (runtime / "observe-decide-review").symlink_to(installed / "observe-decide-review")
    monkeypatch.setattr(notebooks, "load_active_install", lambda: SimpleNamespace(skill_dir=str(installed)))

    with pytest.raises(ValueError, match="Notebook escapes runtime skill directory"):
        capture_notebooks(runtime)


@pytest.mark.parametrize("bad_name", ["../outside", "/outside"])
def test_invalid_snapshot_cannot_modify_runtime(tmp_path, bad_name):
    write(tmp_path, "keep.tsv", b"original")
    before = capture_notebooks(tmp_path)
    with pytest.raises(ValueError, match="Invalid notebook path"):
        restore_notebooks(tmp_path, {"observe-decide-review": {bad_name: "eA=="}})
    assert capture_notebooks(tmp_path) == before


def test_notebook_deltas_survive_arbitrary_withdrawal_and_replay(tmp_path):
    store = CheckpointStore(tmp_path / "game.db", "game")
    snapshots = []
    for event in range(1, 8):
        write(tmp_path / "runtime", "redline.tsv", f"{event-1:05d}\n".encode())
        write(tmp_path / "runtime", "dictionary.tsv", b"unchanged large dictionary")
        snapshot = capture_notebooks(tmp_path / "runtime")
        snapshots.append(snapshot)
        store.append(Checkpoint("game", str(event), {"n": event}, [], "hash", "player", before_notebooks=snapshot))
    document = json.loads(store.path.read_text())
    assert "before_notebooks" not in document["entries"][5]
    encoded_dictionary = base64.b64encode(b"unchanged large dictionary").decode()
    assert store.path.read_text().count(encoded_dictionary) == 1
    assert [record["before_notebooks"] for record in store.records()] == snapshots
    target = store.withdraw(6)
    assert target.before_notebooks == snapshots[5]
    assert len(store.records()) == 5
    store.append(target)
    assert store.records()[5]["before_notebooks"] == snapshots[5]


@pytest.mark.asyncio
async def test_missing_snapshot_refuses_withdrawal_before_rewinding(tmp_path, monkeypatch):
    from argparse import Namespace
    from career_emulator.server.models import GameSession
    from career_emulator.storage import SessionStore
    from career_sim_runner.coach import cli as coach

    db = tmp_path / "game.db"
    session = GameSession(session_id="game")
    await SessionStore(db, tmp_path / "logs").save_session(session)
    store = CheckpointStore(db, "game")
    store.append(Checkpoint("game", "now", {}, [], coach._json_hash(session.to_dict()), "player", before_notebooks=None))
    coach._register_coach_session(db, "player", "game", "player", "")
    monkeypatch.setattr(coach, "resolve_instance_ws_url", lambda: "ws://unused")
    monkeypatch.setattr(coach, "_resolve_mode", lambda: "agent")
    monkeypatch.setattr(coach, "_coach_emulator_log_dir", lambda _db: tmp_path / "logs")

    async def unexpected_rewind(*_args, **_kwargs):
        pytest.fail("Incomplete checkpoints must not rewind Jiuwen")

    monkeypatch.setattr(coach, "rewind_agent_session", unexpected_rewind)
    with pytest.raises(RuntimeError, match="no runtime notebook snapshot"):
        await coach._withdraw(Namespace(db=str(db), session_id="player"))
    assert len(store.records()) == 1
    assert await coach.read_session_payload(db, "game") == session.to_dict()


@pytest.mark.asyncio
async def test_capture_waits_for_previous_review_and_precedes_observation(tmp_path, monkeypatch):
    from career_sim_runner import emulator_adapter

    root = tmp_path / "runtime"
    write(root, "redline.tsv", b"00003\n")
    monkeypatch.setattr(emulator_adapter, "runtime_skills_dir", lambda _player: root)
    gate = Gate(tmp_path / "gate.db")
    gate.create("game", agent_session_id="player", notebook_checkpoints=True)

    class Response:
        def to_mcp_dict(self):
            return {"events": "feedback"}

        def to_dict(self):
            return {"success": True}

    class Engine:
        async def _get_promotion_applicator(self):
            pass

        async def observe(self, _session):
            return Response()

        async def take_action(self, *_args):
            return Response()

    adapter = EmulatorAdapter(Engine(), tmp_path / "observations", gate)
    pending = asyncio.create_task(adapter.observe("game"))
    try:
        await asyncio.sleep(0.1)
        assert "before_notebooks" not in gate.read()
        # The previous event's reviewer runs after its action was checkpointed.
        write(root, "redline.tsv", b"00004\n")
        expected = capture_notebooks(root)
        gate.grant()
        await asyncio.wait_for(pending, 1)
        first_boundary = gate.read()["observe_boundary"]
        write(root, "dictionary.tsv", b"current observation learning")
        await adapter.observe("game")
        assert gate.read()["observe_boundary"] == first_boundary
        await adapter.take_action("game", 1, "00005")
        assert gate.read()["before_notebooks"] == expected
        assert gate.read()["sequence"] == 1
        # The next boundary includes the completed current review.
        write(root, "redline.tsv", b"00005\n")
        expected_next = capture_notebooks(root)
        gate.acknowledge()
        gate.grant()
        await adapter.observe("game")
        assert gate.read()["before_notebooks"] == expected_next
        assert gate.read()["observe_boundary"] != first_boundary
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['agent', 'team'])
async def test_checkpointed_action_requires_fresh_observe(tmp_path, monkeypatch, mode):
    from career_sim_runner import emulator_adapter

    root = tmp_path / 'runtime'
    write(root, 'redline.tsv', b'00001\n')
    monkeypatch.setattr(emulator_adapter, 'runtime_skills_dir', lambda _player: root)
    gate = Gate(tmp_path / 'gate.db')
    gate.create('game', agent_session_id='player', notebook_checkpoints=True, mode=mode)

    class Response:
        def to_mcp_dict(self): return {'events': ''}
        def to_dict(self): return {'success': True}

    class Engine:
        actions = 0
        async def _get_promotion_applicator(self): return None
        async def observe(self, _session): return Response()
        async def take_action(self, *_args):
            self.actions += 1
            return Response()

    engine = Engine()
    adapter = EmulatorAdapter(engine, tmp_path / 'observations', gate)
    for count in range(2):
        gate.grant()
        before = gate.read()
        with pytest.raises(RuntimeError, match='call observe before take_action'):
            await adapter.take_action('game', 1, 'stale menu')
        assert gate.read() == before
        assert engine.actions == count
        await adapter.observe('game')
        boundary = gate.read()['observe_boundary']
        await adapter.observe('game')
        assert gate.read()['observe_boundary'] == boundary
        await adapter.take_action('game', 1, 'current menu')
        assert engine.actions == count + 1
        assert gate.read()['sequence'] == count + 1
        gate.acknowledge()
