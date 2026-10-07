#!/usr/bin/env python3
"""Refresh the existing redline row using public values and fixed competition caps.

One solution serves one game. Inputs are copied from the standard public observe
response. No simulator-side file, adapter, rules, database or extra observe is used.
"""

from __future__ import annotations

import json
from math import ceil, isfinite
from fractions import Fraction
from decimal import Decimal, ROUND_HALF_UP
import os
from pathlib import Path
import re
import runpy
import stat
import sys
import tempfile


NOTEBOOKS = Path(__file__).resolve().parent.parent / "notebooks"
REVIEW_CONTEXT = "review-context.json"
METRICS = {"O": "output", "S": "skill", "N": "network", "H": "health", "D": "dignity", "W": "wealth"}
LOW_THRESHOLDS = {"H": 3, "D": 3, "W": 2}
METRIC_NAMES = {"O": "绩效产出", "S": "专业技能", "N": "人脉", "H": "身心健康",
                "D": "尊严", "W": "个人财富", "R": "隐患"}
# Read existing rows as well as the new Chinese display names.
STATE_ALIASES = {name: metric for metric, name in METRIC_NAMES.items()}
STATE_ALIASES["工作产出"] = "O"
STATE_ALIASES[f"Dignity（{METRIC_NAMES['D']}）"] = "D"
STATE_PATTERN = "|".join(re.escape(name) for name in STATE_ALIASES)


def metric_name(metric: str) -> str:
    return METRIC_NAMES[metric]


def format_state(level: int, values: dict) -> str:
    return f"L={level};" + ";".join(
        f"{metric_name(metric)}{text_number(value)}" for metric, value in values.items())


CAP_METRICS = ("O", "S", "N")
# Current CareerSim competition rules: int(promotion threshold * 1.2).
# L10 has no outgoing promotion requirement, so its O/S/N are uncapped.
STAT_CAPS = {
    1: {"O": 3, "S": 9, "N": 3},
    2: {"O": 6, "S": 21, "N": 7},
    3: {"O": 16, "S": 42, "N": 21},
    4: {"O": 26, "S": 108, "N": 36},
    5: {"O": 38, "S": 156, "N": 54},
    6: {"O": 54, "S": 204, "N": 72},
    7: {"O": 72, "S": 252, "N": 96},
    8: {"O": 90, "S": 300, "N": 120},
    9: {"O": 108, "S": 348, "N": 144},
    10: {"O": None, "S": None, "N": None},
}
PROMOTION_HEADER = ["时间", "状态(O;S;N;R)", "下个晋升点", "反馈结果", "晋升比例推算"]
PROMOTION_RATIOS = {
    1: (Fraction(8, 3), Fraction(8, 3)),
    2: (Fraction(18, 5), Fraction(18, 6)),
    3: (Fraction(35, 14), Fraction(35, 18)),
    4: (Fraction(90, 22), Fraction(90, 30)),
    5: (Fraction(130, 32), Fraction(130, 45)),
    6: (Fraction(170, 45), Fraction(170, 60)),
    7: (Fraction(210, 60), Fraction(210, 80)),
    8: (Fraction(250, 75), Fraction(250, 100)),
    9: (Fraction(290, 90), Fraction(290, 120)),
}


def fixed_promotion_target(level: int) -> tuple[Fraction, Fraction]:
    """Competition ratios change with current level only; L10 keeps L9's pair."""
    if type(level) is not int or not 1 <= level <= 10:
        raise ValueError("invalid promotion level")
    return PROMOTION_RATIOS[min(level, 9)]


def number(value: object) -> int | None:
    """Keep unknown values unknown; never coerce booleans or fractions to integers."""
    if value is None or value in ("?", "unknown"):
        return None
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value):
        return int(value)
    raise ValueError(f"invalid numeric value: {value!r}")


def level_number(value: object) -> int | None:
    """Accept L3 or the reviewer's compact L=3 representation."""
    if isinstance(value, str) and value.startswith("L"):
        value = value[1:]
    level = number(value)
    if level is not None and not 1 <= level <= 10:
        raise ValueError("level must be L1 through L10")
    return level


def read_table(path: Path, columns: int) -> tuple[bytes, list[bytes], list[int]]:
    """Retain bytes for safe replacement, without interpreting historical records."""
    original = path.read_bytes()
    lines = original.splitlines(keepends=True)
    populated = [index for index, line in enumerate(lines) if line.strip()]
    if len(populated) < 2:
        raise ValueError(f"missing header or data row: {path.name}")
    for index in (populated[0], populated[-1]):
        if len(cells(lines[index])) != columns:
            raise ValueError(f"invalid column count: {path.name}")
    return original, lines, populated


