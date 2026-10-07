"""Parse and score structured observation translations."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from career_sim_runner.translation_benchmark.cases import METRICS, TranslationCase

UNKNOWN_VALUES = frozenset({"", "0", "none", "null", "unknown", "未知", "无", "无变化"})
CLASSIFICATION_MAGNITUDE_TOLERANCE = 0.5
DIRECTION_WEIGHTS = {metric: (0.0 if metric == "D" else 5.0 if metric == "R" else 1.0) for metric in METRICS}


@dataclass(frozen=True)
class CaseScore:
    """Scored response for one event node."""

    actual: dict[int, dict[str, str]]
    predicted: dict[int, dict[str, str]]
    wrong_options: list[int]
    mismatches: dict[int, dict[str, dict[str, str]]]
    false_positives: dict[int, dict[str, str]]
    false_positive_metrics: dict[int, dict[str, dict[str, str]]]
    false_negative_metrics: dict[int, dict[str, dict[str, str]]]
    false_positive_count: int
    false_negative_count: int
    metric_correct: int
    metric_total: int
    option_correct: int
    option_total: int
    direction_scores: dict[int, float]
    direction_score: float
    errors: list[str]


def parse_json_object(text: str) -> dict[str, Any]:
    """Extract the last complete top-level JSON object from a model response."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    decoder = json.JSONDecoder()
    last_object: dict[str, Any] | None = None
    index = 0
    while index < len(cleaned):
        object_start = cleaned.find("{", index)
        if object_start < 0:
            break
        try:
            value, consumed = decoder.raw_decode(cleaned[object_start:])
        except json.JSONDecodeError:
            index = object_start + 1
            continue
        if isinstance(value, dict) and "options" in value:
            last_object = value
        index = object_start + consumed
    if last_object is not None:
        return last_object
    raise ValueError("response does not contain a JSON object")


def normalize_metric(metric: str, value: Any) -> str:
    """Normalize one model value while treating uncertainty markers as metadata."""
    if value is None:
        return "unknown"
    if isinstance(value, int):
        if value == 0:
            return "unknown"
        return ("+" if value > 0 else "-") * abs(value)
    normalized = str(value).strip()
    if normalized.lower() in UNKNOWN_VALUES or normalized in UNKNOWN_VALUES:
        return "unknown"
    normalized = re.sub(rf"^{metric}\s*[:=]?\s*", "", normalized, flags=re.IGNORECASE)
    normalized = normalized.replace("?", "").strip()
    if normalized in {"", "=", "0"}:
        return "unknown"
    numbered = re.fullmatch(r"([+-])([1-9][0-9]*)", normalized)
    if numbered and int(numbered.group(2)) <= 20:
        return numbered.group(1) * int(numbered.group(2))
    if re.fullmatch(r"\++|-+", normalized):
        return normalized
    return f"invalid:{value}"


def display_metric(metric: str, value: Any) -> str | None:
    """Return the translator's non-unknown value without discarding uncertainty."""
    if normalize_metric(metric, value) == "unknown":
        return None
    if isinstance(value, int):
        return ("+" if value > 0 else "-") * abs(value)
    displayed = str(value).strip()
    displayed = re.sub(rf"^{metric}\s*[:=]?\s*", "", displayed, flags=re.IGNORECASE)
    numbered = re.fullmatch(r"([+-])([1-9][0-9]*)(\?)?", displayed)
    if numbered and int(numbered.group(2)) <= 20:
        return numbered.group(1) * int(numbered.group(2)) + (numbered.group(3) or "")
    return displayed


def effect_value(metric: str, value: Any) -> float:
    """Convert one signed effect into its direction-vector weight."""
    displayed = display_metric(metric, value)
    if displayed is None:
        return 0.0
    match = re.fullmatch(r"([+-]+)(\?)?", displayed.strip())
    if match is None or len(set(match.group(1))) != 1:
        return 0.0
    magnitude = float(len(match.group(1))) - (0.5 if match.group(2) else 0.0)
    return magnitude if match.group(1)[0] == "+" else -magnitude


