"""Cost accounting across roles, late work, retries and reloads."""

import json

import pytest

from career_sim_runner.coach import benchmark, driver as coach_driver
from career_sim_runner.coach.gate import Gate
from career_sim_runner.transcript import StreamCollector
from career_sim_runner.models import TokenUsage
from career_sim_runner.ws_client import StepDriveResult


def emit(collector, role, input_tokens, output_tokens):
    node = {
        "event_type": "chat.llm_usage", "role": "leader" if role == "leader" else "teammate",
        "member_name": role,
        "usage_metadata": {"model_name": "test-model", "input_tokens": input_tokens,
                           "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens},
    }
    collector.feed_frame({"body": {"event_type": node["event_type"], "delta_kind": "custom", "delta": node}})


def read(output):
    return benchmark.read_ledger(output)


def test_all_roles_late_work_and_summary_are_not_double_counted(tmp_path):
    attempt = benchmark.begin(tmp_path, round=4, game_session_id="game", phase="decision")
    collector = StreamCollector(tmp_path)
    emit(collector, "leader", 100, 10)
    emit(collector, "translator", 200, 20)
    emit(collector, "reviewer", 300, 30)
    collector.feed_frame({"event_type": "chat.usage_summary", "usage": {
        "input_tokens": 600, "output_tokens": 60, "total_tokens": 660}, "model": "test-model"})
    assert collector.totals.total_tokens == 660
    benchmark.refresh(tmp_path, collector.events_path)
    benchmark.finish(tmp_path, attempt, elapsed_s=2, drive_elapsed_s=2, action_success=True)
    # More work after the action belongs to the same attempt until the next begins.
    emit(collector, "reviewer", 5, 2)
    benchmark.refresh(tmp_path, collector.events_path)
    benchmark.refresh(tmp_path, collector.events_path)
    row = read(tmp_path)["attempts"][0]
    assert row["token_usage"]["total_tokens"] == 667
    assert row["by_role"]["reviewer"]["total_tokens"] == 337
    assert row["usage_reports"] == 4
    assert row["cumulative_reported_tokens"] == 667
    assert (tmp_path / "benchmark.md").is_file()


def test_swarmflow_worker_total_is_counted_once_from_nested_usage(tmp_path):
    benchmark.begin(tmp_path, round=1)
    collector = StreamCollector(tmp_path)
    emit(collector, "leader", 100, 10)
    worker_frame = {
        "event_type": "chat.usage_metadata",
        "metadata": {
            "usage_metadata": {
                "model_name": "swarmflow-workers",
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 700,
            },
            "member_name": "swarmflow-workers",
            "role": "swarmflow-workers",
            "run_id": "wf_test",
            "usage_id": "swarmflow:task-test",
        },
    }
    collector.feed_frame(worker_frame)
    collector.feed_frame(worker_frame)  # replay/reconnect must not double charge
    benchmark.refresh(tmp_path, collector.events_path)

    row = read(tmp_path)["attempts"][0]
    assert row["token_usage"]["total_tokens"] == 810
    assert row["by_role"]["swarmflow-workers"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 700,
    }
    records = [json.loads(line) for line in collector.events_path.read_text().splitlines()]
    assert [record["usage_id"] for record in records if record.get("usage_id")] == [
        "swarmflow:task-test"
    ]


def test_swarmflow_budget_tracks_concurrent_run_scopes():
    from openjiuwen.agent_teams.workflow.engine.budget import BudgetLedger

    budget = BudgetLedger()
    budget.add(100, scope="wf_a")
    budget.add(250, scope="wf_b")
    budget.add(50, scope="wf_a")

    assert budget.spent == 400
    assert budget.spent_for("wf_a") == 150
    assert budget.spent_for("wf_b") == 250


@pytest.mark.asyncio
async def test_swarmflow_usage_is_published_without_changing_result_channel():
    from openjiuwen.agent_teams.workflow.tool_swarmflow import SwarmflowTool

    class Session:
        def __init__(self):
            self.chunks = []

        async def write_stream(self, chunk):
            self.chunks.append(chunk)

    session = Session()
    await SwarmflowTool._emit_usage(
        session,
        run_id="wf_test",
        task_id="task-test",
        total_tokens=321,
    )

    assert len(session.chunks) == 1
    chunk = session.chunks[0]
    assert chunk.type == "llm_usage"
    assert chunk.payload["usage_metadata"]["total_tokens"] == 321
    assert chunk.payload["usage_id"] == "swarmflow:task-test"


def test_withdraw_reload_retry_preserves_cumulative_cost(tmp_path, monkeypatch):
    stamps = iter(["2026-09-16T00:00:00+00:00", "2026-09-16T00:00:30+00:00",
                   "2026-09-16T01:00:00+00:00", "2026-09-16T01:00:02+00:00"])
    monkeypatch.setattr(benchmark, "now", lambda: next(stamps))
    first = benchmark.begin(tmp_path, round=4, game_session_id="game")
    benchmark.finish(tmp_path, first, elapsed_s=10, drive_elapsed_s=10, action_success=True)
    benchmark.mark_withdrawn(tmp_path, "game", 4)
    reload = benchmark.begin(tmp_path, round=4, game_session_id="game", phase="reload")
    benchmark.finish(tmp_path, reload, elapsed_s=2)
    retry = benchmark.begin(tmp_path, round=4, game_session_id="game")
    benchmark.finish(tmp_path, retry, elapsed_s=5, drive_elapsed_s=5)
    events = tmp_path / "events-a.jsonl"
    events.write_text('\n'.join(json.dumps({"kind": "usage", "ts": ts, "actor": "leader", "usage": {
        "input_tokens": n, "output_tokens": 0, "total_tokens": n}}) for ts, n in [
            ("2026-09-16T00:00:09+00:00", 100), ("2026-09-16T01:00:06+00:00", 50)]))
    monkeypatch.setattr(benchmark, "now", lambda: "2026-09-16T01:00:07+00:00")
    benchmark.refresh(tmp_path, events, stopped=True)
    data = read(tmp_path)
    assert len(data["attempts"]) == 3
    assert data["attempts"][0]["withdrawn_at"]
    assert data["attempts"][-1]["cumulative_reported_tokens"] == 150
    assert data["attempts"][-1]["cumulative_elapsed_s"] == 17  # excludes hour of editing
    assert data["attempts"][1]["token_usage"] is None


def test_missing_usage_is_unknown_and_partial_roles_are_visible(tmp_path):
    benchmark.begin(tmp_path, round=1)
    collector = StreamCollector(tmp_path)
    collector.feed_frame({"event_type": "chat.tool_call", "role": "teammate", "member_name": "translator",
                          "tool_call": {"name": "read_file"}})
    benchmark.refresh(tmp_path, collector.events_path)
    assert read(tmp_path)["attempts"][0]["token_usage"] is None
    assert read(tmp_path)["summary"]["reported_total_tokens"] is None
    emit(collector, "leader", 10, 5)
    benchmark.refresh(tmp_path, collector.events_path)
    row = read(tmp_path)["attempts"][0]
    assert row["usage_status"] == "partial_roles"
    assert row["roles_without_reported_usage"] == ["translator"]


def test_event_report_updates_and_snapshot_is_immutable(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "manifest.json").write_text('{}')
    output = benchmark.allocate_snapshot(tmp_path / "outputs", source, "mostroles")
    other = benchmark.allocate_snapshot(tmp_path / "outputs", source, "mostroles")
    assert output != other
    assert output.name.endswith("-mostroles")
    (source / "manifest.json").write_text('{"changed":true}')
    benchmark.snapshot_solution(source, output)
    assert (output / "solution" / "manifest.json").read_text() == '{}'
    collector = StreamCollector(output)
    collector.on_event = lambda event: benchmark.render_events(collector.events_path, output)
    emit(collector, "translator", 7, 3)
    emit(collector, "reviewer", 11, 5)
    actors = [json.loads(line)["actor"] for line in collector.events_path.read_text().splitlines()]
    assert actors == ["translator", "reviewer"]
    assert (output / "events.md").is_file()


@pytest.mark.asyncio
async def test_timeout_and_exception_costs_are_persisted(tmp_path, monkeypatch):
    gate = Gate(tmp_path / "gate.sqlite3")
    gate.create("game", log_dir=str(tmp_path / "output"), agent_session_id="player")
    async def timeout(*args):
        return StepDriveResult(1, "timeout", "game", False, None, TokenUsage(), "",
                               tmp_path / "missing.jsonl", tmp_path / "transcript.log")
    monkeypatch.setattr(coach_driver, "_drive", timeout)
    result = await coach_driver.drive(gate, 1)
    assert result.benchmark["termination_reason"] == "timeout"
    assert result.benchmark["elapsed_s"] >= 0
    async def error(*args):
        raise RuntimeError("worker failed")
    monkeypatch.setattr(coach_driver, "_drive", error)
    with pytest.raises(RuntimeError, match="worker failed"):
        await coach_driver.drive(gate, 1)
    data = read(tmp_path / "output")
    assert len(data["attempts"]) == 2
    assert data["attempts"][1]["termination_reason"] == "RuntimeError"


def test_corrections_count_policy_not_reload_or_infrastructure(tmp_path):
    attempt = benchmark.begin(tmp_path, round=3, game_session_id="game")
    benchmark.finish(tmp_path, attempt, elapsed_s=4, action_success=True)
    benchmark.record_correction(tmp_path, game_session_id="game", round_number=3,
                                reason="Ignored health floor", source="withdraw")
    benchmark.begin(tmp_path, round=3, game_session_id="game", phase="reload")
    benchmark.record_correction(tmp_path, game_session_id="game", round_number=3,
                                reason="Retry still violates floor")
    benchmark.record_correction(tmp_path, game_session_id="game", round_number=3,
                                reason="Socket disconnected", kind="infrastructure")
    data = read(tmp_path)
    assert data["summary"]["coach_corrections"] == 2
    assert data["summary"]["corrected_decisions"] == 1
    assert data["summary"]["infrastructure_interventions"] == 1
    assert data["corrections"][0]["attempt_ids"] == [1]


def test_accounting_and_automatic_comparison(tmp_path):
    import csv

    root = tmp_path / 'coach'
    output = root / 'run-one'
    attempt = benchmark.begin(output, round=1, shorttitle='年会社交')
    collector = StreamCollector(output, events_path=output / 'events.jsonl',
                                transcript_path=benchmark.runtime_dir(output) / 'transcript.log')
    emit(collector, 'leader', 1_000_000, 1_000)
    emit(collector, 'translator', 500_000, 500)
    benchmark.refresh(output, collector.events_path)
    benchmark.finish(output, attempt, elapsed_s=12.5)
    benchmark.render_events(collector.events_path, output)
    row = read(output)['attempts'][0]
    assert row['shorttitle'] == '年会社交'
    assert row['token_usage']['total_tokens'] == 1_501_500
    assert row['leader_context_tokens'] == 1_000_000
    assert row['by_role']['translator']['total_tokens'] == 500_500
    assert row['elapsed_s'] == 12.5
    assert (output / 'benchmark.md').is_file()
    assert (output / 'events.md').is_file()
    with (root / 'comparison.csv').open() as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]['total_tokens_M'] == '1.501500'
    assert rows[0]['final_score'] == ''
    # A different run must not overwrite this row; repeat updates must not duplicate it.
    benchmark.begin(root / 'run-two', round=1)
    benchmark.finish(output, attempt, elapsed_s=13)
    with (root / 'comparison.csv').open() as handle:
        rows = list(csv.DictReader(handle))
    assert [r['solution_run'] for r in rows] == ['run-one', 'run-two']


