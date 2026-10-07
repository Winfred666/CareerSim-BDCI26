"""Detect the missing teammate stream without inventing usage or playing a game."""

import json
from datetime import datetime

import pytest

from career_sim_runner.coach import benchmark, stream_audit
from career_sim_runner.transcript import StreamCollector


def usage(request, amount=11):
    return dict(request_id=request, event_type='chat.llm_usage', role='leader',
                usage_metadata=dict(input_tokens=amount-1, output_tokens=1, total_tokens=amount))


def test_bound_stream_rejects_foreign_usage_and_final_frames(tmp_path):
    collector = StreamCollector(tmp_path, agent_session_id='player', request_id='request')
    assert collector.feed_frame(usage('other')) is False
    assert collector.feed_frame(dict(request_id='other', response_kind='e2a.complete', is_final=True)) is False
    assert collector.feed_frame(dict(usage('request'), session_id='foreign')) is False
    assert collector.feed_frame(usage('request')) is True
    events = [json.loads(line) for line in collector.events_path.read_text().splitlines()]
    assert collector.totals.total_tokens == 11
    assert [e['kind'] for e in events] == ['stream_mismatch']*3 + ['usage']
    assert all(e['agent_session_id'] == 'player' and e['request_id'] == 'request' for e in events)
    assert not any(e['kind'] == 'frame_final' for e in events)


def test_delayed_old_session_usage_stays_with_old_attempt(tmp_path):
    first = benchmark.begin(tmp_path, agent_session_id='old')
    benchmark.begin(tmp_path, agent_session_id='new')
    collector = StreamCollector(tmp_path, agent_session_id='old', request_id='old-request',
                                event_context=lambda: dict(attempt=first))
    collector.feed_frame(usage('old-request'))
    benchmark.refresh(tmp_path, collector.events_path, stopped=True)
    rows = benchmark.read_ledger(tmp_path)['attempts']
    assert rows[0]['token_usage']['total_tokens'] == 11
    assert rows[1]['token_usage'] is None


@pytest.mark.parametrize('collect_call', [False, True])
def test_history_exposes_missing_teammate_calls_and_usage(tmp_path, monkeypatch, collect_call):
    monkeypatch.setattr(stream_audit, 'jiuwenswarm_data_dir', lambda: tmp_path / 'instance')
    attempt = benchmark.begin(tmp_path / 'report', agent_session_id='player')
    collector = StreamCollector(tmp_path / 'report', agent_session_id='player', request_id='req')
    collector.feed_frame(usage('req'))
    call = dict(event_type='chat.tool_call', member_name='analyse-s', role='teammate',
                tool_call=dict(name='bash', tool_call_id='member-call', arguments='{}'))
    if collect_call:
        collector.feed_frame(dict(call, request_id='req'))
    stamp = benchmark.read_ledger(tmp_path / 'report')['attempts'][0]['started_at']
    path = tmp_path / 'instance/agent/sessions/player/history.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(dict(call, request_id='req', timestamp=datetime.fromisoformat(stamp).timestamp()))+'\n')
    benchmark.finish(tmp_path / 'report', attempt, stream_request_id='req')
    benchmark.refresh(tmp_path / 'report', collector.events_path, stopped=True)
    row = benchmark.read_ledger(tmp_path / 'report')['attempts'][0]
    assert row['usage_status'] == ('partial_roles' if collect_call else 'partial_stream')
    assert row['roles_without_reported_usage'] == ['analyse-s']
    assert row['missing_history_tool_call_ids'] == ([] if collect_call else ['member-call'])
    assert row['token_usage']['total_tokens'] == 11
    text = (tmp_path / 'report/benchmark.md').read_text()
    assert '用量不完整' in text and '| analyse-s | — | — | — |' in text


def test_wrong_request_is_excluded_from_attempt_totals(tmp_path):
    attempt = benchmark.begin(tmp_path, agent_session_id='player')
    collector = StreamCollector(tmp_path, agent_session_id='player', request_id='wrong')
    collector.feed_frame(usage('wrong'))
    benchmark.finish(tmp_path, attempt, stream_request_id='expected')
    benchmark.refresh(tmp_path, collector.events_path, stopped=True)
    row = benchmark.read_ledger(tmp_path)['attempts'][0]
    assert row['token_usage'] is None
    assert row['usage_status'] == 'partial_stream'
    assert '会话请求不匹配' in row['stream_audit_issues']


def test_missing_history_does_not_claim_complete_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(stream_audit, 'jiuwenswarm_data_dir', lambda: tmp_path / 'missing')
    benchmark.begin(tmp_path, agent_session_id='player')
    collector = StreamCollector(tmp_path, agent_session_id='player', request_id='req')
    collector.feed_frame(usage('req'))
    benchmark.refresh(tmp_path, collector.events_path, stopped=True)
    row = benchmark.read_ledger(tmp_path)['attempts'][0]
    assert row['usage_status'] == 'unverified'
    assert row['token_usage']['total_tokens'] == 11
    assert '完整性未核验' in (tmp_path / 'benchmark.md').read_text()


def test_history_cache_refreshes_and_incomplete_write_is_unverified(tmp_path, monkeypatch):
    monkeypatch.setattr(stream_audit, 'jiuwenswarm_data_dir', lambda: tmp_path)
    path = tmp_path / 'agent/sessions/player/history.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text('')
    assert stream_audit.history_calls('player') == ()
    call = dict(event_type='chat.tool_call', role='leader', timestamp=1,
                tool_call=dict(tool_call_id='new'))
    path.write_text(json.dumps(call)+'\n')
    assert stream_audit.history_calls('player')[0]['tool_call_id'] == 'new'
    path.write_text('{')
    assert stream_audit.history_calls('player') is None
