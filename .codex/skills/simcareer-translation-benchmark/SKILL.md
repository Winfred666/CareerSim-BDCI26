---
name: simcareer-translation-benchmark
description: Evaluate CareerSim's current O/N/S/HW/R questionnaire agents against hidden dev deltas in isolated parallel API contexts. Report weighted cosine, errors and all-attempt token usage; compare matching saved dictionary results. Never use for live coaching.
---

# CareerSim parallel translation benchmark

Run at the repository root. Use the configured `deepseek-flash` Player directly;
no Jiuwen team workspace or live game is needed. Do not touch a coach session.

## Responsibility and context

- For each event, five agents independently answer **O, N, S, HW, R**. H/W share
  one agent; D is excluded. Each group has its own conversation and script state.
- `solution/skills/observe-decide-review/scripts/questionnaires/` is the sole
  translation policy. `ready` supplies public history, event/options and the
  current question. Scripts advance the questions and save numeric results.
- API tools replace the shell transport with a typed `submit` schema for the
  current group's answers. This is a recorded format change, not extra policy.
  The wake is `请调用脚本回答新问题`; no event/question ID is model-authored.
- Python aggregates the five results and scores against GT. GT, expected deltas,
  correctness hints, other agents' answers and previous benchmark findings must
  never enter an agent's request. The active Codex agent interprets saved evidence.
- Never ask translators to review, self-check, repair semantic omissions, score
  themselves, explain errors, compare with GT or suggest policy changes. Zero or
  incorrect answers are valid observations, never a reason for another call.
- Public prior history is reconstructed only for a unique path. Merge nodes keep
  their fixed-seed position and explicitly receive unavailable history. Do not
  choose a branch or discard a difficult event to improve the sample.

## Run and resume

```bash
uv run python -m career_sim_runner.translation_benchmark --limit 20 --pilot
```

The pilot saves the first event. Inspect its actual requests, JSON, input/output
and reasoning usage before continuing **that same run**:

```bash
uv run python -m career_sim_runner.translation_benchmark --limit 20 --resume-run <run-id>
```

Resume uses the saved `runtime/` candidate and verifies its hashes; live policy edits do not change a running evaluation.

Use `--limit 0` for the whole dev pool. Default seed is
`career-sim-translation-v1`. Do not silently change model, policy or selected
cases. `--solution`/`--skill-id` selects only a user-designated candidate.

Use one benchmark process, protected by its file lock. When execution yields,
wait on that process. Reuse every saved valid answer, including incorrect ones
and successful groups from a partly failed event. Persist accepted answers
before advancing script state. An interrupted, unaccounted request has unknown
usage, not zero.

Only transport failure or unusable answer structure may retry automatically,
at most once per request and transport cohort. After two failures, inspect the
saved request/runtime before any further dispatch. A necessary format-only
harness repair uses `--continue-transport` to record the cohort boundary and
retain all successful answers; never retranslates them. No nested retry loops.
A clean rerun of completed translations requires explicit user authorization.

## Evidence and comparison

Evidence lives under `.career_sim_runner/translation_benchmark/<run>/`:

- `manifest.json`, `runtime/`: model/settings, source hashes and frozen scripts.
- `events/<n>/<group>/attempts.jsonl`: every actual request, raw reply, failure,
  timing and available usage. `conversation.json` retains independent context.
- `events/<n>/notebooks/translations/`: script-owned event, cursor, answers,
  completion receipts and numeric aggregate; no GT in agent state.
- `predictions.json`, `events/<n>/comparison.json`: saved translations and
  deterministic GT comparisons; translators cannot read comparison files.
- `metrics.json`, `benchmark.md`: aggregate/per-metric scores, wrong options,
  calls/retries, input/output/reasoning/cached tokens, missing-usage counts.
- `baseline.json`: original saved dictionary rows and baseline selection.

Report weighted signed cosine (R weight 5, D 0, others 1; >65% is the historical
acceptance threshold), six-metric accuracy, fully-correct-option accuracy, FP/FN,
omissions and reversals. Accuracy includes zero-valued cells and tolerates up to
0.5 magnitude error; full option accuracy requires **all six metrics** correct.
FP/FN exclude D. Omissions/reversals count O/S/N/H/R cells, ignore magnitude and
exclude D/W. Give per-metric diagnosis and failing titles/option numbers.

Account for **all attempts**, even failed/interrupted ones. Report unknown usage
and reasoning separately; a missing field is not zero. Distinguish summed model
time from parallel wall-clock time and analyst work.

By default choose the best **completed full-pool** historical dictionary run by
its full-pool cosine, then extract the same current case IDs. `--baseline-run`
can name a specific saved run. Never choose the best baseline on the tested
subset. Rescore unchanged historical responses using today's six-metric scorer;
verify identical GT. Historical batch/review token billing may not be attributable
to the 20 individual events; label those tokens as attributed ledger usage, not
a controlled same-event cost ratio. Record context/history/runtime differences.
This iterated dev pool is in-sample; a gain does not establish generalization.

Dictionary search, attribution, compression and old dev modes are archived in
`career_sim_runner/translation_benchmark/archive/` and
`archive/translation-dictionary/`. They are not part of the current CLI. The old
SQLite ledger and all original benchmark artifacts remain read-only evidence.