def direction_cosine(expected: dict[str, Any], predicted: dict[str, Any]) -> float:
    """Return signed weighted cosine, ignoring D and giving R fivefold weight."""
    gt_vector = [effect_value(metric, expected.get(metric)) for metric in METRICS]
    pred_vector = [effect_value(metric, predicted.get(metric)) for metric in METRICS]
    weights = [DIRECTION_WEIGHTS[metric] for metric in METRICS]
    gt_norm = math.sqrt(sum(weight * value * value for weight, value in zip(weights, gt_vector, strict=True)))
    pred_norm = math.sqrt(sum(weight * value * value for weight, value in zip(weights, pred_vector, strict=True)))
    if gt_norm == 0 and pred_norm == 0:
        return 1.0
    if gt_norm == 0 or pred_norm == 0:
        return 0.0
    dot_product = sum(
        weight * gt * pred for weight, gt, pred in zip(weights, gt_vector, pred_vector, strict=True)
    )
    return max(-1.0, min(1.0, dot_product / (gt_norm * pred_norm)))


def option_direction_scores(
    expected: dict[int, dict[str, Any]], predicted: dict[int, dict[str, Any]]
) -> dict[int, float]:
    """Score each expected option by weighted signed direction cosine."""
    return {
        choice: direction_cosine(expected_metrics, predicted.get(choice, {}))
        for choice, expected_metrics in expected.items()
    }


def classification_errors(
    expected: dict[int, dict[str, Any]], predicted: dict[int, dict[str, Any]]
) -> tuple[dict[int, dict[str, dict[str, str]]], dict[int, dict[str, dict[str, str]]]]:
    """Return metric-cell false positives and false negatives.

    A false positive is a guessed effect, a wrong-direction effect, or a
    same-direction effect whose magnitude exceeds GT by more than half a
    level. A false negative is a missed effect, a wrong-direction effect, or a
    same-direction effect whose magnitude is more than half a level below GT.
    Wrong directions therefore count in both groups.
    D is explicitly excluded from false negatives, but remains eligible for
    false positives and all other benchmark scores.
    """
    false_positives: dict[int, dict[str, dict[str, str]]] = {}
    false_negatives: dict[int, dict[str, dict[str, str]]] = {}
    for choice, expected_metrics in expected.items():
        predicted_metrics = predicted.get(choice, {})
        for metric in METRICS:
            expected_display = display_metric(metric, expected_metrics.get(metric)) or "unknown"
            predicted_display = display_metric(metric, predicted_metrics.get(metric)) or "unknown"
            expected_value = effect_value(metric, expected_display)
            predicted_value = effect_value(metric, predicted_display)
            opposite = expected_value * predicted_value < 0
            details = {"expected": expected_display, "predicted": predicted_display}
            magnitude_delta = abs(predicted_value) - abs(expected_value)
            if predicted_value != 0 and (
                expected_value == 0 or opposite or magnitude_delta > CLASSIFICATION_MAGNITUDE_TOLERANCE
            ):
                false_positives.setdefault(choice, {})[metric] = details
            if (
                metric != "D"
                and expected_value != 0
                and (predicted_value == 0 or opposite or magnitude_delta < -CLASSIFICATION_MAGNITUDE_TOLERANCE)
            ):
                false_negatives.setdefault(choice, {})[metric] = details
    return false_positives, false_negatives


def directional_error_summary(expected: dict, predicted: dict) -> dict[str, Any]:
    """Count disjoint omissions/reversals per O/S/N/H/R cell, ignoring magnitude.

    Missing, unknown and invalid predictions have no usable direction and count
    as omissions when GT is nonzero. Uncertainty retains its signed direction.
    Accept integer or JSON string option keys for ledger/resume compatibility.
    """
    omissions: dict[int, dict[str, dict[str, str]]] = {}
    reversals: dict[int, dict[str, dict[str, str]]] = {}
    for choice, metrics in expected.items():
        pred = predicted.get(int(choice), predicted.get(str(choice), {}))
        for metric in "OSNHR":
            gt = effect_value(metric, metrics.get(metric))
            value = effect_value(metric, pred.get(metric))
            target = omissions if gt != 0 and value == 0 else reversals if gt * value < 0 else None
            if target is not None:
                target.setdefault(int(choice), {})[metric] = {
                    "expected": display_metric(metric, metrics.get(metric)) or "unknown",
                    "predicted": display_metric(metric, pred.get(metric)) or "unknown",
                }
    return {
        "omission_count": sum(map(len, omissions.values())),
        "reversal_count": sum(map(len, reversals.values())),
        "omission_metrics": omissions,
        "reversal_metrics": reversals,
    }


