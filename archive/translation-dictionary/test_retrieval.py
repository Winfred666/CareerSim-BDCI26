from pathlib import Path
import runpy
SCRIPT = Path(__file__).with_name("retrieval.py")

def test_public_question_retrieves_existing_contextual_effects(tmp_path):
    lookup = runpy.run_path(str(SCRIPT))['dictionary_matches']
    dictionary = tmp_path / 'event-translator-dictionary.tsv'
    dictionary.write_text('情景\t效果\n逐行细审【Review积压】\tS++,H--\n抓大放小【Review边界问题未修】\tS-,R++\n理疗\tH++\n')
    obs = {'current_event': {'title': 'Code Review颗粒度', 'description': '边界处理问题'},
           'choices': [{'choice': 1, 'action': '逐行细审'}, {'choice': 2, 'action': '只关注核心逻辑'}]}
    assert lookup(tmp_path, obs) == ['逐行细审【Review积压】\tS++,H--', '抓大放小【Review边界问题未修】\tS-,R++']
    dictionary.write_text(dictionary.read_text().replace('R++', 'R+?'))
    assert lookup(tmp_path, obs)[1].endswith('R+?')
    assert lookup(tmp_path, {'current_event': {'title': '无关题目'}}) == []
    for choice in obs['choices']: choice['status_updates'] = {'Skill': 1}
    assert lookup(tmp_path, obs) == []


def test_atomic_dictionary_retrieval_keeps_other_metrics_visible(tmp_path):
    lookup = runpy.run_path(str(SCRIPT))['dictionary_matches']
    rows = ['职场黑话\t高效指标表达']
    rows += [f'项目推进方案{i}【项目推进】\tN+' for i in range(10)]
    rows += ['项目推进【暴露未公开信息】\tR++', '项目推进【实际交付】\tO++']
    (tmp_path / 'event-translator-dictionary.tsv').write_text('\n'.join(rows) + '\n')
    obs = {'current_event': {'title': '项目推进', 'description': '讨论项目推进方案'},
           'choices': [{'choice': 1, 'action': '推进项目并交付'}]}
    matches = lookup(tmp_path, obs)
    assert sum(row.endswith('N+') for row in matches) == 4
    assert any(row.endswith('R++') for row in matches)
    assert any(row.endswith('O++') for row in matches)


def test_bundled_dictionary_retrieval_uses_metrics_beyond_the_first(tmp_path):
    lookup = runpy.run_path(str(SCRIPT))['dictionary_matches']
    rows = ['职场黑话\t高效指标表达']
    rows += [f'深夜赶工方案{i}\tH-' for i in range(4)]
    bundled = '深夜赶工\tH--,O+,R+'
    rows.append(bundled)
    (tmp_path / 'event-translator-dictionary.tsv').write_text('\n'.join(rows) + '\n')
    obs = {'choices': [{'choice': 1, 'action': '深夜赶工'}]}
    assert bundled in lookup(tmp_path, obs)


def test_atomic_dictionary_retrieval_separates_option_actions(tmp_path):
    lookup = runpy.run_path(str(SCRIPT))['dictionary_matches_by_choice']
    (tmp_path / 'event-translator-dictionary.tsv').write_text(
        '职场黑话\t高效指标表达\n修复漏洞\tS++\n公开抱怨\tN--\n'
    )
    obs = {'current_event': {'title': '如何处理', 'description': '有人考虑公开抱怨'},
           'choices': [{'choice': 1, 'action': '修复漏洞'},
                       {'choice': 2, 'action': '公开抱怨'}]}
    assert lookup(tmp_path, obs) == {
        1: ['修复漏洞\tS++'],
        2: ['公开抱怨\tN--'],
    }


def test_atomic_dictionary_retrieval_handles_two_letter_technical_terms(tmp_path):
    lookup = runpy.run_path(str(SCRIPT))['dictionary_matches']
    (tmp_path / 'event-translator-dictionary.tsv').write_text(
        '职场黑话\t高效指标表达\n建设质量体系【CI】\tS+++\n'
    )
    obs = {'current_event': {'title': 'CI 投入', 'description': '团队缺少自动化门禁'},
           'choices': [{'choice': 1, 'action': '搭建 CI 流程'}]}
    assert lookup(tmp_path, obs) == ['建设质量体系【CI】\tS+++']


def test_atomic_dictionary_retrieval_finds_three_character_chinese_cue(tmp_path):
    lookup = runpy.run_path(str(SCRIPT))['dictionary_matches']
    (tmp_path / 'event-translator-dictionary.tsv').write_text(
        '职场黑话\t高效指标表达\n带病硬撑|带病坚持工作【仍承担交付责任】\tH--\n'
        '维持既定交付节奏\tO+\n'
    )
    obs = {'current_event': {'title': '带病坚持上班', 'description': '身体不适，任务仍需完成'},
           'choices': [{'choice': 1, 'action': '继续上班'}]}
    assert '带病硬撑|带病坚持工作【仍承担交付责任】\tH--' in lookup(tmp_path, obs)


def test_event_retrieval_excludes_feedback_shortfall_clues(tmp_path):
    lookup = runpy.run_path(str(SCRIPT))['dictionary_matches']
    (tmp_path / 'event-translator-dictionary.tsv').write_text(
        '职场黑话\t高效指标表达\n半年谈话技能不足\tS短板线索\n'
        '补足工作技能【实际完成学习】\tS++\n'
    )
    obs = {'current_event': {'title': '半年谈话技能不足', 'description': '决定补足工作技能'},
           'choices': [{'choice': 1, 'action': '完成学习'}]}
    assert lookup(tmp_path, obs) == ['补足工作技能【实际完成学习】\tS++']
