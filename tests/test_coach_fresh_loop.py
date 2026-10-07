"""A disposable context must never authorize or discard an in-flight action."""
import asyncio
import json
import sqlite3
import shutil
import threading
import time
from argparse import Namespace
from pathlib import Path
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import websockets

from career_sim_runner.coach import fresh_loop


def test_retired_adapter_cleanup_cannot_signal_another_game(monkeypatch, tmp_path):
    control = tmp_path / 'owned.sqlite3'
    proc = tmp_path / 'proc'
    for pid, module, gate in [(11, 'career_sim_runner.emulator_adapter', str(control.resolve())),
                              (12, 'career_sim_runner.emulator_adapter', '/other/game.sqlite3'),
                              (13, 'other.module', str(control.resolve()))]:
        p = proc / str(pid)
        p.mkdir(parents=True)
        (p / 'cmdline').write_bytes(b'python\0-m\0' + module.encode() + b'\0')
        (p / 'environ').write_bytes(b'CAREER_COACH_GATE=' + gate.encode() + b'\0')
    killed = []
    monkeypatch.setattr(fresh_loop.os, 'kill', lambda pid, sig: killed.append((pid, sig)))
    fresh_loop.close_retired_adapters(control, proc)
    assert killed == [(11, signal.SIGTERM)]


@pytest.mark.asyncio
async def test_explicit_recovery_refuses_a_player_still_running(monkeypatch, tmp_path):
    journal = tmp_path / 'fresh-loop.json'
    journal.write_text('{"game":"game"}')
    monkeypatch.setattr(fresh_loop.cli, '_load_state', lambda _: {'benchmark_output_dir': str(tmp_path)})
    monkeypatch.setattr(fresh_loop.benchmark, 'runtime_dir', lambda _: tmp_path)
    async def resolve(*_):
        return {'game_session_id': 'game', 'agent_session_id': 'current'}
    monkeypatch.setattr(fresh_loop.cli, '_resolve_context', resolve)
    control = tmp_path / 'control'
    control.touch()
    monkeypatch.setattr(fresh_loop, 'Gate', lambda _: type('Active', (), {
        'path': control, 'read': lambda _: {'worker_status': 'running'}})())
    args = Namespace(command='recover', db=str(tmp_path / 'db'), player='player', ws_url='ws://invalid')
    with pytest.raises(RuntimeError, match='stopped, acknowledged'):
        await fresh_loop.run(args)


@pytest.mark.parametrize("state", [
    {"busy": True, "permit": False, "sequence": 1, "acknowledged": 1},
    {"busy": False, "permit": True, "sequence": 1, "acknowledged": 1},
    {"busy": False, "permit": False, "sequence": 2, "acknowledged": 1},
])
def test_retirement_refuses_unsettled_action(monkeypatch, tmp_path, state):
    control = tmp_path / "control"
    control.touch()

    class UnsafeGate:
        def __init__(self, _):
            self.path = control

        def read(self):
            return state

        def revoke(self):
            pytest.fail("Must preserve the in-flight action")

    monkeypatch.setattr(fresh_loop, "Gate", UnsafeGate)
    with pytest.raises(RuntimeError, match="acknowledged"):
        asyncio.run(fresh_loop.stop_session(tmp_path / "db", "ws://invalid", "sid", output=tmp_path))


def test_rotation_refuses_changed_template_before_any_rpc(monkeypatch, tmp_path):
    journal = tmp_path / "fresh-loop.json"
    journal.write_text('{"phase":"active","game":"game","template_session":"template","history_hash":"immutable"}')
    monkeypatch.setattr(fresh_loop.cli, "_load_state", lambda _: {"benchmark_output_dir": str(tmp_path)})
    monkeypatch.setattr(fresh_loop.benchmark, "runtime_dir", lambda _: tmp_path)

    async def resolve(*_):
        return {"game_session_id": "game", "agent_session_id": "current", "seed": "seed"}

    monkeypatch.setattr(fresh_loop.cli, "_resolve_context", resolve)
    monkeypatch.setattr(fresh_loop.cli, "read_history", lambda *_: [{"role": "user", "content": "changed"}])

    async def forbid_rpc(*_, **__):
        pytest.fail("Template corruption must be detected before retirement or fork")

    monkeypatch.setattr(fresh_loop, "_session_request", forbid_rpc)
    args = Namespace(command="rotate", db=str(tmp_path / "db"), player="player", ws_url="ws://invalid")
    with pytest.raises(RuntimeError, match="Immutable"):
        asyncio.run(fresh_loop.run(args))


