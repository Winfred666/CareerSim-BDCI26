"""Deterministic sampling must not reuse storage IDs or change real state."""

from types import SimpleNamespace
import random

import pytest

from career_sim_runner.coach.seed import DEFAULT_BENCHMARK_SEED, configure_sampling_seed


@pytest.mark.asyncio
async def test_seed_is_stable_across_games_and_changes_with_month():
    class Resolver:
        def _sample_root_nodes(self, session, limit, existing_event_ids):
            ids = [i for i in range(100) if i not in existing_event_ids]
            random.Random(f"{session.session_id}:{session.career.current_month}").shuffle(ids)
            return ids[:limit]
    resolver = Resolver()
    class Engine:
        async def _get_resolver(self):
            return resolver
    engine = Engine()
    await configure_sampling_seed(engine, DEFAULT_BENCHMARK_SEED)
    first = SimpleNamespace(session_id="unique-a", career=SimpleNamespace(current_month=1))
    second = SimpleNamespace(session_id="unique-b", career=SimpleNamespace(current_month=1))
    a = resolver._sample_root_nodes(first, 5, {1})
    assert a == resolver._sample_root_nodes(second, 5, {1})
    assert first.session_id == "unique-a"
    assert second.session_id == "unique-b"
    assert 1 not in a
    second.career.current_month = 2
    assert a != resolver._sample_root_nodes(second, 5, {1})
    await configure_sampling_seed(engine, DEFAULT_BENCHMARK_SEED)  # idempotent
    with pytest.raises(RuntimeError, match="Cannot change"):
        await configure_sampling_seed(engine, "other")
