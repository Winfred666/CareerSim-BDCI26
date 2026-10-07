"""Human-readable report rendering for translation benchmark results."""

from __future__ import annotations

import json
from typing import Any

from career_sim_runner.translation_benchmark.cases import METRICS
from career_sim_runner.translation_benchmark.scoring import (
    classification_errors, directional_error_summary, option_direction_scores,
)


def _percent(correct: int, total: int) -> str:
    return f"{(100 * correct / total):.2f}%" if total else "n/a"


def _direction_percent(score: float) -> str:
    return f"{100 * score:.2f}%"


def _cell(value: object) -> str:
    return str(value).replace("|", "／").replace("\n", " ")


def _choice_values(values: dict[Any, Any], choice: int) -> dict[str, str]:
    selected = values.get(choice, values.get(str(choice), {}))
    return selected if isinstance(selected, dict) else {}


def _effect_text(values: dict[str, str], metrics: list[str]) -> str:
    return " ".join(f"{metric}{values[metric]}" for metric in metrics)


def _direction_scores(result: dict[str, Any]) -> dict[int, float]:
    stored = result.get("direction_scores")
    if isinstance(stored, dict):
        return {int(choice): float(score) for choice, score in stored.items()}
    expected = {int(choice): values for choice, values in result.get("expected", {}).items()}
    predicted = {int(choice): values for choice, values in result.get("predicted", {}).items()}
    return option_direction_scores(expected, predicted)


def _direction_score(result: dict[str, Any]) -> float:
    scores = _direction_scores(result)
    return sum(scores.values()) / len(scores) if scores else 0.0


def _classification_counts(result: dict[str, Any]) -> tuple[int, int]:
    """Return stored or deterministically reconstructed FP/FN metric-cell counts."""
    if "false_positive_count" in result and "false_negative_count" in result:
        return int(result["false_positive_count"]), int(result["false_negative_count"])
    expected = {int(choice): values for choice, values in result.get("expected", {}).items()}
    predicted = {int(choice): values for choice, values in result.get("predicted", {}).items()}
    false_positives, false_negatives = classification_errors(expected, predicted)
    return (
        sum(len(metrics) for metrics in false_positives.values()),
        sum(len(metrics) for metrics in false_negatives.values()),
    )


def _logged_mismatches(result: dict[str, Any]) -> dict[int, dict[str, dict[str, str]]]:
    """Return GT-effect mismatches, excluding no-effect false positives."""
    logged: dict[int, dict[str, dict[str, str]]] = {}
    for raw_choice, raw_mismatches in result.get("mismatches", {}).items():
        choice = int(raw_choice)
        mismatches = {
            metric: values for metric, values in raw_mismatches.items() if values.get("expected") != "unknown"
        }
        if mismatches:
            logged[choice] = mismatches
    return logged


def _fenced(title: str, content: str, language: str = "") -> list[str]:
    fence = "```"
    while fence in content:
        fence += "`"
    return [f"### {title}", "", f"{fence}{language}", content, fence, ""]