def _roster_database(path):
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE team_info(team_name TEXT PRIMARY KEY,leader_member_name TEXT)")
        connection.execute("CREATE TABLE team_member(team_name TEXT,member_name TEXT,status TEXT,execution_status TEXT,prompt TEXT)")
        connection.execute("CREATE TABLE messages(team_name TEXT,body TEXT)")
        connection.execute("INSERT INTO team_info VALUES('team','leader')")
        for name in ['leader', 'analyse-o', 'analyse-n', 'analyse-s', 'analyse-hw', 'analyse-r']:
            connection.execute("INSERT INTO team_member VALUES('team',?,'ready','idle','fixed instruction')", (name,))
        connection.execute("INSERT INTO messages VALUES('team','previous event history')")


def test_roster_fixture_excludes_history_and_is_idempotent(tmp_path):
    database = tmp_path / "team.db"
    _roster_database(database)
    roster = fresh_loop.capture_roster(database, "team")
    assert set(roster) == {'team_info', 'team_member'}
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM team_info")
        connection.execute("DELETE FROM team_member")
        connection.execute("DELETE FROM messages")
    fresh_loop.restore_roster(database, "team", roster)
    fresh_loop.restore_roster(database, "team", roster)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM team_member WHERE status='stopped' AND execution_status='idle'").fetchone()[0] == 6
        connection.execute("UPDATE team_member SET prompt='changed' WHERE member_name='analyse-o'")
    with pytest.raises(RuntimeError, match="refusing to merge"):
        fresh_loop.restore_roster(database, "team", roster)


def test_roster_schema_failure_rolls_back_all_configuration(tmp_path):
    database = tmp_path / "team.db"
    _roster_database(database)
    roster = fresh_loop.capture_roster(database, "team")
    roster['team_member'][0]['unexpected'] = 'schema drift'
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM team_info")
        connection.execute("DELETE FROM team_member")
    with pytest.raises(RuntimeError, match="schema"):
        fresh_loop.restore_roster(database, "team", roster)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM team_info").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM team_member").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('existing', [False, True])
async def test_auto_rotation_prepares_once_then_only_rotates(monkeypatch, tmp_path, existing):
    flow = tmp_path / 'skills/observe-decide-review/notebooks/workflow-state.json'
    flow.parent.mkdir(parents=True)
    flow.write_text('{"phase":"reviewed"}')
    if existing:
        (tmp_path / 'fresh-loop.json').write_text('{}')
    monkeypatch.setattr(fresh_loop.cli, '_load_state', lambda _: {'benchmark_output_dir': str(tmp_path)})
    monkeypatch.setattr(fresh_loop.benchmark, 'runtime_dir', lambda _: tmp_path)
    monkeypatch.setattr(fresh_loop.cli, '_runtime_skills_dir', lambda _: tmp_path / 'skills')

    async def resolve(*_):
        return {'agent_session_id': 'current'}

    monkeypatch.setattr(fresh_loop.cli, '_resolve_context', resolve)
    monkeypatch.setattr(fresh_loop.cli, 'read_history', lambda *_: [{'event_type': 'chat.tool_call'}])
    monkeypatch.setattr(fresh_loop, 'Gate', lambda _: None)
    calls = []

    async def run(args):
        calls.append(args.command)
        return {'warm_session': 'warm'} if args.command == 'prepare' else {'agent_session_id': 'new'}

    monkeypatch.setattr(fresh_loop, 'run', run)
    result = await fresh_loop.next_event(tmp_path / 'db', 'player', 'ws://invalid')
    assert calls == (['rotate'] if existing else ['prepare', 'freeze', 'rotate'])
    assert result['agent_session_id'] == 'new'


@pytest.mark.asyncio
async def test_auto_rotation_does_not_resume_a_player_that_exited_before_review(monkeypatch, tmp_path):
    flow = tmp_path / 'skills/observe-decide-review/notebooks/workflow-state.json'
    flow.parent.mkdir(parents=True)
    flow.write_text('{"phase":"decide_running"}')
    monkeypatch.setattr(fresh_loop.cli, '_load_state', lambda _: {'benchmark_output_dir': str(tmp_path)})
    monkeypatch.setattr(fresh_loop.benchmark, 'runtime_dir', lambda _: tmp_path)
    monkeypatch.setattr(fresh_loop.cli, '_runtime_skills_dir', lambda _: tmp_path / 'skills')

    async def resolve(*_):
        return {'agent_session_id': 'current'}

    monkeypatch.setattr(fresh_loop.cli, '_resolve_context', resolve)
    monkeypatch.setattr(fresh_loop, 'Gate', lambda _: type('Stopped', (), {
        'read': lambda _: {'worker_status': 'stopped'}})())

    async def forbidden(*_):
        pytest.fail('An exited Player must not be resumed or replaced')

    monkeypatch.setattr(fresh_loop, 'run', forbidden)
    with pytest.raises(RuntimeError, match='exited before completing review'):
        await fresh_loop.next_event(tmp_path / 'db', 'player', 'ws://invalid')


