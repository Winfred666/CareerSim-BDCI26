"""Coach-only first-observe templates; game and learned notebooks stay external.

Use prepare once, freeze at its first gated observe, then rotate between accepted
events. The stable coach handle is independent of each disposable Jiuwen session.
All game actions still go through the ordinary one-event coach gate.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import signal
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

from career_sim_runner.coach import benchmark, cli
from career_sim_runner.coach.driver import launch
from career_sim_runner.coach.gate import Gate, gate_path
from career_sim_runner.paths import jiuwenswarm_data_dir
from career_sim_runner.setup import ensure_instance_configured
from career_sim_runner.ws_client import (
    _session_request, cancel_agent_session, delete_agent_session,
    reload_agent_config, rewind_agent_session,
)


def template_prompt(skill: Path, game: str) -> str:
    return (
        f"继续已有CareerSim游戏，session_id={game}。初始化、手册和五名问卷角色绑定已完成。"
        "以下已载入本角色完整 SKILL.md；跳过初始化，不重读文件，直接调用 observe 开始循环。"
        "不创建游戏，不重放手册，不检索目录。循环所需状态与前情只使用脚本。\n\n"
        + skill.read_text(encoding="utf-8")
    )


def capture_roster(team_db: Path, team: str) -> dict:
    """Freeze configuration only, excluding all coordination/game history."""
    with sqlite3.connect(f"file:{team_db}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        info = [dict(r) for r in connection.execute("SELECT * FROM team_info WHERE team_name=?", (team,))]
        members = [dict(r) for r in connection.execute("SELECT * FROM team_member WHERE team_name=?", (team,))]
    if len(info) != 1 or {m["member_name"] for m in members} != {
        info[0]["leader_member_name"], "analyse-o", "analyse-n", "analyse-s", "analyse-hw", "analyse-r"
    }:
        raise RuntimeError("Cannot freeze an incomplete questionnaire roster")
    for member in members:
        member.update(status="stopped", execution_status="idle")
    return {"team_info": info, "team_member": members}


def restore_roster(team_db: Path, team: str, roster: dict):
    """Recreate a deleted team configuration; never copy messages or tasks."""
    with sqlite3.connect(team_db) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM team_info WHERE team_name=?", (team,)).fetchone():
            connection.row_factory = sqlite3.Row
            current = {table: [dict(row) for row in connection.execute(
                f"SELECT * FROM {table} WHERE team_name=?", (team,))]
                for table in ("team_info", "team_member")}
            if any(sorted(current[table], key=lambda row: json.dumps(row, sort_keys=True))
                   != sorted(roster[table], key=lambda row: json.dumps(row, sort_keys=True))
                   for table in current):
                raise RuntimeError("Retired team still exists; refusing to merge its history")
            return  # Idempotent resume after the fixture transaction committed.
        for table in ("team_info", "team_member"):
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            for row in roster[table]:
                if set(columns) != set(row) or row["team_name"] != team:
                    raise RuntimeError("Roster schema or identity changed")
                names = ",".join('"' + column.replace('"', '""') + '"' for column in columns)
                placeholders = ",".join("?" for _ in columns)
                connection.execute(f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
                                   [row[column] for column in columns])


def preserve_workspace(saved: dict):
    """Keep game scripts/state outside the native team deletion directory."""
    skills = Path(saved['workspace']) / 'skills'
    data = jiuwenswarm_data_dir()
    if not skills.resolve().is_relative_to((data / '.agent_teams').resolve()):
        return  # An installed, session-independent workspace is already safe.
    if Path(saved['game']).name != saved['game']:
        raise RuntimeError('Invalid game workspace identity')
    target = data / 'agent/workspace/.coach-games' / saved['game'] / 'skills'
    if target.exists():
        raise RuntimeError('Persistent game workspace already exists; refusing to replace state')
    target.parent.mkdir(parents=True, exist_ok=True)
    skills.rename(target)
    saved['persistent_skills'] = str(target)
    restore_workspace_link(saved)


def restore_workspace_link(saved: dict):
    """Restore only the skill mount after native deletion; never copy memory."""
    if 'persistent_skills' not in saved:
        return
    target = Path(saved['persistent_skills'])
    if not target.is_dir():
        raise RuntimeError('Persistent game skills are missing')
    link = Path(saved['workspace']) / 'skills'
    if link.is_symlink() and link.resolve() == target.resolve():
        return
    if link.exists() or link.is_symlink():
        raise RuntimeError('Game skill mount changed; refusing to merge workspaces')
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)


def close_retired_adapters(control: Path, proc_root: Path = Path('/proc')):
    """Release only MCP processes bound to the acknowledged, retired gate."""
    expected = b'CAREER_COACH_GATE=' + os.fsencode(control.resolve())
    for process in proc_root.iterdir():
        if not process.name.isdecimal():
            continue
        try:
            command = (process / 'cmdline').read_bytes().split(b'\0')
            environment = (process / 'environ').read_bytes().split(b'\0')
            if b'career_sim_runner.emulator_adapter' in command and expected in environment:
                os.kill(int(process.name), signal.SIGTERM)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass


def detach_retired_team(sid: str, output: Path) -> tuple[Path, Path] | None:
    """Move only the stopped team's disposable tree out of the native delete RPC."""
    if not sid or Path(sid).name != sid or sid in {'.', '..'}:
        raise RuntimeError('Invalid retired session identity')
    data = jiuwenswarm_data_dir().resolve()
    token = hashlib.sha256(sid.encode()).hexdigest()
    journal = benchmark.runtime_dir(output) / 'retired-teams' / f'{token}.json'
    root = data / '.agent_teams'
    retired = root / '.coach-retired' / token
    if journal.exists():
        saved = json.loads(journal.read_text())
        if saved.get('session_id') != sid or saved.get('retired') != str(retired):
            raise RuntimeError('Retired team cleanup identity changed')
        team = saved['team_name']
    else:
        metadata = data / 'agent/sessions' / sid / 'metadata.json'
        if not metadata.exists():
            return None
        team = str(json.loads(metadata.read_text()).get('team_name') or '')
        if not team:
            return None
    if not team or Path(team).name != team or team in {'.', '..', '.coach-retired'}:
        raise RuntimeError('Invalid retired team path')
    source = root / team
    if source.is_symlink() or retired.is_symlink() or retired.parent.is_symlink():
        raise RuntimeError('Retired team cleanup refuses directory symlinks')
    if source.exists():
        if retired.exists():
            raise RuntimeError('Retired team was recreated before cleanup completed')
        skills = cli._runtime_skills_dir(sid)
        if skills is not None and skills.is_dir() and skills.resolve().is_relative_to(source.resolve()):
            # Native team-workspace/skills contains links to the installed skills.
            # Removing those links preserves their external scripts and notebooks.
            if any(skill.is_dir() and skill.resolve().is_relative_to(source.resolve())
                   for skill in skills.iterdir()):
                raise RuntimeError('Preserve game skills before retiring their team directory')
        retired.parent.mkdir(parents=True, exist_ok=True)
        journal.parent.mkdir(parents=True, exist_ok=True)
        cli.atomic_json(journal, {'session_id': sid, 'team_name': team,
                                 'retired': str(retired), 'phase': 'detaching'})
        source.rename(retired)  # Same filesystem, independent of the number of files.
    elif not retired.exists():
        return None
    return retired, journal