def render_events(metadata: dict[str, Any], results: list[dict[str, Any]]) -> str:
    """Render exact per-case input/output context and parsed structures."""
    lines = [
        "# Observation Translator Events",
        "",
        f"- Mode: `{metadata.get('mode', 'sample')}`",
        f"- Seed: `{metadata['seed']}`",
        *([f"- Prompt cohorts: `{json.dumps(metadata['prompt_cohorts'], ensure_ascii=False)}`"] if metadata.get("prompt_cohorts") else []),
        *([f"- Fixed-seed offset: {metadata['offset']}"] if metadata.get("offset") else []),
        f"- Jiuwen session: `{metadata['session_id']}`",
        *([f"- Jiuwen batches: {metadata['session_count']}"] if metadata.get("mode") in ("dev-batch", "dev-full") else []),
        *([f"- Events per batch: {metadata['batch_size']}"] if metadata.get("batch_size") else []),
        f"- Dictionary SHA256: `{metadata['dictionary_sha256']}`",
        "- R sign: `R+` = hidden risk increases; `R-` = hidden risk decreases.",
    ]
    if metadata.get("selection_mode") == "ledger_unseen":
        lines.extend(
            [
                f"- Selection: `ledger_unseen` ({metadata['excluded_case_count']} historical case IDs excluded)",
                f"- Exclusion-set SHA256: `{metadata['excluded_case_ids_sha256']}`",
            ]
        )
    if metadata.get("translator_sha256"):
        lines.extend(
            [
                f"- Solution SKILL.md SHA256: `{metadata['solution_skill_sha256']}`",
                f"- Observation Translator path: `{metadata['translator_path']}`",
                f"- Observation Translator SHA256: `{metadata['translator_sha256']}`",
                f"- Extracted translation rules SHA256: `{metadata['translation_rules_sha256']}`",
            ]
        )
    for position, result in enumerate(results, start=1):
        direction_scores = _direction_scores(result)
        direction_score = _direction_score(result)
        false_positive_count, false_negative_count = _classification_counts(result)
        detail = directional_error_summary(result["expected"], result.get("predicted", result["actual"]))
        comparison = {
            **detail,
            "GT": result["expected"],
            "Pred": result["predicted"],
            "normalized_for_score": result["actual"],
            "wrong_options": result["wrong_options"],
            "false_positives": result["false_positives"],
            "false_positive_metrics": result.get("false_positive_metrics", {}),
            "false_negative_metrics": result.get("false_negative_metrics", {}),
            "false_positive_count": false_positive_count,
            "false_negative_count": false_negative_count,
            "mismatches": result["mismatches"],
            "schema_errors": result["errors"],
            "direction_cosine_by_option": direction_scores,
            "direction_score": direction_score,
            "dictionary_activations": result.get("dictionary_activations", {}),
            "jiuwen_session_id": result.get("jiuwen_session_id", metadata["session_id"]),
        }
        lines.extend(
            [
                "",
                f"## {position}. {_cell(result['shorttitle'])}",
                "",
                f"- Case ID: `{result['case_id']}`",
                f"- Jiuwen session: `{result.get('jiuwen_session_id', metadata['session_id'])}`",
                f"- Metric score: {result['metric_correct']}/{result['metric_total']}",
                f"- 遗漏次数（O/S/N/H/R）: {detail['omission_count']}",
                f"- 反向次数（O/S/N/H/R）: {detail['reversal_count']}",
                f"- FP（多报/夸大）: {false_positive_count}",
                f"- FN（漏报/低估；排除 D）: {false_negative_count}",
                f"- 综合分（主方向余弦）: {_direction_percent(direction_score)}",
                "- 主方向余弦（选项）: "
                + ", ".join(f"{choice}={_direction_percent(score)}" for choice, score in direction_scores.items()),
                "",
            ]
        )
        lines.extend(
            _fenced(
                "Input observation",
                json.dumps(result["observation"], ensure_ascii=False, indent=2),
                "json",
            )
        )
        lines.extend(_fenced("Input prompt sent to Jiuwen", result["input_prompt"], "text"))
        lines.extend(_fenced("Raw Jiuwen output", result["response_text"], "text"))
        lines.extend(
            _fenced(
                "Parsed deterministic comparison",
                json.dumps(comparison, ensure_ascii=False, indent=2),
                "json",
            )
        )
    return "\n".join(lines).rstrip() + "\n"