def cells(line: bytes) -> list[str]:
    return line.decode("utf-8").rstrip("\r\n").split("\t")


def text_number(value: int | None) -> str:
    return "?" if value is None else str(value)


def parse_expected(state: str) -> dict[str, int | None]:
    expected = {}
    for token in state.split(";"):
        match = re.fullmatch(rf"({STATE_PATTERN}|L|[OSNHDWR])=?(.+)", token)
        if not match:
            raise ValueError("invalid pending review state")
        key, value = match.groups()
        key = STATE_ALIASES.get(key, key)
        if key in expected:
            raise ValueError("invalid pending review state")
        expected[key] = level_number(value) if key == "L" else number(value)
    if set(expected) != set("LOSNHDWR"):
        raise ValueError("incomplete pending review state")
    return expected


def risk_from_state(state: str) -> int | None:
    match = re.search(rf"(?:^|;)(?:R|{metric_name('R')})=?(\?|unknown|-?\d+)(?=;|>|$)", state)
    if not match:
        raise ValueError("redline state has no R estimate")
    value = number(match[1])
    if value is not None and value < 0:
        raise ValueError("negative R estimate")
    return value


def fixed_stat_caps() -> dict[int, tuple[int, dict[str, int | None]]]:
    """Supply fresh fixed caps in the numeric policy's existing per-level format."""
    return {level: (0, dict(values)) for level, values in STAT_CAPS.items()}


def near_cap_margins(caps: dict[str, int | None]) -> dict[str, Fraction]:
    """Exact cap minus cap / 1.2; round only when classifying near-cap states."""
    return {metric: Fraction(max(0, caps.get(metric) or 0), 6)
            for metric in CAP_METRICS}


def labels_for(actual: dict[str, int | None], caps: dict[str, int | None], level: int,
               target: tuple[float, float] | None = None) -> str:
    """Determine every label from actual values, never from previous labels."""
    labels = []
    for metric in ("H", "D", "W", "R", "O", "S", "N"):
        value = actual[metric]
        if value is None:
            continue
        low = LOW_THRESHOLDS.get(metric)
        if low is not None and value <= low:
            labels.append(f"{metric_name(metric)}过低")
        if metric == "R" and value > 1:
            labels.append(f"{metric_name(metric)}偏高")
        if metric == "H":
            if value == 10:
                labels.append(f"{metric_name(metric)}封顶")
        elif metric in CAP_METRICS and caps[metric] is not None:
            gap = caps[metric] - value
            if gap == 0:
                labels.append(f"{metric_name(metric)}封顶")
    return "且".join(labels) or "无"