async def dispose_retired_team(detached: tuple[Path, Path] | None):
    """Finish slow filesystem collection locally, after native runtime deletion."""
    if detached is None:
        return
    retired, journal = detached
    started = time.monotonic()
    if retired.exists():
        await asyncio.to_thread(shutil.rmtree, retired)
    saved = json.loads(journal.read_text())
    saved.update(phase='disposed', collection_elapsed_s=time.monotonic() - started)
    cli.atomic_json(journal, saved)


async def stop_session(db: Path, ws: str, sid: str, *, output: Path, delete: bool = True):
    output = output.resolve()
    exists = (jiuwenswarm_data_dir() / "agent/sessions" / sid).exists()
    gate = Gate(gate_path(db, sid))
    if gate.path.exists():
        state = gate.read()
        if state["busy"] or state["permit"] or state["sequence"] != state["acknowledged"]:
            raise RuntimeError("Session is not at an acknowledged action boundary")
        if not exists:
            close_retired_adapters(gate.path)
            await dispose_retired_team(detach_retired_team(sid, output))
            return  # Retirement completed before its journal update.
        gate.revoke()
        await cancel_agent_session(ws, sid, mode="team", timeout_s=30)
        deadline = time.monotonic() + 60
        while gate.read()["worker_status"] not in {"new", "stopped"}:
            if time.monotonic() >= deadline:
                raise RuntimeError("Worker still reporting; retry retirement, never duplicate an event")
            await asyncio.sleep(.25)
        benchmark.seal_agent_history(output, output / "events.jsonl", sid)
    if delete and exists:
        close_retired_adapters(gate.path)
        detached = detach_retired_team(sid, output)
        await delete_agent_session(ws, sid, timeout_s=30)
        await dispose_retired_team(detached)


