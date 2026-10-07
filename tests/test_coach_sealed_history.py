"""Deleting an isolated event session preserves its already completed audit."""

import json
from datetime import datetime

import pytest

from career_sim_runner.coach import benchmark, stream_audit
from career_sim_runner.transcript import StreamCollector


@pytest.mark.parametrize('collected', [False, True])
def test_deleted_session_preserves_audit_and_usage(tmp_path, monkeypatch, collected):
    monkeypatch.setattr(stream_audit, 'jiuwenswarm_data_dir', lambda: tmp_path / 'instance')
    output = tmp_path / 'report'
    attempt = benchmark.begin(output, agent_session_id='player')
    collector = StreamCollector(output, agent_session_id='player', request_id='req')
    collector.feed_frame(dict(request_id='req', event_type='chat.llm_usage', role='leader',
                              usage_metadata=dict(input_tokens=10, output_tokens=1, total_tokens=11)))
    call = dict(event_type='chat.tool_call', member_name='analyse-s', role='teammate',
                tool_call=dict(name='bash', tool_call_id='member-call', arguments='{}'))
    if collected:
        collector.feed_frame(dict(call, request_id='req'))
    stamp = benchmark.read_ledger(output)['attempts'][0]['started_at']
    history = tmp_path / 'instance/agent/sessions/player/history.jsonl'
    history.parent.mkdir(parents=True)
    history.write_text(json.dumps(dict(call, request_id='req',
                                      timestamp=datetime.fromisoformat(stamp).timestamp())) + '\n')
    benchmark.finish(output, attempt, stream_request_id='req')
    benchmark.seal_agent_history(output, collector.events_path, 'player')
    before = benchmark.read_ledger(output)['attempts'][0]
    history.unlink()
    benchmark.refresh(output, collector.events_path, stopped=True)
    after = benchmark.read_ledger(output)['attempts'][0]
    for key in ('history_audit_status', 'missing_history_tool_call_ids', 'stream_audit_issues',
                'observed_roles', 'roles_without_reported_usage', 'usage_status', 'token_usage'):
        assert after[key] == before[key], key
    assert after['history_sealed'] is True
    assert after['usage_status'] == ('partial_roles' if collected else 'partial_stream')