def test_stream_appends_across_reload_without_duplicate_tokens(tmp_path):
    output = tmp_path / 'run'
    benchmark.begin(output, round=1)
    events = output / 'events.jsonl'
    first = StreamCollector(output, events_path=events)
    emit(first, 'leader', 10, 1)
    benchmark.refresh(output, events)
    # Fresh collector is what a reloaded worker uses, with the same total log.
    benchmark.begin(output, round=1, phase='reload')
    benchmark.begin(output, round=1)
    second = StreamCollector(output, events_path=events)
    emit(second, 'reviewer', 20, 2)
    benchmark.refresh(output, events)
    records = [json.loads(line) for line in events.read_text().splitlines()]
    assert [r['seq'] for r in records] == [1, 2]
    assert read(output)['summary']['reported_total_tokens'] == 33
    assert len(list(output.glob('events*.jsonl'))) == 1


@pytest.mark.parametrize("actor", ["leader", "agent", "unknown"])
def test_agent_context_uses_latest_input_and_exposes_reload_drop(tmp_path, actor):
    output = tmp_path / 'run'
    solution = output / 'solution'
    solution.mkdir(parents=True)
    (solution / 'manifest.json').write_text(json.dumps({"mode": "team" if actor == "leader" else "agent"}))
    events = output / 'events.jsonl'
    first = benchmark.begin(output, round=1)
    collector = StreamCollector(output, events_path=events)
    emit(collector, actor, 100_000, 10)
    emit(collector, actor, 140_000, 10)
    benchmark.finish(output, first, elapsed_s=1)
    second = benchmark.begin(output, round=2)
    emit(collector, actor, 60_000, 10)
    benchmark.finish(output, second, elapsed_s=1)
    benchmark.refresh(output, events, stopped=True)
    rows = read(output)['attempts']
    assert rows[0]['leader_context_tokens'] == 140_000
    assert rows[0]['leader_context_delta_tokens'] == 140_000
    assert rows[1]['leader_context_tokens'] == 60_000
    assert rows[1]['leader_context_delta_tokens'] == -80_000
    report = (output / 'benchmark.md').read_text()
    assert '| Agent 新增上下文 | Agent 累计上下文 |' in report
    assert '0.140 M' in report and '-0.080 M' in report


