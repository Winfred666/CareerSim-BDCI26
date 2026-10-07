"""Offline score backfill for existing translation runs."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from career_sim_runner.translation_benchmark.scoring import (
    classification_errors,
    directional_error_summary,
    option_direction_scores,
    parse_json_object,
    predicted_options,
)
from career_sim_runner.translation_benchmark.driver import parse_batch_response
from career_sim_runner.translation_benchmark.storage import Ledger


def _percent(score: float) -> str:
    return f"{100 * score:.2f}%"


def _upsert_summary(lines: list[str], prefix: str, value: str, after_prefix: str) -> None:
    """Replace one summary line or insert it after another stable summary line."""
    indices = [index for index, line in enumerate(lines) if line.startswith(prefix)]
    if indices:
        lines[indices[0]] = value
        for index in reversed(indices[1:]):
            lines.pop(index)
        return
    insert_at = next((index + 1 for index, line in enumerate(lines) if line.startswith(after_prefix)), 1)
    lines.insert(insert_at, value)


def _patch_report(
    path: Path,
    overall: float,
    per_position: dict[int, float],
    option_total: int,
    false_positive_count: int,
    false_negative_count: int,
    per_position_classification: dict[int, tuple[int, int]],
    details: dict[int, dict],
) -> None:
    """Add or refresh all offline scores without disturbing GT/Pred details."""
    if not path.is_file():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    summary = f"- 综合分（主方向余弦）: {_percent(overall)} (mean signed cosine across {option_total} options)"
    rule = (
        "- 综合分规则: per-option signed weighted cosine (D×0, R×5, others×1); "
        "unknown=0, +?=0.5, +=1, ++?=1.5, ++=2 or +2; options equally weighted. "
        "超过 65% 视为可接受。"
    )
    _upsert_summary(lines, "- 综合分（主方向余弦）:", summary, "- GT-effect-correct options:")
    _upsert_summary(lines, "- 综合分规则:", rule, "- 综合分（主方向余弦）:")
    _upsert_summary(
        lines, "- R sign:",
        "- R sign: `R+` = hidden risk increases; `R-` = hidden risk decreases.",
        "- Dictionary SHA256:",
    )
    _upsert_summary(
        lines, "- FP（多报/夸大）:", f"- FP（多报/夸大）: {false_positive_count}", "- GT-effect-correct options:"
    )
    _upsert_summary(
        lines,
        "- FN（漏报/低估",
        f"- FN（漏报/低估；排除 D）: {false_negative_count}",
        "- FP（多报/夸大）:",
    )
    _upsert_summary(
        lines,
        "- FP/FN 规则:",
        "- FP/FN 规则: 按选项×指标格计数；反向通常同时计 FP 和 FN；同方向幅度允许 ±0.5 档，超过容差才计 FP/FN；FN 明确排除 D 指标。",
        "- FN（漏报/低估",
    )

    for key, label in (("omission_count", "遗漏"), ("reversal_count", "反向")):
        prefix = f"- {label}次数（O/S/N/H/R）:"
        _upsert_summary(lines, prefix, f"{prefix} {sum(d[key] for d in details.values())}", "- FP/FN 规则:")
    _upsert_summary(lines, "- 遗漏/反向规则:",
                    "- 遗漏/反向规则: 仅 O/S/N/H/R，按选项×指标格计数；GT 非零且预测无可用方向为遗漏；符号相反为反向；同向幅度误差不计。",
                    "- FP/FN 规则:")
    header_index = next(
        (index for index, line in enumerate(lines) if line.startswith("| # | 事件 shorttitle |")),
        None,
    )
    if header_index is not None:
        old_headers = [cell.strip() for cell in lines[header_index].split("|")[1:-1]]
        new_headers = ["#", "事件 shorttitle", "指标分", "综合分", "FP", "FN", "遗漏", "反向", "错误选项", "结构错误"]
        lines[header_index] = "| " + " | ".join(new_headers) + " |"
        lines[header_index + 1] = "|---:|---|---:|---:|---:|---:|---:|---:|---|---|"
        index = header_index + 2
        while index < len(lines) and lines[index].startswith("|"):
            cells = [cell.strip() for cell in lines[index].split("|")[1:-1]]
            values = dict(zip(old_headers, cells, strict=False))
            try:
                position = int(values.get("#", ""))
            except ValueError:
                index += 1
                continue
            fp_count, fn_count = per_position_classification[position]
            values.update(
                {
                    "综合分": _percent(per_position[position]),
                    "FP": str(fp_count),
                    "FN": str(fn_count),
                    "遗漏": str(details[position]["omission_count"]),
                    "反向": str(details[position]["reversal_count"]),
                }
            )
            lines[index] = "| " + " | ".join(values.get(header, "—") for header in new_headers) + " |"
            index += 1
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _patch_events(
    path: Path,
    per_position: dict[int, float],
    option_scores: dict[int, dict[int, float]],
    classification_counts: dict[int, tuple[int, int]],
    details: dict[int, dict],
) -> None:
    """Add the per-event direction score to preserved input/output evidence."""
    if not path.is_file():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    output: list[str] = []
    position: int | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        heading = re.match(r"^##\s+(\d+)\.\s", line)
        if heading is not None:
            position = int(heading.group(1))
        if line == "### Parsed deterministic comparison" and position in per_position:
            output.append(line)
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                output.append(lines[index])
                index += 1
            if index < len(lines):
                output.append(lines[index])
                index += 1
                start = index
                while index < len(lines) and not lines[index].startswith("```"):
                    index += 1
                try:
                    comparison = json.loads("\n".join(lines[start:index]))
                except json.JSONDecodeError:
                    output.extend(lines[start:index])
                else:
                    comparison["direction_cosine_by_option"] = option_scores[position]
                    comparison["direction_score"] = per_position[position]
                    comparison.update(details[position])
                    output.extend(json.dumps(comparison, ensure_ascii=False, indent=2).splitlines())
            continue
        output.append(line)
        if line.startswith("- Metric score:") and position in per_position:
            index += 1
            score_prefixes = (
                "- FP（多报/夸大）:",
                "- FN（漏报/低估",
                "- 综合分（主方向余弦）:",
                "- 主方向余弦（选项）:",
                "- 遗漏次数", "- 反向次数",
            )
            while index < len(lines) and lines[index].startswith(score_prefixes):
                index += 1
            output.append(f"- 遗漏次数（O/S/N/H/R）: {details[position]['omission_count']}")
            output.append(f"- 反向次数（O/S/N/H/R）: {details[position]['reversal_count']}")
            fp_count, fn_count = classification_counts[position]
            output.append(f"- FP（多报/夸大）: {fp_count}")
            output.append(f"- FN（漏报/低估；排除 D）: {fn_count}")
            score_line = f"- 综合分（主方向余弦）: {_percent(per_position[position])}"
            output.append(score_line)
            choices_line = "- 主方向余弦（选项）: " + ", ".join(
                f"{choice}={_percent(score)}" for choice, score in option_scores[position].items()
            )
            output.append(choices_line)
            continue
        index += 1
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def _expected(raw: str) -> dict[int, dict[str, Any]]:
    decoded = json.loads(raw)
    return {int(choice): metrics for choice, metrics in decoded.items()}


def _predicted(response_text: str, case_id: str) -> dict[int, dict[str, str]]:
    try:
        document = parse_batch_response(response_text).get(case_id)
    except ValueError:
        from career_sim_runner.translation_benchmark.minimal import recover_documents
        document = recover_documents(response_text, parse_batch_response).get(case_id)
    if document is None:
        try:
            document = parse_json_object(response_text)
        except ValueError:
            return {}
    return predicted_options(document)


def rescore_existing(output_root: Path, run_id: str | None = None) -> dict[str, Any]:
    """Backfill deterministic scores for every recorded run without calling Jiuwen."""
    database_path = output_root / "benchmark.sqlite3"
    if not database_path.is_file():
        raise FileNotFoundError(f"Translation benchmark ledger not found: {database_path}")
    Ledger(database_path)
    summaries: list[dict[str, Any]] = []
    results_updated = 0
    with sqlite3.connect(database_path) as database:
        runs = list(database.execute(
            "SELECT run_id, output_dir FROM runs WHERE (? IS NULL OR run_id=?) ORDER BY started_at",
            (run_id, run_id),
        ))
        if run_id is not None and not runs:
            raise ValueError(f"Benchmark run not found: {run_id}")
        for selected_run_id, raw_output_dir in runs:
            rows = list(
                database.execute(
                    """
                    SELECT position, case_id, expected_json, response_text
                    FROM results WHERE run_id=? ORDER BY position
                    """,
                    (selected_run_id,),
                )
            )
            per_position: dict[int, float] = {}
            per_position_options: dict[int, dict[int, float]] = {}
            per_position_classification: dict[int, tuple[int, int]] = {}
            details: dict[int, dict] = {}
            option_scores: list[float] = []
            run_false_positive_count = 0
            run_false_negative_count = 0
            for position, case_id, expected_json, response_text in rows:
                expected = _expected(expected_json)
                predicted = _predicted(response_text, case_id)
                detail = directional_error_summary(expected, predicted)
                details[int(position)] = detail
                database.execute(
                    "UPDATE results SET omission_count=?, reversal_count=?, omission_metrics_json=?, reversal_metrics_json=? WHERE run_id=? AND position=?",
                    (detail["omission_count"], detail["reversal_count"],
                     json.dumps(detail["omission_metrics"], ensure_ascii=False, sort_keys=True),
                     json.dumps(detail["reversal_metrics"], ensure_ascii=False, sort_keys=True), selected_run_id, position),
                )
                scores = option_direction_scores(expected, predicted)
                false_positives, false_negatives = classification_errors(expected, predicted)
                false_positive_count = sum(len(metrics) for metrics in false_positives.values())
                false_negative_count = sum(len(metrics) for metrics in false_negatives.values())
                case_score = sum(scores.values()) / len(scores) if scores else 0.0
                per_position[int(position)] = case_score
                per_position_options[int(position)] = scores
                option_scores.extend(scores.values())
                per_position_classification[int(position)] = (false_positive_count, false_negative_count)
                run_false_positive_count += false_positive_count
                run_false_negative_count += false_negative_count
                database.execute(
                    """
                    UPDATE results SET direction_score=?, direction_scores_json=?,
                        false_positive_count=?, false_negative_count=?,
                        false_positive_metrics_json=?, false_negative_metrics_json=?
                    WHERE run_id=? AND position=?
                    """,
                    (
                        case_score,
                        json.dumps(scores, ensure_ascii=False, sort_keys=True),
                        false_positive_count,
                        false_negative_count,
                        json.dumps(false_positives, ensure_ascii=False, sort_keys=True),
                        json.dumps(false_negatives, ensure_ascii=False, sort_keys=True),
                        selected_run_id,
                        position,
                    ),
                )
                results_updated += 1
            overall = sum(option_scores) / len(option_scores) if option_scores else 0.0
            database.execute(
                """
                UPDATE runs SET direction_score=?, direction_option_total=?,
                    false_positive_count=?, false_negative_count=? WHERE run_id=?
                """,
                (overall, len(option_scores), run_false_positive_count, run_false_negative_count, selected_run_id),
            )
            new_totals = {key: sum(d[key] for d in details.values()) for key in ("omission_count", "reversal_count")}
            database.execute("UPDATE runs SET omission_count=?, reversal_count=? WHERE run_id=?",
                             (new_totals["omission_count"], new_totals["reversal_count"], selected_run_id))
            output_dir = Path(raw_output_dir)
            _patch_report(
                output_dir / "benchmark.md",
                overall,
                per_position,
                len(option_scores),
                run_false_positive_count,
                run_false_negative_count,
                per_position_classification,
                details,
            )
            _patch_events(output_dir / "events.md", per_position, per_position_options, per_position_classification, details)
            summaries.append(
                {
                    **new_totals,
                    "run_id": selected_run_id,
                    "cases_scored": len(rows),
                    "options_scored": len(option_scores),
                    "direction_score": overall,
                    "accuracy_acceptable": overall > 0.65,
                    "false_positive_count": run_false_positive_count,
                    "false_negative_count": run_false_negative_count,
                }
            )
    return {
        "ok": True,
        "mode": "offline_rescore",
        "runs_updated": len(summaries),
        "results_updated": results_updated,
        "runs": summaries,
    }