def test_native_team_deletion_keeps_scripts_notebooks_and_same_absolute_commands(monkeypatch, tmp_path):
    team = tmp_path / '.agent_teams/team'
    workspace = team / 'team-workspace'
    skills = workspace / 'skills'
    source = Path(__file__).resolve().parents[1] / 'solution/skills/observe-decide-review'
    shutil.copytree(source, skills / source.name)
    notes = skills / source.name / 'notebooks'
    flow = notes / 'workflow-state.json'
    flow.write_text('{"phase":"reviewed","session_id":"game"}')
    learned = notes / 'questionnaire-feedback.json'
    learned.write_text('{"pending":{"N":{"actual":{"N":1}}}}')
    history = team / 'workspaces/previous-event.txt'
    history.parent.mkdir()
    history.write_text('OLD CONVERSATION')
    monkeypatch.setattr(fresh_loop, 'jiuwenswarm_data_dir', lambda: tmp_path)
    saved = {'workspace': str(workspace), 'game': 'game'}
    original = {p.relative_to(skills): p.read_bytes() for p in skills.rglob('*') if p.is_file()}
    fresh_loop.preserve_workspace(saved)
    assert skills.is_symlink()
    assert {p.relative_to(skills): p.read_bytes() for p in skills.rglob('*') if p.is_file()} == original
    for _ in range(2):
        shutil.rmtree(team)  # Native session deletion removes the shared team directory.
        assert Path(saved['persistent_skills']).is_dir()
        fresh_loop.restore_workspace_link(saved)
        fresh_loop.restore_workspace_link(saved)
        assert flow.read_text() == '{"phase":"reviewed","session_id":"game"}'
        assert learned.read_text() == '{"pending":{"N":{"actual":{"N":1}}}}'
        assert (skills / source.name / 'scripts/translate.py').exists()
        assert not history.exists()


def _retirement_fixture(monkeypatch, tmp_path, sid='event'):
    data = tmp_path / 'native'
    session = data / 'agent/sessions' / sid
    session.mkdir(parents=True)
    (session / 'metadata.json').write_text(json.dumps({'team_name': 'team'}))
    team = data / '.agent_teams/team'
    team.mkdir(parents=True, exist_ok=True)
    (team / 'old-context').write_text('discard this event')
    skills = data / 'agent/workspace/skills'
    skills.mkdir(parents=True, exist_ok=True)
    if not (skills / 'workflow-state.json').exists():
        (skills / 'workflow-state.json').write_text('{"phase":"reviewed","session_id":"game"}')
    monkeypatch.setattr(fresh_loop, 'jiuwenswarm_data_dir', lambda: data)
    monkeypatch.setattr(fresh_loop.cli, '_runtime_skills_dir', lambda _: skills)
    monkeypatch.setattr(fresh_loop.benchmark, 'runtime_dir', lambda _: tmp_path / 'private')
    return data, session, team, skills


@pytest.mark.asyncio
async def test_interrupted_retirement_finishes_collection_without_native_session(monkeypatch, tmp_path):
    data, session, team, skills = _retirement_fixture(monkeypatch, tmp_path)
    detached = fresh_loop.detach_retired_team('event', tmp_path)
    assert detached[0].exists() and not team.exists()
    # The native RPC completed, but the controller exited before local collection.
    shutil.rmtree(session)
    gate = fresh_loop.Gate(tmp_path / 'gate')
    gate.create('game')
    gate.update(worker_status='stopped')
    monkeypatch.setattr(fresh_loop, 'gate_path', lambda *_: gate.path)
    monkeypatch.setattr(fresh_loop, 'close_retired_adapters', lambda _: None)
    async def forbidden(*_, **__):
        pytest.fail('A deleted native session must not be recreated or called again')
    monkeypatch.setattr(fresh_loop, 'delete_agent_session', forbidden)
    await fresh_loop.stop_session(tmp_path / 'db', 'ws://unused', 'event', output=tmp_path)
    assert not detached[0].exists()
    assert json.loads(detached[1].read_text())['phase'] == 'disposed'
    assert (skills / 'workflow-state.json').exists()
    await fresh_loop.stop_session(tmp_path / 'db', 'ws://unused', 'event', output=tmp_path)


