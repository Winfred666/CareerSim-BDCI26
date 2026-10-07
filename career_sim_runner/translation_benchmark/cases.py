"""Build deterministic translation cases from the installed CareerSim event pool."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Any

from career_emulator.events import DEFAULT_EVENT_DIR, EventPoolLoader
from career_emulator.server.models import ScenarioDefinition

METRICS = ("H", "D", "S", "N", "O", "W", "R")
STATUS_TO_METRIC = {
    "Health": "H",
    "Dignity": "D",
    "Skill": "S",
    "Network": "N",
    "Output": "O",
    "Wealth": "W",
    "HiddenRisk": "R",
}


@dataclass(frozen=True)
class TranslationCase:
    """One event node and its hidden authoritative metric deltas."""

    case_id: str
    shorttitle: str
    observation: dict[str, Any]
    expected: dict[int, dict[str, str]]


def metric_symbol(status_name: str, delta: int) -> str:
    """Convert one emulator status update to translator H/D/S/N/O/W/R notation."""
    if delta == 0:
        return "unknown"
    return ("+" if delta > 0 else "-") * abs(delta)


def expected_metrics(status_updates: dict[str, int]) -> dict[str, str]:
    """Return only authoritative non-zero effects for one option."""
    expected: dict[str, str] = {}
    for status_name, delta in status_updates.items():
        metric = STATUS_TO_METRIC.get(status_name)
        if metric is not None and int(delta) != 0:
            expected[metric] = metric_symbol(status_name, int(delta))
    return expected


def _month_for_scenario(preconditions: dict[str, Any]) -> int:
    condition = preconditions.get("current_month")
    threshold = getattr(condition, "threshold", 1)
    try:
        return max(1, min(48, int(threshold)))
    except (TypeError, ValueError):
        return 1


def _observation(event_name: str, description: str, actions: list[str], month: int) -> dict[str, Any]:
    level = min(9, max(1, (month - 1) // 6 + 1))
    return {
        "current_state": {
            "time": {
                "current_month": month,
                "current_quarter": (month - 1) // 3 + 1,
                "current_year": (month - 1) // 12 + 1,
            },
            "status": {
                "health": 5,
                "dignity": 5,
                "skill": 3,
                "network": 3,
                "level": f"L{level}",
                "duration_in_level": 1,
                "output": 3,
                "wealth": 3,
                "energy": 3,
            },
            "simulation_flags": {"alive": True, "failed": False, "failure_reason": None},
            "session_id": "translation-benchmark",
            "available_choice_count": len(actions),
        },
        "current_event": {"title": event_name, "description": description},
        "choices": [{"choice": number, "action": action} for number, action in enumerate(actions, start=1)],
        "events": "",
    }


def public_event_history(scenario: ScenarioDefinition, target: str) -> list[dict[str, Any]]:
    """Reconstruct a unique prior path using only public descriptions and chosen actions.

    These are synthetic node samples, not recorded playthroughs. Never choose one
    history arbitrarily at a merge, nor include unchosen branches, future nodes,
    or any status updates. Fail explicitly if a history cannot be established.
    """
    if target not in scenario.nodes:
        raise ValueError(f"Unknown event node: {scenario.event_id}:{target}")
    paths: list[list[dict[str, Any]]] = []

    def visit(node_id: str, history: list[dict[str, Any]], seen: frozenset[str]) -> None:
        if len(paths) > 1:
            return
        if node_id == target:
            paths.append(history)
            return
        if node_id in seen:
            raise ValueError(f"Cyclic event history: {scenario.event_id}:{target}")
        node = scenario.nodes[node_id]
        for number, choice in enumerate(node.choices, start=1):
            visit(choice.next_node, [*history, {
                "description": node.description,
                "selected_choice": {"choice": number, "action": choice.action},
            }], seen | {node_id})

    visit(scenario.start_node, [], frozenset())
    if len(paths) != 1:
        raise ValueError(
            f"Expected one event history, found {len(paths)}: {scenario.event_id}:{target}"
        )
    return paths[0]


async def load_cases(
    seed: str,
    limit: int,
    exclude_case_ids: set[str] | frozenset[str] | None = None,
    *, strict_history: bool = True,
) -> tuple[list[TranslationCase], str]:
    """Load decision nodes with their public prior path, in fixed-seed order."""
    scenarios = await EventPoolLoader().load()
    cases: list[TranslationCase] = []
    for event_id, scenario in sorted(scenarios.items()):
        month = _month_for_scenario(scenario.preconditions)
        for node_id, node in sorted(scenario.nodes.items()):
            if not node.choices:
                continue
            case_id = f"{event_id}:{node_id}"
            suffix = node_id.removeprefix("node_")
            observation = _observation(
                scenario.event_name, node.description,
                [choice.action for choice in node.choices], month,
            )
            cases.append(
                TranslationCase(
                    case_id=case_id,
                    shorttitle=f"{scenario.event_name}·{suffix}",
                    observation=observation,
                    expected={
                        number: expected_metrics(choice.status_updates)
                        for number, choice in enumerate(node.choices, start=1)
                    },
                )
            )

    eligible = cases if not exclude_case_ids else [case for case in cases if case.case_id not in exclude_case_ids]
    ordered = sorted(
        eligible,
        key=lambda case: hashlib.sha256(f"{seed}\0{case.case_id}".encode()).digest(),
    )
    if limit > 0:
        ordered = ordered[:limit]
    enriched = []
    for case in ordered:
        event_id, node_id = case.case_id.split(":", 1)
        try:
            history = public_event_history(scenarios[event_id], node_id)
        except ValueError as exc:
            if strict_history or not str(exc).startswith('Expected one event history, found'):
                raise
            # Preserve the fixed-seed sample at merge nodes without inventing a
            # path. Every parallel translator gets the same unavailable history.
            enriched.append(replace(case, observation={**case.observation, 'event_history': {
                'source': '前置路径不唯一，未提供前情，不推测已选分支。', 'steps': []}}))
            continue
        observation = case.observation
        if history:
            observation = {**observation, "event_history": {
                "source": "沿事件图的唯一前置路径复原，并非实际运行日志",
                "steps": history,
            }}
        enriched.append(replace(case, observation=observation))
    return enriched, str(DEFAULT_EVENT_DIR.resolve())