@pytest.mark.parametrize(('observation', 'expected'), [
    ({'ending_score': {'quantitative_score': 80}}, None),
    ({'ending_score': {'outcome': 'completed', 'completed': True, 'survival_months': 12,
                      'quantitative_score': 80}}, None),
    ({'ending_score': {'outcome': 'completed', 'completed': True, 'survival_months': 48,
                      'quantitative_score': 80}}, 80),
    ({'ending_score': {'outcome': 'eliminated', 'quantitative_score': 0},
      'current_state': {'simulation_flags': {'alive': False}}}, 0),
])
def test_final_score_requires_verified_ending(observation, expected):
    assert benchmark.terminal_score(observation) == expected


def test_withdraw_retains_ending_through_reload_retry_and_lower_ending(tmp_path):
    import csv

    output = tmp_path / 'run'

    def score():
        with (tmp_path / 'comparison.csv').open() as handle:
            row = next(csv.DictReader(handle))
        return row['final_score'], row['final_score_withdrawn']

    attempt = benchmark.begin(output, game_session_id='game', round=1)
    assert score() == ('', '')
    benchmark.finish(output, attempt, final_score=80, elapsed_s=2)
    assert score() == ('80', 'False')
    benchmark.mark_withdrawn(output, 'game', 1)
    assert score() == ('80', 'True')
    benchmark.begin(output, round=1, phase='reload')
    retry = benchmark.begin(output, game_session_id='game', round=1)
    benchmark.finish(output, retry, final_score=None, elapsed_s=3)
    assert score() == ('80', 'True')
    benchmark.compare(tmp_path)
    assert score() == ('80', 'True')
    assert read(output)['summary']['elapsed_s'] == 5
    # The latest ending wins even when its verified score is zero.
    final = benchmark.begin(output, game_session_id='game', round=2)
    benchmark.finish(output, final, final_score=0)
    assert score() == ('0', 'False')
    benchmark.mark_withdrawn(output, 'game', 2)
    assert score() == ('0', 'True')