@pytest.mark.asyncio
async def test_failed_native_delete_retains_quarantine_for_explicit_retry(monkeypatch, tmp_path):
    _, session, team, skills = _retirement_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(fresh_loop, 'close_retired_adapters', lambda _: None)
    calls = 0
    async def delete(*_, **__):
        nonlocal calls
        calls += 1
        assert not team.exists()
        if calls == 1:
            raise TimeoutError('native cleanup failed')
        shutil.rmtree(session)
    monkeypatch.setattr(fresh_loop, 'delete_agent_session', delete)
    with pytest.raises(TimeoutError):
        await fresh_loop.stop_session(tmp_path / 'db', 'ws://unused', 'event', output=tmp_path)
    detached = fresh_loop.detach_retired_team('event', tmp_path)
    assert detached[0].exists() and session.exists()
    # There is no automatic recovery; an explicit second retirement is idempotent.
    await fresh_loop.stop_session(tmp_path / 'db', 'ws://unused', 'event', output=tmp_path)
    assert calls == 2 and not detached[0].exists()
    assert (skills / 'workflow-state.json').exists()


@pytest.mark.parametrize('unsafe', ['session_path', 'team_path', 'team_symlink', 'unpreserved_skills'])
def test_retirement_cannot_move_another_directory_or_game_skills(monkeypatch, tmp_path, unsafe):
    _, session, team, skills = _retirement_fixture(monkeypatch, tmp_path)
    sid = 'event'
    if unsafe == 'session_path':
        sid = '../event'
    elif unsafe == 'team_path':
        (session / 'metadata.json').write_text('{"team_name":"../outside"}')
    elif unsafe == 'team_symlink':
        shutil.rmtree(team)
        team.symlink_to(skills, target_is_directory=True)
    else:
        local_skills = team / 'skills'
        (local_skills / 'observe-decide-review/notebooks').mkdir(parents=True)
        (local_skills / 'observe-decide-review/notebooks/state.json').write_text('{"keep":true}')
        monkeypatch.setattr(fresh_loop.cli, '_runtime_skills_dir', lambda _: local_skills)
    with pytest.raises(RuntimeError):
        fresh_loop.detach_retired_team(sid, tmp_path)
    assert team.exists() and (skills / 'workflow-state.json').exists()


@pytest.mark.asyncio
async def test_native_skill_links_can_be_retired_without_deleting_shared_notebooks(monkeypatch, tmp_path):
    _, _, team, skills = _retirement_fixture(monkeypatch, tmp_path)
    installed = skills / 'observe-decide-review'
    (installed / 'notebooks').mkdir(parents=True)
    (installed / 'notebooks/state.json').write_text('{"keep":true}')
    mounts = team / 'team-workspace/skills'
    mounts.mkdir(parents=True)
    (mounts / installed.name).symlink_to(installed, target_is_directory=True)
    monkeypatch.setattr(fresh_loop.cli, '_runtime_skills_dir', lambda _: mounts)
    retired = fresh_loop.detach_retired_team('event', tmp_path)
    await fresh_loop.dispose_retired_team(retired)
    assert not team.exists() and not retired[0].exists()
    assert (installed / 'notebooks/state.json').read_text() == '{"keep":true}'


