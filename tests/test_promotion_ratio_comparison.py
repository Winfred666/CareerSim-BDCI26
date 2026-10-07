from pathlib import Path
import runpy
import pytest


helpers = runpy.run_path(str(Path(__file__).resolve().parents[1] /
    'solution/skills/observe-decide-review/scripts/refresh-context.py'))


def test_feedback_shortfalls_uses_explicit_public_labels_without_dictionary(tmp_path):
    parse = helpers['feedback_shortfalls']
    assert parse('【半年谈话】成果不足，协作不足', tmp_path) == ['O', 'N']
    # Generic praise/criticism is not an explicit metric shortfall.
    assert parse('【半年谈话】亮点不算突出，技能不足', tmp_path) == ['S']
    assert parse('整体达标，只会忙不会赢', tmp_path) == []


def test_near_cap_requires_a_verified_cap_and_no_contradiction():
    near = helpers['near_cap_metrics']
    assert near(dict(O=99, S=999, N=99), 4, {4: (0, dict(O=None, S=None, N=None))}) == set()
    caps = {4: (0, dict(O=26, S=None, N=None))}
    assert near(dict(O=25, S=99, N=99), 4, caps, (3, 2)) == {'O'}
    assert near(dict(O=27, S=99, N=99), 4, caps) == set()
