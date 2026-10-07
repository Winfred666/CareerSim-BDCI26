"""Offline per-metric/cohort diagnostics; never calls a translator."""
from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path

from career_sim_runner.translation_benchmark.driver import _saved_fence
from career_sim_runner.translation_benchmark.scoring import directional_error_summary


def audit_run(output: Path) -> dict:
    """Recompute diagnostic tables from immutable GT/Pred evidence only."""
    blocks = re.split(r"(?m)^## (\d+)\. ", (output / "events.md").read_text())
    cohort_file = output / "prompt-cohorts.json"
    cohorts = json.loads(cohort_file.read_text()) if cohort_file.exists() else [{"start": 1, "prompt_version": "legacy"}]
    counts = {}
    errors = []
    events = 0
    for index in range(1, len(blocks), 2):
        position = int(blocks[index])
        block = blocks[index + 1]
        cohort = max((c for c in cohorts if c["start"] <= position), key=lambda c: c["start"])
        name = f"{cohort['start']}:{cohort['prompt_version']}"
        per_metric = counts.setdefault("all", {m: Counter() for m in "OSNHR"})
        comparison = json.loads(_saved_fence(block, "Parsed deterministic comparison"))
        observation = json.loads(_saved_fence(block, "Input observation"))
        gt, pred = comparison["GT"], comparison["Pred"]
        detail = directional_error_summary(gt, pred)
        for metrics in gt.values():
            for m in "OSNHR":
                if metrics.get(m) not in (None, "unknown"):
                    per_metric[m]["nonzero_gt"] += 1
        actions = {str(c["choice"]): c["action"] for c in observation["choices"]}
        for kind in ("omission", "reversal"):
            for choice, metrics in detail[f"{kind}_metrics"].items():
                for metric, values in metrics.items():
                    per_metric[metric][kind] += 1
                    per_metric[metric][f"{kind}_{'positive' if values['expected'].startswith('+') else 'negative'}"] += 1
                    errors.append({"position": position, "cohort": name, "shorttitle": block.split("\n")[0],
                                   "choice": choice, "metric": metric, "kind": kind, **values,
                                   "action": actions[str(choice)],
                                   "description": observation["current_event"].get("description", ""),
                                   "claimed_rows": ",".join(map(str, comparison.get("dictionary_activations", {}).get(str(choice), [])))})
        events += 1
    attempts_path = output / "translation-attempts.jsonl"
    attempts = [json.loads(line) for line in attempts_path.read_text().splitlines()] if attempts_path.exists() else []
    usage = Counter()
    for attempt in attempts:
        u = attempt.get("usage")
        usage["calls"] += 1
        usage["failed_calls"] += bool(attempt.get("error") or attempt.get("missing"))
        usage["usage_unavailable_calls"] += u is None
        if u:
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                usage[key] += u.get(key, 0)
            usage["reasoning_unavailable_calls"] += u.get("reasoning_tokens") is None
            usage["known_reasoning_tokens"] += u.get("reasoning_tokens") or 0
    lines = ["# Automated translation audit", "", f"Events: {events}", "",
             "All retained and newly translated cases are combined below. Runtime/source changes remain in prompt-cohorts.json.", ""]
    for name, metrics in counts.items():
        lines += ["## Combined metric counts", "", "| Metric | GT nonzero | Omissions | Reversals | Omitted gains | Omitted costs |",
                  "|---|---:|---:|---:|---:|---:|"]
        for m, c in metrics.items():
            gain = "negative" if m == "R" else "positive"
            cost = "positive" if m == "R" else "negative"
            lines.append(f"| {m} | {c['nonzero_gt']} | {c['omission']} | {c['reversal']} | "
                         f"{c['omission_'+gain]} | {c['omission_'+cost]} |")
    lines += ["", "## Minimal-runtime request accounting", "", "```json", json.dumps(usage, indent=2), "```", "",
              "Unavailable reasoning usage is not a measured zero. All attempts are counted, not only accepted outputs."]
    (output / "automated-audit.md").write_text("\n".join(lines) + "\n")
    if errors:
        with (output / "directional-errors.tsv").open("w") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(errors[0]), delimiter="\t")
            writer.writeheader()
            writer.writerows(errors)
    return {"events": events, "cohorts": counts, "request_usage": dict(usage)}