def render_report(metadata: dict[str, Any], results: list[dict[str, Any]]) -> str:
    """Render scores and the wrong option numbers for every event shorttitle."""
    metric_correct = sum(int(result["metric_correct"]) for result in results)
    metric_total = sum(int(result["metric_total"]) for result in results)
    option_correct = sum(int(result["option_correct"]) for result in results)
    option_total = sum(int(result["option_total"]) for result in results)
    total_tokens = sum(int(result["usage"].get("total_tokens", 0)) for result in results)
    direction_scores = [score for result in results for score in _direction_scores(result).values()]
    direction_score = sum(direction_scores) / len(direction_scores) if direction_scores else 0.0
    classification_counts = [_classification_counts(result) for result in results]
    false_positive_count = sum(counts[0] for counts in classification_counts)
    false_negative_count = sum(counts[1] for counts in classification_counts)
    details = [directional_error_summary(r["expected"], r.get("predicted", r["actual"])) for r in results]
    lines = [
        "# Observation Translator Benchmark",
        "",
        f"- Mode: `{metadata.get('mode', 'sample')}`",
        f"- Seed: `{metadata['seed']}`",
        *([f"- Prompt cohorts: `{json.dumps(metadata['prompt_cohorts'], ensure_ascii=False)}`"] if metadata.get("prompt_cohorts") else []),
        *([f"- Fixed-seed offset: {metadata['offset']}"] if metadata.get("offset") else []),
        f"- Jiuwen session: `{metadata['session_id']}`",
        *([f"- Jiuwen batches: {metadata['session_count']}"] if metadata.get("mode") in ("dev-batch", "dev-full") else []),
        *([f"- Events per batch: {metadata['batch_size']}"] if metadata.get("batch_size") else []),
        f"- Dataset: `{metadata['dataset_root']}`",
        f"- Dictionary SHA256: `{metadata['dictionary_sha256']}`",
        "- R sign: `R+` = hidden risk increases; `R-` = hidden risk decreases.",
        *(
            [
                f"- Solution SKILL.md SHA256: `{metadata['solution_skill_sha256']}`",
                f"- Observation Translator path: `{metadata['translator_path']}`",
                f"- Observation Translator SHA256: `{metadata['translator_sha256']}`",
                f"- Extracted translation rules SHA256: `{metadata['translation_rules_sha256']}`",
            ]
            if metadata.get("translator_sha256")
            else []
        ),
        f"- Scoring: `{metadata['scoring_mode']}` (direct structural comparison; no LLM judge)",
        f"- Cases completed: {len(results)}/{metadata['case_count']}",
        f"- Sparse-option review events: {metadata.get('effect_review_count', 0)}",
        f"- Metric score: {metric_correct}/{metric_total} ({_percent(metric_correct, metric_total)})",
        f"- GT-effect-correct options: {option_correct}/{option_total} ({_percent(option_correct, option_total)})",
        f"- 遗漏次数（O/S/N/H/R）: {sum(d['omission_count'] for d in details)}",
        f"- 反向次数（O/S/N/H/R）: {sum(d['reversal_count'] for d in details)}",
        "- 遗漏/反向规则: 按选项×指标格计数，仅 O/S/N/H/R；GT 非零且预测无可用方向为遗漏；符号相反为反向。两者互斥，同向幅度误差不计。",
        f"- FP（多报/夸大）: {false_positive_count}",
        f"- FN（漏报/低估；排除 D）: {false_negative_count}",
        "- FP/FN 规则: 按选项×指标格计数；反向通常同时计 FP 和 FN；同方向幅度允许 ±0.5 档，超过容差才计 FP/FN；FN 明确排除 D 指标。",
        f"- 综合分（主方向余弦）: {_direction_percent(direction_score)} "
        f"(mean signed cosine across {len(direction_scores)} options)",
        "- 综合分规则: per-option signed weighted cosine (D×0, R×5, others×1); "
        "unknown=0, +?=0.5, +=1, ++?=1.5, ++=2 or +2; options equally weighted. "
        "超过 65% 视为可接受。",
        "- Wrong-option selection: GT-effect mismatches only; no-effect false positives affect Metric score "
        "and remain visible in Pred.",
        (f"- Recorded-result tokens (not full request cost; see automated-audit.md): {total_tokens}"
         if metadata.get("prompt_cohorts") else f"- Jiuwen tokens: {total_tokens}"),
        "",
        "| # | 事件 shorttitle | 指标分 | 综合分 | FP | FN | 遗漏 | 反向 | 错误选项 | 结构错误 |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    if metadata.get("mode") in ("dev", "dev-batch", "dev-full"):
        counts = metadata.get("dictionary_activation_counts", {})
        line_count = int(metadata["dictionary_line_count"])
        option_count = int(metadata["option_count"])
        activations = sum(counts.values())
        compression = line_count / option_count if option_count else 0.0
        dev_lines = [
            f"- Compression rate (dictionary lines/options): {line_count}/{option_count} = {compression:.4f}",
            f"- Dictionary activation: {sum(value > 0 for value in counts.values())}/{line_count} lines used; "
            f"{activations} distinct event×line matches; mean {activations / line_count if line_count else 0:.2f} events/line",
            "- Activation counts are translator-attributed, deduplicated within each event; see `dictionary-activation.tsv`.",
        ]
        insert_at = next(i for i, line in enumerate(lines) if line.startswith(("- Jiuwen tokens:", "- Recorded-result tokens")))
        lines[insert_at:insert_at] = dev_lines
    if metadata.get("selection_mode") == "ledger_unseen":
        selection = [
            f"- Selection: `ledger_unseen` ({metadata['excluded_case_count']} historical case IDs excluded)",
            f"- Exclusion-set SHA256: `{metadata['excluded_case_ids_sha256']}`",
        ]
        insert_at = next(i for i, line in enumerate(lines) if line.startswith("- Dictionary SHA256:")) + 1
        lines[insert_at:insert_at] = selection
    for position, result in enumerate(results, start=1):
        false_positive_count, false_negative_count = _classification_counts(result)
        wrong = ", ".join(str(value) for value in _logged_mismatches(result)) or "—"
        errors = "; ".join(result["errors"]) or "—"
        lines.append(
            f"| {position} | {_cell(result['shorttitle'])} | "
            f"{result['metric_correct']}/{result['metric_total']} | "
            f"{_direction_percent(_direction_score(result))} | "
            f"{false_positive_count} | {false_negative_count} | {details[position-1]['omission_count']} | {details[position-1]['reversal_count']} | {wrong} | {_cell(errors)} |"
        )

    wrong_results = [result for result in results if _logged_mismatches(result) or result["errors"]]
    if wrong_results:
        lines.extend(["", "## Wrong option details", ""])
        for result in wrong_results:
            lines.append(f"### {_cell(result['shorttitle'])}")
            lines.append("")
            if result["errors"]:
                lines.append(f"- Schema: {_cell('; '.join(result['errors']))}")
            for choice in _logged_mismatches(result):
                choice_texts = result.get("choice_texts", {})
                choice_text = choice_texts.get(choice, choice_texts.get(str(choice), f"Option {choice}"))
                expected = _choice_values(result["expected"], choice)
                predicted = _choice_values(result.get("predicted", result["actual"]), choice)
                gt_metrics = [metric for metric in METRICS if expected.get(metric) != "unknown" and metric in expected]
                predicted_metrics = [
                    metric for metric in METRICS if predicted.get(metric) not in {None, "unknown", "missing"}
                ]
                line = f"{choice}. {_cell(choice_text)} (GT:{_effect_text(expected, gt_metrics)})"
                if predicted_metrics:
                    line += f" (Pred:{_effect_text(predicted, predicted_metrics)})"
                lines.append(line)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
