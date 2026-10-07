"""Dictionary attribution and compression metrics for the dev benchmark."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
from typing import Any


@dataclass(frozen=True)
class DictionaryRow:
    line: int
    scenario: str
    effects: str


def dictionary_rows(source: str) -> list[DictionaryRow]:
    """Count single or bundled mappings, preserving physical source line IDs."""
    rows: list[DictionaryRow] = []
    for line, text in enumerate(source.splitlines(), start=1):
        cells = text.split("\t")
        if len(cells) > 2:
            raise ValueError(f"dictionary line {line} must have two columns")
        if len(cells) != 2 or not all(cell.strip() for cell in cells):
            continue
        if line == 1 and cells == ["职场黑话", "高效指标表达"]:
            continue
        effect = cells[1].strip()
        components = [part.strip() for part in effect.split(",")]
        if not re.fullmatch(r"[OSN]短板线索", effect) and not (
            all(re.fullmatch(r"[HDSNOWR](?:\++|-+|[+-]\d+|=)\??", part) for part in components)
            and len({part[0] for part in components}) == len(components)
        ):
            raise ValueError(f"dictionary line {line} has invalid metric effects: {effect}")
        rows.append(DictionaryRow(line, cells[0], cells[1]))
    return rows


def numbered_dictionary(source: str, rows: list[DictionaryRow]) -> str:
    """Give the translator stable references without changing mapping cells."""
    numbers = {row.line for row in rows}
    return "\n".join(
        f"L{line} {text}" if line in numbers else text
        for line, text in enumerate(source.splitlines(), start=1)
    ) + "\n"


def parse_activations(
    document: dict[str, Any], option_numbers: set[int], rows: list[DictionaryRow]
) -> tuple[dict[int, list[int]], list[str]]:
    """Validate row IDs claimed for each option; absent means no dictionary use."""
    valid = {row.line: {part.strip()[0] for part in row.effects.split(",")} for row in rows}
    claims: dict[int, list[int]] = {}
    errors: list[str] = []
    options = document.get("options", [])
    if not isinstance(options, list):
        return claims, ["dictionary attribution: options must be a list"]
    for option in options:
        if not isinstance(option, dict) or not isinstance(option.get("choice"), int):
            continue
        choice = option["choice"]
        if choice not in option_numbers:
            continue
        raw = option.get("dictionary_lines")
        if isinstance(raw, dict):
            if set(raw) != set("HDSNOWR"):
                errors.append(f"option {choice} dictionary_lines must cover H/D/S/N/O/W/R")
            selected: set[int] = set()
            invalid = False
            for metric, lines in raw.items():
                if metric not in set("HDSNOWR") or not isinstance(lines, list):
                    invalid = True
                    continue
                for value in lines:
                    if not isinstance(value, int) or isinstance(value, bool) or metric not in valid.get(value, set()):
                        invalid = True
                    else:
                        selected.add(value)
            if invalid:
                errors.append(f"option {choice} dictionary_lines contains an invalid metric line")
            claims[choice] = sorted(selected)
        elif isinstance(raw, list):
            if any(not isinstance(value, int) or isinstance(value, bool) or value not in valid for value in raw):
                errors.append(f"option {choice} dictionary_lines contains an invalid line")
                continue
            claims[choice] = sorted(set(raw))
        else:
            errors.append(f"option {choice} dictionary_lines must be a per-metric object or list")
    return claims, errors


def activation_counts(results: list[dict[str, Any]], rows: list[DictionaryRow]) -> dict[int, int]:
    """Count distinct events that applied each row, regardless of option count."""
    counts: Counter[int] = Counter()
    valid = {row.line for row in rows}
    for result in results:
        used = {
            int(line)
            for lines in result.get("dictionary_activations", {}).values()
            for line in lines
            if int(line) in valid
        }
        counts.update(used)
    return {row.line: counts[row.line] for row in rows}


def activation_tsv(source: str, counts: dict[int, int]) -> str:
    """Keep the live dictionary two-column; add a third column in evidence only."""
    output: list[str] = []
    for line, text in enumerate(source.splitlines(), start=1):
        if line == 1:
            output.append(f"{text}\tactivation time")
        elif line in counts:
            output.append(f"{text}\t{counts[line]}")
        else:
            output.append(text)
    return "\n".join(output) + "\n"
