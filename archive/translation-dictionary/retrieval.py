"""Archived public-text dictionary retrieval; not loaded by the live solution."""
from pathlib import Path
from collections import Counter
import re

def dictionary_matches(notebooks: Path, observation: dict) -> list[str]:
    """Retrieve a few existing scenario entries using only the public question."""
    choices = observation.get("choices") or []
    if choices and all(isinstance(c.get("status_updates"), dict) for c in choices):
        return []
    path = notebooks / "event-translator-dictionary.tsv"
    if not path.exists():
        return []
    event = observation.get("current_event") or {}
    text = " ".join([str(event.get("title", "")), str(event.get("description", "")),
                     *(str(c.get("action", "")) for c in choices)]).lower()
    rows = []
    for index, row in enumerate(path.read_text().splitlines()[1:]):
        key, sep, value = row.partition("\t")
        if sep and value and not value.endswith("短板线索"):
            rows.append((index, row, key))

    def bigrams(phrase: str) -> set[str]:
        return {
            word[i:i + 2]
            for word in re.findall(r"[\u4e00-\u9fff]+", phrase)
            for i in range(len(word) - 1)
        }

    public_bigrams = bigrams(text)
    frequency = Counter(part for _, _, key in rows for part in bigrams(key))
    matches = []
    for index, row, key in rows:
        score = 0
        for token in re.findall(r"[a-z][a-z0-9_-]{1,}|[\u4e00-\u9fff]{2,}", key.lower()):
            if token in text:
                score = max(score, len(token))
            elif re.fullmatch(r"[\u4e00-\u9fff]+", token):
                for size in range(min(12, len(token)), 2, -1):
                    if any(token[i:i + size] in text for i in range(len(token) - size + 1)):
                        score = max(score, size)
                        break
        if not score:
            rare = [frequency[part] for part in bigrams(key) & public_bigrams
                    if frequency[part] <= 8 and part not in {"上班", "工作", "时间"}]
            if rare:
                score = 2 + (9 - min(rare)) / 10
        if score:
            matches.append((-score, index, row))
    # Balance all metrics in a row, including bundled special-scenario effects.
    def row_metrics(row: str) -> set[str]:
        return set(re.findall(r"(?:^|,)\s*([HDSNOWR])", row.rsplit("\t", 1)[-1]))

    selected = []
    metric_counts = {metric: 0 for metric in "HDSNOWR"}
    for negative_score, _, row in sorted(matches):
        metrics = row_metrics(row)
        if negative_score > -3 or not any(metric_counts[metric] < 4 for metric in metrics):
            continue
        selected.append(row)
        for metric in metrics:
            metric_counts[metric] += 1
    # One rare short cue can fill a metric that had no stronger lexical hit.
    for negative_score, _, row in sorted(matches):
        metrics = row_metrics(row)
        if negative_score <= -3 or not any(metric_counts[metric] == 0 for metric in metrics):
            continue
        selected.append(row)
        for metric in metrics:
            metric_counts[metric] += 1
    return selected


def dictionary_matches_by_choice(notebooks: Path, observation: dict) -> dict[int, list[str]]:
    """Use each option's action; the whole-case hints separately cover context."""
    matches: dict[int, list[str]] = {}
    for choice in observation.get("choices") or []:
        number = choice.get("choice") if isinstance(choice, dict) else None
        if type(number) is not int:
            continue
        matches[number] = dictionary_matches(
            notebooks, {**observation, "current_event": {}, "choices": [choice]}
        )
    return matches