def atomic_update(path: Path, original: bytes | None, replacement: bytes) -> None:
    """Atomically save a notebook, preserving permissions and checking prior contents."""
    if replacement == original:
        return
    fd, temporary = tempfile.mkstemp(prefix=".notebook-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(replacement)
            if path.exists():
                os.fchmod(output.fileno(), stat.S_IMODE(path.stat().st_mode))
        if (path.read_bytes() if path.exists() else None) != original:
            raise ValueError("notebook changed during update")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def refresh(notebooks: Path, status: object, has_events: str, *,
            risk_override: int | None = None, emit: bool = True) -> str:
    if not isinstance(status, dict):
        raise ValueError("missing public status")
    if has_events not in ("0", "1"):
        raise ValueError("has-events must be 0 (empty events) or 1 (nonempty events)")
    level = level_number(status["level"])
    if level is None:
        raise ValueError("unknown current level")
    actual = {metric: number(status[field]) for metric, field in METRICS.items()}
    for metric, minimum in (("O", -1), ("S", 0), ("N", 0), ("H", 0), ("D", 0)):
        value = actual[metric]
        if value is not None and (value < minimum or (metric in ("H", "D") and value > 10)):
            raise ValueError(f"invalid observed {metric_name(metric)}")
    redline_path = notebooks / "redline-guardian-keeps.tsv"
    original_redline, redline_lines, redline_rows = read_table(redline_path, 3)
    event_key, prior_state, prior_labels = cells(redline_lines[redline_rows[-1]])
    if not re.fullmatch(r"\d{5}", event_key):
        raise ValueError("invalid redline event key")
    if prior_labels == "待判定":
        parse_expected(prior_state)

    actual["R"] = risk_override if risk_override is not None else risk_from_state(prior_state)

    caps = STAT_CAPS[level]
    labels = labels_for(actual, caps, level)
    state = format_state(level, actual)
    if redline_path.read_bytes() != original_redline:
        raise ValueError("redline changed during refresh")

    old_redline_line = redline_lines[redline_rows[-1]]
    ending = old_redline_line[len(old_redline_line.rstrip(b"\r\n")) :]
    redline_lines[redline_rows[-1]] = "\t".join([event_key, state, labels]).encode() + ending
    atomic_update(redline_path, original_redline, b"".join(redline_lines))
    # Verify the complete redline file, including history.
    if redline_path.read_bytes() != b"".join(redline_lines):
        raise ValueError("redline verification failed")
    if emit:
        print("redline_updated")
    return event_key


def record_feedback(notebooks: Path, observation: dict) -> tuple[str, str] | None:
    """Apply fresh public HR feedback to the current redline R estimate."""
    current = observation["current_state"]
    events = observation.get("events", "")
    if not isinstance(events, str):
        raise ValueError("invalid public events")
    session_file = notebooks / "workflow-state.json"
    if session_file.exists():
        expected_session = json.loads(session_file.read_text()).get("session_id")
        if expected_session and current.get("session_id") != expected_session:
            raise ValueError("observation session mismatch")
    # Half-year talk may mention HR before the salary's actual risk hint. Prefer
    # the explicit public hint, so unrelated praise cannot overwrite R with 0.
    hr = next((part.strip() for part in events.split("\n")
               if part.strip().startswith("闲聊的时候，关系好的HR")), "")
    hr = hr or next((part.strip() for part in events.split("\n") if "HR" in part), "")
    risk_feedback = None
    for phrase, value, certainty in (
        ("雷全要炸", "4", "低"), ("自求多福", "3", "低"),
        ("不少风险", "2", "低"), ("有点风险", "1", "低"),
        ("继续保持", "0", "高"), ("不错", "0", "高"),
    ):
        if phrase in hr:
            risk_feedback = (value, certainty)
            break
    _, lines, populated = read_table(notebooks / "redline-guardian-keeps.tsv", 3)
    prior_risk = risk_from_state(cells(lines[populated[-1]])[1])
    risk = risk_feedback[0] if risk_feedback else text_number(prior_risk)
    record_promotion(notebooks, observation, risk)
    return risk_feedback


def promotion_levels(feedback: str) -> tuple[int, int] | None:
    match = re.search(r"L(\d+)\s*(?:→|.*?晋升至)\s*L(\d+)", feedback)
    if not match:
        return None
    before, after = map(int, match.groups())
    return (before, after) if after == before + 1 else None


def near_cap_metrics(state: dict[str, int], level: int, caps: dict,
                     target: tuple[float, float] | None = None) -> set[str]:
    """Use the same forecast-based margins as redline labels, including exact caps."""
    margins = near_cap_margins(caps.get(level, (0, {}))[1])
    return {metric for metric in CAP_METRICS
            if level in caps and caps[level][1][metric] is not None
            and ceil(caps[level][1][metric] - margins[metric])
            <= state[metric] <= caps[level][1][metric]}


def promotion_target(text: str) -> tuple[float, float] | None:
    match = re.search(r"下次晋升比例推算：S:O=(\d+(?:\.\d+)?)，S:N=(\d+(?:\.\d+)?)", text)
    if match:
        target = float(match[1]), float(match[2])
        if not all(isfinite(value) and value > 0 for value in target):
            raise ValueError("invalid promotion ratio")
        if "精确值：" in text:
            exact = re.search(r"精确值：S:O=(\d+/[1-9]\d*)，S:N=(\d+/[1-9]\d*)(?:；|$)", text)
            if exact is None:
                raise ValueError("malformed exact promotion ratio")
            target = tuple(Fraction(value) for value in exact.groups())
            if any(value <= 0 for value in target):
                raise ValueError("invalid promotion ratio")
        return target
    if "下次晋升比例推算：" in text:
        raise ValueError("malformed promotion ratio record")
    return None


def format_promotion_target(target: tuple[float, float], reason: str) -> str:
    """Validate the serialized forecast before it can enter a notebook."""
    if len(target) != 2 or not all(isfinite(value) and value > 0 for value in target):
        raise ValueError("invalid promotion ratio")
    if not reason or any(char in reason for char in "\t\r\n"):
        raise ValueError("invalid promotion ratio reason")
    exact = tuple(Fraction(str(value)) for value in target)
    display = [(Decimal(v.numerator) / Decimal(v.denominator)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
               for v in exact]
    text = (f"下次晋升比例推算：S:O={display[0]}，S:N={display[1]}；理由：{reason}"
            f"；精确值：S:O={exact[0].numerator}/{exact[0].denominator}，S:N={exact[1].numerator}/{exact[1].denominator}")
    promotion_target(text)  # Reject values that round to zero as well.
    return text


def feedback_shortfalls(text: str, notebooks: Path) -> list[str]:
    """Read explicit public review labels; no translation dictionary dependency."""
    talk = re.search(r"【半年谈话】(.*?)(?=半年绩效奖金|晋升通知：|$)", text, re.S)
    if talk is None:
        return []
    labels = {'O': '成果', 'N': '协作', 'S': '技能'}
    return [m for m, label in labels.items()
            if re.search(label + r'(?:不足|欠缺|短板)', talk[1])]


def record_promotion(notebooks: Path, observation: dict, risk: str) -> None:
    events = observation.get("events", "")
    if not any(marker in events for marker in ("【半年谈话】", "半年绩效奖金", "晋升通知：")):
        return
    current = observation["current_state"]
    month = current["time"]["current_month"]
    if type(month) is not int or month < 1:
        raise ValueError("invalid feedback month")
    path = notebooks / "promotion-signal-researcher-keeps.tsv"
    original = path.read_bytes()
    raw_rows = [cells(line) for line in original.splitlines(keepends=True) if line.strip()]
    if not raw_rows:
        raw_rows = [PROMOTION_HEADER]
    header = raw_rows[0]
    if len(header) not in (5, 6, 7) or any(len(row) != len(header) for row in raw_rows):
        raise ValueError("invalid promotion notebook")
    if header[:3] != PROMOTION_HEADER[:3] or "反馈结果" not in header:
        raise ValueError("invalid promotion notebook header")
    caps = fixed_stat_caps()
    feedback_index = header.index("反馈结果")
    inference_index = header.index("晋升比例推算") if "晋升比例推算" in header else None
    rows = [row[:3] + [row[feedback_index], row[inference_index] if inference_index is not None else ""]
            for row in raw_rows[1:]]
    if inference_index is None:
        inferred_level = 1
        for row in rows:
            levels = promotion_levels(row[3])
            if levels:
                inferred_level = levels[1]
            elif "晋升通知：" in row[3]:
                inferred_level = min(10, inferred_level + 1)
            row[4] = format_promotion_target(fixed_promotion_target(inferred_level),
                                             f"固定表，当前L{inferred_level}")
    matches = [i for i, row in enumerate(rows) if row[0] == f"第{month}月"]
    if len(matches) > 1:
        raise ValueError("duplicate promotion month")
    level = level_number(current["status"]["level"])
    if level is None:
        raise ValueError("invalid promotion level")
    inference = format_promotion_target(fixed_promotion_target(level), f"固定表，当前L{level}")
    if matches and any(marker in rows[matches[0]][3] for marker in
                       ("评审前日志推算", "未晋升，记录当前观察状态")):
        if rows[matches[0]][4] != inference:
            rows[matches[0]][4] = inference
            replacement = ("\t".join(PROMOTION_HEADER) + "\n"
                           + "".join("\t".join(saved) + "\n" for saved in rows)).encode()
            atomic_update(path, original, replacement)
        return  # Observe/review may replay after redline has consumed the boundary.
    grade = re.search(r"半年绩效奖金[（(]([A-Z])[）)]", events)
    feedback = [grade[1]] if grade else []
    feedback.extend(part.strip() for part in events.splitlines() if "晋升通知：" in part)
    shortfalls = feedback_shortfalls(events, notebooks)
    if shortfalls:
        feedback.append("半年谈话" + "、".join(shortfalls) + "短板线索")
    if not feedback:
        feedback = ["已收到半年谈话" if "【半年谈话】" in events else "未明确"]
    status = current["status"]
    window = (month // 6 + 1) * 6
    feedback_text = "；".join(feedback)
    promoted = "晋升通知：" in feedback_text
    base_level = max(1, level - 1) if promoted else level
    if not promoted:
        # A non-promotion is an observation, not a successful pre-promotion sample.
        # Public status already includes any actual review/monthly settlement.
        state_values = {metric: number(status[METRICS[metric]]) for metric in CAP_METRICS}
        if any(value is None for value in state_values.values()):
            raise ValueError("missing observed promotion metrics")
        feedback.append("未晋升，记录当前观察状态")
    else:
        _, review_lines, review_rows = read_table(notebooks / "redline-guardian-keeps.tsv", 3)
        _, pending_state, pending_label = cells(review_lines[review_rows[-1]])
        expected = parse_expected(pending_state) if pending_label == "待判定" else {}
        state_values = {metric: None for metric in CAP_METRICS}
        if expected.get("L") == base_level:
            for metric in CAP_METRICS:
                value = expected[metric]
                cap = caps[base_level][1][metric]
                state_values[metric] = min(value, cap) if value is not None and cap is not None else value
            risk = text_number(expected["R"])
            feedback.append("评审前日志推算（已扣已知封顶；未知封顶项为估计，R为估计）")
        else:
            feedback.append("评审前状态未知（缺少跨月行动复核，不用扣分后状态替代）")
    state = ";".join(f"{metric}{text_number(state_values[metric])}" for metric in ("O", "S", "N")) + f";R{risk}"
    feedback_text = "；".join(feedback)
    row = [f"第{month}月", state, str(window), feedback_text, inference]
    if matches:
        rows[matches[0]] = row
    else:
        rows.append(row)
    replacement = ("\t".join(PROMOTION_HEADER) + "\n"
                   + "".join("\t".join(saved) + "\n" for saved in rows)).encode()
    if replacement != original:
        atomic_update(path, original, replacement)


def choices_for_translation(observation: dict) -> list[dict]:
    """Omit the quarterly skip when an affordable harmless action exists."""
    choices = observation.get("choices") or []
    event = observation.get("current_event") or {}
    if (event.get("decision_type") != "energy_action" or not choices
            or not all(isinstance(c.get("status_updates"), dict) for c in choices)):
        return choices
    energy = observation["current_state"]["status"]["energy"]
    if type(energy) is not int or energy < 0:
        raise ValueError("invalid remaining energy")
    harmless = any(
        c.get("action_id") != "no_action"
        and type(c.get("energy_cost")) is int and c["energy_cost"] <= energy
        and all(type(delta) is int and (delta <= 0 if key.lower() in ("hiddenrisk", "r") else delta >= 0)
                for key, delta in c["status_updates"].items())
        for c in choices
    )
    return ([c for c in choices if c.get("action_id") != "no_action"]
            if harmless else choices)


def refresh_observation(notebooks: Path, observation_path: Path, *, emit: bool = True) -> None:
    """Consume the public observation and refresh state plus feedback notebooks."""
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    if not isinstance(observation, dict):
        raise ValueError("invalid observation")
    translation = runpy.run_path(str(Path(__file__).with_name("translation-state.py")))
    if isinstance(observation.get("ending_score"), dict):
        session_id = translation["bound_session"](notebooks)
        observed_session = (observation.get("current_state") or {}).get("session_id", session_id)
        if observed_session != session_id:
            raise ValueError("terminal session mismatch")
        translation["save"](notebooks / "ending.json", {
            "session_id": session_id, "ending_score": observation["ending_score"]})
        return
    if (observation.get("current_event") or {}).get("title") == "新员工手册":
        raise ValueError("initialization must finish before observe workflow")
    month = observation["current_state"]["time"]["current_month"]
    if type(month) is not int or month < 1:
        raise ValueError("invalid observation month")
    risk_feedback = record_feedback(notebooks, observation)
    current = observation["current_state"]
    events = observation.get("events", "")
    previous_key = refresh(notebooks, current["status"], str(int(bool(events.strip()))),
                           risk_override=int(risk_feedback[0]) if risk_feedback else None, emit=False)
    review_path = notebooks / REVIEW_CONTEXT
    original = review_path.read_bytes() if review_path.exists() else None
    choices = choices_for_translation(observation)
    context = {"event_key": f"{int(previous_key) + 1:05d}",
               "current_state": current, "choices": choices}
    atomic_update(review_path, original, (json.dumps(context, ensure_ascii=False) + "\n").encode())
    if choices:
        translation["prepare"](notebooks, {**observation, "choices": choices}, context["event_key"])
    if emit:
        print("redline_updated")



if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: refresh-context.py <observe_json_path>", file=sys.stderr)
        sys.exit(2)
    try:
        observation_path = Path(sys.argv[1])
        import runpy
        state = runpy.run_path(str(Path(__file__).with_name('translation-state.py')))
        state['begin_stage']('observe')
        refresh_observation(NOTEBOOKS, observation_path, emit=False)
        result = state['finish_stage']('observe')
        if result['phase'] == 'terminal':
            print('terminal')
        else:
            state['begin_stage']('analyse')
            task = state['current_task']()
            if all(isinstance(choice.get('status_updates'), dict) for choice in task['choices']):
                state['finish_stage']('analyse')
                state['begin_stage']('decide')
                print('可直接进入 decide 阶段 read-context')
            else:
                print('请进入 dispatch 阶段，分配任务')
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"redline_update_error: {error}", file=sys.stderr)
        sys.exit(3)
