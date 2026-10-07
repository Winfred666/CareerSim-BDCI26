"""Checks for the pinned JiuwenSwarm coach control branch."""

from career_sim_runner.jiuwen_patch import (
    JIUWENSWARM_TARGETS,
    LLM_RETRY_TARGETS,
    REWIND_TARGETS,
    TASK_ERROR_TARGETS,
    TARGETS,
    _site_packages_root,
    ensure_openjiuwen_control_patch,
)


def test_openjiuwen_control_patch_is_complete_and_idempotent() -> None:
    patch = ensure_openjiuwen_control_patch()
    assert patch.is_file()
    assert ensure_openjiuwen_control_patch() == patch
    root = _site_packages_root()
    for relative, markers in TARGETS.items():
        text = (root / relative).read_text(encoding="utf-8")
        assert all(marker in text for marker in markers)
    for relative, markers in JIUWENSWARM_TARGETS.items():
        text = (root / relative).read_text(encoding="utf-8")
        assert all(marker in text for marker in markers)
    for relative, markers in REWIND_TARGETS.items():
        text = (root / relative).read_text(encoding="utf-8")
        assert all(marker in text for marker in markers)
    for relative, markers in (LLM_RETRY_TARGETS | TASK_ERROR_TARGETS).items():
        text = (root / relative).read_text(encoding="utf-8")
        assert all(marker in text for marker in markers)


def test_server_extension_imports_outside_checkout(tmp_path):
    """Real service children do not inherit the repository import path."""
    import subprocess
    import sys
    ensure_openjiuwen_control_patch()
    subprocess.run([sys.executable, "-I", "-c",
                    "from jiuwenswarm.agents.harness.common.careersim_rewind import rewind_before_tool"],
                   cwd=tmp_path, check=True, capture_output=True)


def test_scoped_worker_usage_preserves_upstream_dual_budget():
    """Concurrent run accounting must keep both upstream budget ceilings."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import Mock
    from openjiuwen.agent_teams.workflow.backends.budget_rail import SwarmflowBudgetRail
    from openjiuwen.agent_teams.workflow.engine.budget import BudgetLedger
    from openjiuwen.core.single_agent.rail.base import ModelCallInputs

    shared, first, second = BudgetLedger(total=100), BudgetLedger(total=20), BudgetLedger(total=80)
    rails = (SwarmflowBudgetRail(shared, first, scope="first"),
             SwarmflowBudgetRail(shared, second, scope="second"))
    for rail, tokens in zip(rails, (25, 30)):
        ctx = SimpleNamespace(inputs=ModelCallInputs(
            response=SimpleNamespace(usage_metadata=SimpleNamespace(total_tokens=tokens))),
            request_force_finish=Mock())
        asyncio.run(rail.after_model_call(ctx))
        assert ctx.request_force_finish.called == (tokens == 25)
    assert (shared.spent, first.spent, second.spent) == (55, 25, 30)
    assert (shared.spent_for("first"), shared.spent_for("second")) == (25, 30)