async def configure_gate(db: Path, ws: str, sid: str, game: str, output: Path, prompt: str):
    gate = Gate(gate_path(db, sid))
    gate.create(game, agent_session_id=sid, ws_url=ws, mode="team", existing_game=True,
                log_dir=str(output), benchmark_output_dir=str(output), prompt=prompt,
                random_seed=cli._load_state(db)["random_seed"], notebook_checkpoints=True)
    logdir, _ = cli._ensure_coach_emulator_log_dir(db, game_session_id=game)
    ensure_instance_configured(log_dir=logdir, coach_gate=gate.path, db_path=db)
    if not await reload_agent_config(ws):
        gate.revoke()
        raise RuntimeError("Agent config reload failed; execution remains disabled")
    return gate


async def next_event(db: Path, player: str, ws: str):
    """Use the same frozen prefix for each event of an automatic evaluation."""
    out = Path(cli._load_state(db)["benchmark_output_dir"])
    journal = benchmark.runtime_dir(out) / "fresh-loop.json"
    context = await cli._resolve_context(db, player)
    root = cli._runtime_skills_dir(context["agent_session_id"])
    flowpath = root / "observe-decide-review/notebooks/workflow-state.json"
    gate = Gate(gate_path(db, context["agent_session_id"]))
    while json.loads(flowpath.read_text()).get("phase") != "reviewed":
        if gate.read()["worker_status"] == "stopped":
            raise RuntimeError("Player exited before completing review; no event rotation")
        await asyncio.sleep(.25)
    args = argparse.Namespace(db=str(db), player=player, ws_url=ws)
    if not journal.exists():
        args.command = "prepare"
        warm = await run(args)
        warm_gate = Gate(gate_path(db, warm["warm_session"]))
        while True:
            history = cli.read_history(jiuwenswarm_data_dir(), warm["warm_session"])
            if any(r.get("event_type") == "chat.tool_call" for r in history):
                break
            if warm_gate.read()["worker_status"] == "stopped":
                raise RuntimeError("Template Player exited before first observe")
            await asyncio.sleep(.25)
        args.command = "freeze"
        await run(args)
    args.command = "rotate"
    return await run(args)