def test_compare_recovers_withdrawn_ending_from_legacy_summary(tmp_path):
    output = tmp_path / 'run'
    output.mkdir()
    (output / 'benchmark.md').write_text('# Old report\n')
    (output / 'benchmark.json').write_text(json.dumps({
        'attempts': [{'attempt': 1, 'round': 198, 'final_score': 76.92,
                      'withdrawn_at': '2026-09-26T07:39:23Z'}],
        'summary': {'attempts': 1, 'elapsed_s': 2, 'coach_corrections': 1,
                    'final_score': None},
    }))
    # Existing membership is authoritative; compare must migrate old CSV headers.
    (tmp_path / 'comparison.csv').write_text(
        'solution_run,elapsed_s,final_score\nrun,2.000,\narchived,3.000,70\n')
    rows = {row['solution_run']: row for row in benchmark.compare(tmp_path)}
    assert rows['run']['final_score'] == 76.92
    assert rows['run']['final_score_withdrawn'] is True
    assert rows['archived']['final_score'] == '70'


def test_deleted_comparison_row_is_not_recreated(tmp_path):
    import csv

    root = tmp_path / 'coach'
    old = root / 'old-run'
    keep = root / 'keep-run'
    benchmark.begin(old, round=1)
    attempt = benchmark.begin(keep, round=1)
    path = root / 'comparison.csv'
    with path.open(newline='') as handle:
        rows = [row for row in csv.DictReader(handle)
                if row['solution_run'] != 'old-run']
    with path.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=benchmark.COMPARISON_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    benchmark.finish(keep, attempt, elapsed_s=2)
    benchmark.compare(root)
    with path.open(newline='') as handle:
        names = [row['solution_run'] for row in csv.DictReader(handle)]
    assert names == ['keep-run']