def _actual_options(
    document: dict[str, Any], errors: list[str]
) -> tuple[dict[int, dict[str, str]], dict[int, dict[str, str]]]:
    raw_options = document.get("options")
    if not isinstance(raw_options, list):
        errors.append("options must be a list")
        return {}, {}
    actual: dict[int, dict[str, str]] = {}
    predicted: dict[int, dict[str, str]] = {}
    for raw_option in raw_options:
        if not isinstance(raw_option, dict):
            errors.append("option must be an object")
            continue
        raw_choice = raw_option.get("choice")
        if not isinstance(raw_choice, (int, str)):
            errors.append("option choice must be an integer")
            continue
        try:
            choice = int(raw_choice)
        except ValueError:
            errors.append("option choice must be an integer")
            continue
        raw_metrics = raw_option.get("metrics")
        if not isinstance(raw_metrics, dict):
            errors.append(f"option {choice} metrics must be an object")
            raw_metrics = {}
        if choice in actual:
            errors.append(f"duplicate option {choice}")
        unexpected_metrics = sorted(set(raw_metrics) - set(METRICS))
        if unexpected_metrics:
            errors.append(f"option {choice} unexpected metrics: {unexpected_metrics}")
        normalized_metrics: dict[str, str] = {}
        displayed_metrics: dict[str, str] = {}
        for metric in METRICS:
            if metric not in raw_metrics:
                errors.append(f"option {choice} missing metric {metric}")
                normalized_metrics[metric] = "missing"
            else:
                normalized_metrics[metric] = normalize_metric(metric, raw_metrics[metric])
                displayed = display_metric(metric, raw_metrics[metric])
                if displayed is not None:
                    displayed_metrics[metric] = displayed
        actual[choice] = normalized_metrics
        predicted[choice] = displayed_metrics
    return actual, predicted


def predicted_options(document: dict[str, Any]) -> dict[int, dict[str, str]]:
    """Extract sparse displayed predictions, preserving uncertainty markers."""
    _, predicted = _actual_options(document, [])
    return predicted


def score_case(case: TranslationCase, document: dict[str, Any]) -> CaseScore:
    """Compare one structural translation with hidden emulator deltas."""
    errors: list[str] = []
    if str(document.get("shorttitle") or "") != case.shorttitle:
        errors.append("shorttitle does not match the requested event")
    actual, predicted = _actual_options(document, errors)
    expected_choices = set(case.expected)
    extra_choices = sorted(set(actual) - expected_choices)
    if extra_choices:
        errors.append(f"unexpected options: {extra_choices}")

    wrong_options: list[int] = []
    mismatches: dict[int, dict[str, dict[str, str]]] = {}
    false_positives: dict[int, dict[str, str]] = {}
    metric_correct = 0
    option_correct = 0
    for choice, expected_effects in case.expected.items():
        option_mismatches: dict[str, dict[str, str]] = {}
        option_false_positives: dict[str, str] = {}
        actual_metrics = actual.get(choice, {})
        for metric in METRICS:
            expected = expected_effects.get(metric, "unknown")
            received = actual_metrics.get(metric, "missing")
            if received == expected:
                metric_correct += 1
            elif expected != "unknown":
                option_mismatches[metric] = {"expected": expected, "actual": received}
            elif received != "missing":
                option_false_positives[metric] = received
        if option_mismatches:
            wrong_options.append(choice)
            mismatches[choice] = option_mismatches
        else:
            option_correct += 1
        if option_false_positives:
            false_positives[choice] = option_false_positives

    direction_scores = option_direction_scores(case.expected, predicted)
    direction_score = sum(direction_scores.values()) / len(direction_scores) if direction_scores else 0.0
    false_positive_metrics, false_negative_metrics = classification_errors(case.expected, predicted)

    return CaseScore(
        actual=actual,
        predicted=predicted,
        wrong_options=wrong_options,
        mismatches=mismatches,
        false_positives=false_positives,
        false_positive_metrics=false_positive_metrics,
        false_negative_metrics=false_negative_metrics,
        false_positive_count=sum(len(metrics) for metrics in false_positive_metrics.values()),
        false_negative_count=sum(len(metrics) for metrics in false_negative_metrics.values()),
        metric_correct=metric_correct,
        metric_total=len(case.expected) * len(METRICS),
        option_correct=option_correct,
        option_total=len(case.expected),
        direction_scores=direction_scores,
        direction_score=direction_score,
        errors=errors,
    )
