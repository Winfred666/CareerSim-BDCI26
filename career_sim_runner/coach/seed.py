"""Coach-only deterministic sampling, independent of unique game storage IDs."""

from copy import copy

DEFAULT_BENCHMARK_SEED = "career-sim-benchmark-v1"


async def configure_sampling_seed(engine, seed: str) -> None:
    """Bind the existing sampler to a stable seed without changing its rules.

    The emulator seeds sampling with ``session_id:month`` but generates a new
    UUID for each game. Only the sampler's shallow session view gets the seed
    identity. Storage IDs, real state, eligibility and transitions stay intact.
    Ordinary (ungated) play never calls this adapter.
    """
    if not seed:
        return
    resolver = await engine._get_resolver()
    if getattr(resolver, "_coach_sampling_seed", None) == seed:
        return
    if getattr(resolver, "_coach_sampling_seed", None) is not None:
        raise RuntimeError("Cannot change sampling seed inside an active emulator")
    original = resolver._sample_root_nodes

    def sample(session, limit, existing_event_ids):
        view = copy(session)
        view.session_id = seed
        return original(view, limit, existing_event_ids)

    resolver._sample_root_nodes = sample
    resolver._coach_sampling_seed = seed