async def run(args):
    db = Path(args.db).resolve()
    out = Path(cli._load_state(db)["benchmark_output_dir"])
    journal = benchmark.runtime_dir(out) / "fresh-loop.json"
    context = await cli._resolve_context(db, args.player)
    game, current = context["game_session_id"], context["agent_session_id"]
    ws = args.ws_url
    saved = json.loads(journal.read_text()) if journal.exists() else {}
    if args.command == 'recover':
        # Explicit coach recovery only: rebuild a deleted instruction fixture,
        # preserving the game, notebooks and fresh-event conversation policy.
        if saved.get('game') != game:
            raise RuntimeError('Recovery fixture belongs to another game')
        gate = Gate(gate_path(db, current))
        if gate.path.exists():
            control = gate.read()
            if (control['worker_status'] != 'stopped' or control['busy'] or control['permit']
                    or control['sequence'] != control['acknowledged']):
                raise RuntimeError('Recovery requires a stopped, acknowledged Player')
        flow = json.loads((Path(saved['workspace']) / 'skills/observe-decide-review/notebooks/workflow-state.json').read_text())
        if flow.get('session_id') != game or flow.get('phase') != 'reviewed':
            raise RuntimeError('Recovery requires the completed review boundary')
        if (jiuwenswarm_data_dir() / 'agent/sessions' / current).exists():
            await stop_session(db, ws, current, output=out.resolve())
        before = await cli.read_session_payload(db, game)
        restore_workspace_link(saved)
        restore_roster(Path(saved['team_db']), saved['team_name'], saved['roster'])
        created = await _session_request(ws, 'session.create', '',
                  {'mode': 'team', 'work_mode': 'work', 'create_token': uuid.uuid4().hex})
        base = created['session_id']
        await _session_request(ws, 'team.session.bind', base,
              {'session_id': base, 'team_name': saved['team_name'], 'mode': 'team'})
        prefix = [{'id': uuid.uuid4().hex, 'role': 'user', 'channel_id': 'web',
                   'timestamp': time.time(), 'mode': 'team',
                   'content': template_prompt(Path(saved['workspace']) / 'skills/observe-decide-review/SKILL.md', game)}]
        history = jiuwenswarm_data_dir() / 'agent/sessions' / base / 'history.jsonl'
        history.write_text(json.dumps(prefix[0], ensure_ascii=False) + '\n')
        saved.update(template_session=base, history_hash=cli._json_hash(prefix), phase='ready')
        saved.setdefault('recoveries', []).append({'at': time.time(),
                                                  'previous_session': current, 'template_session': base,
                                                  'game_hash': cli._json_hash(before)})
        cli.atomic_json(journal, saved)
        if before != await cli.read_session_payload(db, game):
            raise RuntimeError('Recovery changed the game')
        args.command = 'rotate'
        return await run(args)
    if args.command == "prepare":
        if saved:
            raise RuntimeError("Template preparation already recorded; inspect its phase")
        root = cli._runtime_skills_dir(current)
        flow = json.loads((root / "observe-decide-review/notebooks/workflow-state.json").read_text())
        if flow["phase"] != "reviewed":
            raise RuntimeError("Current event review is incomplete")
        metadata = json.loads((jiuwenswarm_data_dir() / "agent/sessions" / current / "metadata.json").read_text())
        saved = {"phase": "preparing", "player": args.player, "game": game,
                 "workspace": flow["workspace"], "team_name": metadata["team_name"],
                 "payload_hash": cli._json_hash(await cli.read_session_payload(db, game)),
                 "create_token": uuid.uuid4().hex, "original_session": current}
        team_db = jiuwenswarm_data_dir() / ".agent_teams/team.db"
        saved.update(team_db=str(team_db), roster=capture_roster(team_db, saved["team_name"]))
        preserve_workspace(saved)
        cli.atomic_json(journal, saved)
        # Already retired originals are kept until the first clone is verified.
        await stop_session(db, ws, current, output=out, delete=False)
        result = await _session_request(ws, "session.create", "",
                 {"mode": "team", "work_mode": "work", "create_token": saved["create_token"]})
        warm = result["session_id"]
        await _session_request(ws, "team.session.bind", warm,
              {"session_id": warm, "team_name": saved["team_name"], "mode": "team"})
        skill = Path(saved["workspace"]) / "skills/observe-decide-review/SKILL.md"
        gate = await configure_gate(db, ws, warm, game, out, template_prompt(skill, game))
        attempt = benchmark.begin(out, game_session_id=game, agent_session_id=warm,
                                  shorttitle="首次observe模板", phase="context_template")
        gate.update(active_attempt=attempt)
        saved.update(phase="warming", warm_session=warm, attempt=attempt,
                     warming_started=time.time())
        cli.atomic_json(journal, saved)
        launch(gate)  # Deliberately no grant: cannot observe or act on the game.
        return {"warm_session": warm, "permit": False}
    if saved.get("game") != game:
        raise RuntimeError("Template belongs to another game")
    if args.command == "freeze":
        if saved["phase"] not in {"warming", "freezing"}:
            raise RuntimeError("Template is not ready to freeze")
        warm = saved["warm_session"]
        history = cli.read_history(jiuwenswarm_data_dir(), warm)
        calls = [r["tool_call"] for r in history if r.get("event_type") == "chat.tool_call"]
        if len(calls) != 1 or not calls[0]["name"].endswith("_observe"):
            raise RuntimeError("First call must be observe; no file searches or handbook replay")
        if cli._json_hash(await cli.read_session_payload(db, game)) != saved["payload_hash"]:
            raise RuntimeError("Warmup changed the game")
        base = saved.setdefault("template_session", "career-loop-template-" + uuid.uuid4().hex[:12])
        saved.setdefault("rewind_operation", uuid.uuid4().hex)
        saved["phase"] = "freezing"
        cli.atomic_json(journal, saved)
        session_dir = jiuwenswarm_data_dir() / "agent/sessions" / base
        if not session_dir.exists():
            await _session_request(ws, "session.fork", warm,
                {"source_session_id": warm, "target_session_id": base, "title": "首次observe前固定指令"})
            await _session_request(ws, "team.session.bind", base,
                {"session_id": base, "team_name": saved["team_name"], "mode": "team"})
        rewind = await rewind_agent_session(ws, base, before_tool_call_id=calls[0]["tool_call_id"],
                          operation_id=saved["rewind_operation"], reset_team=False)
        frozen = cli.read_history(jiuwenswarm_data_dir(), base)
        if len(frozen) != 1 or frozen[0].get("role") != "user" or "# CareerSim Leader" not in str(frozen[0].get("content")):
            raise RuntimeError("Frozen context did not retain loaded rules")
        await stop_session(db, ws, warm, output=out)
        restore_workspace_link(saved)
        # Template waiting and coach editing are idle, not Player execution.
        call_record = next(r for r in history if r.get("event_type") == "chat.tool_call")
        stamp = call_record.get("timestamp")
        elapsed = max(0.0, stamp - saved["warming_started"]) if isinstance(stamp, (int, float)) else None
        benchmark.finish(out, saved["attempt"], shorttitle="首次observe模板", action_success=False,
                         termination_reason="template_ready", phase="context_template",
                         elapsed_s=elapsed)
        saved.update(phase="ready", history_hash=cli._json_hash(frozen), rewind=rewind)
        cli.atomic_json(journal, saved)
        return {"template": base, "history_records": len(frozen), "game_unchanged": True}
    if args.command == "rotate":
        if saved["phase"] not in {"ready", "active", "rotating"}:
            raise RuntimeError("Template is unavailable")
        base = saved["template_session"]
        if cli._json_hash(cli.read_history(jiuwenswarm_data_dir(), base)) != saved["history_hash"]:
            raise RuntimeError("Immutable template changed")
        root = Path(saved["workspace"]) / "skills"
        flowpath = root / "observe-decide-review/notebooks/workflow-state.json"
        deadline = time.monotonic() + 60
        while json.loads(flowpath.read_text())["phase"] != "reviewed":
            if time.monotonic() >= deadline:
                raise RuntimeError("Review did not finish; do not retire or duplicate an action")
            await asyncio.sleep(.25)
        before = await cli.read_session_payload(db, game)
        if saved["phase"] != "rotating":
            if "roster" not in saved:
                team_db = jiuwenswarm_data_dir() / ".agent_teams/team.db"
                saved.update(team_db=str(team_db), roster=capture_roster(team_db, saved["team_name"]))
            saved.update(phase="rotating", retiring=current,
                         next_session="career-event-" + uuid.uuid4().hex[:12],
                         retired=False, roster_restored=False,
                         rotation_payload_hash=cli._json_hash(before))
            cli.atomic_json(journal, saved)
        if saved["rotation_payload_hash"] != cli._json_hash(before):
            raise RuntimeError("Game advanced during context rotation")
        retiring = saved["retiring"]
        if not saved.get("retired"):
            await stop_session(db, ws, retiring, output=out)
            restore_workspace_link(saved)
            saved["retired"] = True
            cli.atomic_json(journal, saved)
        if not saved.get("roster_restored"):
            restore_roster(Path(saved["team_db"]), saved["team_name"], saved["roster"])
            saved["roster_restored"] = True
            cli.atomic_json(journal, saved)
        sid = saved["next_session"]
        if not (jiuwenswarm_data_dir() / "agent/sessions" / sid).exists():
            await _session_request(ws, "session.fork", base,
                  {"source_session_id": base, "target_session_id": sid, "title": "独立事件上下文"})
            await _session_request(ws, "team.session.bind", sid,
                  {"session_id": sid, "team_name": saved["team_name"], "mode": "team"})
        gate = Gate(gate_path(db, sid))
        if not gate.path.exists():
            # Team runtimes do not inherit the host Agent's forked memory. Seed
            # the identical loaded instruction prefix in chat.send as well.
            # It contains no event history and requires no file/tool replay.
            frozen = cli.read_history(jiuwenswarm_data_dir(), base)
            await configure_gate(db, ws, sid, game, out,
                                 frozen[0]["content"])
        cli._register_coach_session(db, args.player, game, sid, context["seed"], context_bootstrap=True)
        cli._save_state(db, agent_session_id=sid, context_mode="isolated-events")
        with benchmark.ledger(out) as document:
            document["context_mode"] = "isolated-events"
            document.setdefault("isolated_from_round", len(cli.CheckpointStore(db, game).records()) + 1)
        saved.update(phase="active", active_session=sid)
        cli.atomic_json(journal, saved)
        if before != await cli.read_session_payload(db, game):
            raise RuntimeError("Context rotation changed the game")
        return {"agent_session_id": sid, "seed": context["seed"], "game_unchanged": True}
    raise RuntimeError("Unknown operation")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "freeze", "rotate", "recover"])
    parser.add_argument("--player", required=True)
    parser.add_argument("--db", default=".career_sim_runner/career_emu/career_emulator.sqlite3")
    parser.add_argument("--ws-url", default="ws://127.0.0.1:21092")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False))


if __name__ == "__main__":
    main()
