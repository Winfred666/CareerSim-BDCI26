"""The SDK loader must deliver the frozen loop to the cold Leader."""
from jiuwenswarm.agents.harness.team.config_loader import _build_leader_spec
from openjiuwen.agent_teams.schema.blueprint import LeaderSpec


def test_cold_leader_loader_preserves_private_loop():
    leader = LeaderSpec.model_validate(_build_leader_spec({'leader': {
        'member_name': 'team-leader', 'display_name': 'Leader',
        'prompt': 'K=/installed/skill; handbook 已完成；直接 observe，再执行 refresh-redline。',
        'desc': '执行循环',
    }}))
    assert leader.prompt.startswith('K=/installed/skill;')
    assert 'handbook 已完成' in leader.prompt
    assert leader.desc == '执行循环'