@pytest.mark.asyncio
async def test_48_retirements_keep_slow_native_directory_collection_outside_rpc(monkeypatch, tmp_path):
    """Use the installed native delete implementation and real websocket RPCs, without a model."""
    from openjiuwen.agent_teams.runtime import manager as native
    from openjiuwen.agent_teams.spawn import shared_resources
    from openjiuwen.agent_teams.external.cli_agent import session_cleanup
    from career_sim_runner.ws_client import delete_agent_session

    data, session, team, skills = _retirement_fixture(monkeypatch, tmp_path, 'baseline')
    protected = data / '.agent_teams/other-team'
    protected.mkdir()
    (protected / 'history').write_text('other game')
    template = data / 'agent/sessions/template'
    template.mkdir()
    (template / 'history.jsonl').write_text('fixed first-observe instructions')
    game_db = tmp_path / 'game.sqlite3'
    with sqlite3.connect(game_db) as db:
        db.execute('CREATE TABLE game_state(payload TEXT)')
        db.execute('INSERT INTO game_state VALUES(?)', ('{"month":6,"alive":true}',))
    game_bytes = game_db.read_bytes()
    skills_bytes = (skills / 'workflow-state.json').read_bytes()
    team_db = data / '.agent_teams/team.db'
    _roster_database(team_db)
    roster = fresh_loop.capture_roster(team_db, 'team')
    persisted = {'baseline'}
    checkpoints = SimpleNamespace(
        session_exists=AsyncMock(side_effect=lambda sid: sid in persisted),
        release=AsyncMock(side_effect=lambda sid: persisted.discard(sid)),
    )
    released_tables = []
    async def drop_tables(sid):
        released_tables.append(sid)
    async def delete_team_row(name):
        with sqlite3.connect(team_db) as db:
            for table in ('team_info', 'team_member', 'messages'):
                db.execute(f'DELETE FROM {table} WHERE team_name=?', (name,))
        return True
    database = SimpleNamespace(initialize=AsyncMock(), drop_session_tables_by_id=drop_tables,
                               team=SimpleNamespace(delete_team=delete_team_row))
    runtime = native.TeamRuntimeManager()
    runtime._pool = SimpleNamespace(has_active=AsyncMock(return_value=False))
    monkeypatch.setattr(runtime, '_resolve_any_team_session_release_info',
                        AsyncMock(return_value=SimpleNamespace(db_config=None)))
    monkeypatch.setattr(native.CheckpointerFactory, 'get_checkpointer', lambda: checkpoints)
    monkeypatch.setattr(shared_resources, 'get_shared_db', lambda _: database)
    monkeypatch.setattr(session_cleanup, 'cleanup_external_cli_backend_sessions', AsyncMock())
    monkeypatch.setattr(native, 'remove_session_worktrees', AsyncMock(return_value=True))
    monkeypatch.setattr(native, 'team_home', lambda name: data / '.agent_teams' / name)
    real_rmtree = shutil.rmtree
    baseline_collected = threading.Event()
    local_collections = []
    responses = set()

    def slow_rmtree(path, *args, **kwargs):
        # This delay exceeds the test RPC deadline, just as the observed 30s failure did.
        time.sleep(.15)
        real_rmtree(path, *args, **kwargs)
        if Path(path) == team:
            baseline_collected.set()
        else:
            local_collections.append(Path(path))
    monkeypatch.setattr(fresh_loop.shutil, 'rmtree', slow_rmtree)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(runtime.delete_team('team', ['baseline'], force=True), timeout=.1)
    assert await asyncio.to_thread(baseline_collected.wait, 2)
    real_rmtree(session)

    async def handler(socket):
        await socket.send('{}')
        request = json.loads(await socket.recv())
        sid = request['session_id']
        if request['method'] == 'session.delete':
            assert not team.exists(), 'Slow team tree must be detached before native deletion'
            await runtime.delete_team('team', [sid], force=True)
            real_rmtree(data / 'agent/sessions' / sid)
            responses.add(sid)
        else:
            assert request['method'] == 'chat.interrupt'
        await socket.send(json.dumps({'request_id': request['request_id'],
                                     'body': {'ok': True, 'payload': {'session_id': sid}}}))

    async def bounded_delete(ws, sid, **_):
        return await delete_agent_session(ws, sid, timeout_s=.1)
    monkeypatch.setattr(fresh_loop, 'delete_agent_session', bounded_delete)
    sealed = []
    monkeypatch.setattr(fresh_loop.benchmark, 'seal_agent_history', lambda _, __, sid: sealed.append(sid))
    monkeypatch.setattr(fresh_loop, 'close_retired_adapters', lambda _: None)

    async with websockets.serve(handler, '127.0.0.1', 0) as server:
        ws = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        for index in range(48):
            sid = f'event-{index}'
            fresh_loop.restore_roster(team_db, 'team', roster)
            _retirement_fixture(monkeypatch, tmp_path, sid)
            persisted.add(sid)
            gate = fresh_loop.Gate(tmp_path / f'gate-{index}')
            gate.create('game')
            gate.update(worker_status='stopped')
            monkeypatch.setattr(fresh_loop, 'gate_path', lambda *_, path=gate.path: path)
            await fresh_loop.stop_session(game_db, ws, sid, output=tmp_path)
            assert sid in responses and sid not in persisted
            assert gate.read()['revoked'] and not team.exists()
            assert not list((data / '.agent_teams/.coach-retired').iterdir())
            assert [p.name for p in (data / 'agent/sessions').iterdir()] == ['template']
            assert game_db.read_bytes() == game_bytes
            assert (skills / 'workflow-state.json').read_bytes() == skills_bytes
            assert (protected / 'history').read_text() == 'other game'
    assert len(sealed) == len(responses) == len(local_collections) == 48
    assert released_tables == ['baseline'] + [f'event-{i}' for i in range(48)]
    journals = list((tmp_path / 'private/retired-teams').glob('*.json'))
    assert len(journals) == 48
    assert all(json.loads(path.read_text())['phase'] == 'disposed' for path in journals)
