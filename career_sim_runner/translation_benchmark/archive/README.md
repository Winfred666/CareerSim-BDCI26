# Observation Translator Benchmark

Run a fixed-seed ordered sample of official CareerSim event nodes through one
fresh Jiuwen session:

```bash
uv run python -m career_sim_runner.translation_benchmark --limit 20
```

Use `--mode dev --limit 20` for a sequential sample that attributes dictionary
rows, `--mode dev-batch --limit 64` for a bounded-batch fixed-seed probe, or
`--mode dev-full` to translate every decision node in the installed dev action
pool. Add `--offset 128 --limit 64` to probe a later fixed-seed slice. Full
mode forces all cases and rejects `--unseen-only` and nonzero `--offset`. The dev report
shows `dictionary mapping lines / tested options` as the compression rate,
along with distinct event activations per row. Its `dictionary-activation.tsv`
adds a third `activation time` column only in benchmark evidence. The live
dictionary stays two-column. Attribution reflects which source line IDs the
translator claims to have applied, deduplicated within each event; it is not
an independent correctness judgment.
General rows describe one metric; special-scenario rows at the end may bundle
several comma-separated effects. Check each component against the option:
only applicable components contribute, and independent mechanisms add.
Deduplicate a general and specific row's overlapping mechanism per metric,
while retaining other applicable components. A bundled row may be attributed
under several metrics but still counts once per event. Any uncertain
component makes that metric's combined effect uncertain. The compression
denominator counts tested options even when an option activates several rows.
Keep the action phrase reusable; add the shortest necessary `【context】` when
an atomic row would otherwise be ambiguous, and move long scenario clauses
into those brackets.
Full mode uses bounded batches with fresh Jiuwen sessions and at most four
concurrent batches to avoid carrying the entire pool in one model context.
Each event includes the same public-text dictionary shortlist produced by
the live observe refresher; the translator must verify each suggested row's
context and may use other rows from the full dictionary.
Evidence is written in fixed case order after each concurrent group. Each event still has its own comparison
and case-specific session identifier in `events.md`.
Use `--batch-size 1` with `dev-batch` or `dev-full` to match the live
one-event-per-observe context; record batch size when comparing scores.

Direction cosine uses weighted signed vectors: D×0, R×5, and every other
metric ×1, with options equally weighted. More than 65% is the current
acceptance threshold. `R+` means hidden risk increases; `R-` means it
decreases. The prompt favors repeated signs such as `++`; numbered `+2` is
accepted as the same effect.

Use `--unseen-only` to exclude every case ID already recorded in
`benchmark.sqlite3` before applying the seed ordering and limit. The report
records the exclusion count and a hash of the exact exclusion set.

The driver resolves `observation-translator` from the selected solution's
current root `SKILL.md`, extracts that role's dictionary-based translation step
regardless of its number, and reads the
current event dictionary on every run. It records hashes for all three source
artifacts and the extracted rule text. It never writes to the solution and scores structural H/D/S/N/O/W/R output
against the event pool's hidden `status_updates`. Only file writing, feedback
bookkeeping, routing, and game tools are omitted from the live solution prompt.
Runtime evidence is written beneath
`.career_sim_runner/translation_benchmark/`; `benchmark.md` maps each event
shorttitle to its wrong option numbers, `events.md` shows each exact input
observation and prompt alongside the raw and parsed Jiuwen output, and
`events.jsonl` retains low-level stream events. `benchmark.sqlite3` is the
structural cross-run ledger. The run directory does not emit transcript or JSON
result/summary files. Batched dev runs also retain `review-attempts.jsonl` when
sparse-option reviews were requested; each row records whether the replacement
was accepted and the raw response, including rejected attempts. The driver does
not ask an LLM to judge another LLM's
answer. Ground truth stores only real effects: false-positive predictions
for no-effect metrics reduce the metric score but do not by themselves add an
option to the wrong-option list. Each logged mismatch uses the compact form
`1. option text (GT:H+ N++) (Pred:S+ N+?)`: Pred preserves every non-unknown
translator effect, including uncertainty markers and false positives. Use
`--limit 0` to test every decision node.
The default `--transport model` uses the configured Player model directly,
with only the solution translation rules, dictionary, public cases and JSON
schema. It disables thinking and unrelated Agent tools/workspace context;
these runtime changes are recorded in `prompt-cohorts.json`. No model review
or analytical calls are made. Python validates, scores, persists each case and
emits `automated-audit.md` and `directional-errors.tsv` after completion.

`translation-attempts.jsonl` retains exact prompts, responses, errors and known
usage for all minimal-runtime attempts. A failed request has at most one retry;
valid cases in a partially malformed batch are salvaged and cached. Incorrect
translations are scored, never retranslated to improve them. A service error
is not scored as a semantic omission. Explicit `--continue-current-source`
continues only remaining cases after an authorized source revision, preserving
old results and recording separate cohorts. Default resume rejects a source
change.

Alongside exact metric matching, the report includes `综合分（主方向余弦）`.
Each option becomes a signed H/D/S/N/O/W/R vector: missing or unknown is 0,
`+?` is 0.5, `+` is 1, `++?` is 1.5, `++` is 2, and larger or negative
effects follow the same rule. The benchmark computes signed cosine similarity
per option and averages options equally. Two zero vectors score 1; one zero
vector scores 0. FP/FN are counted per option-metric cell: wrong directions
generally count once in both; same-direction magnitude errors are tolerated up
to and including 0.5 levels, then larger overestimates count as FP and larger
underestimates count as FN. FN explicitly excludes the D metric; D remains
eligible for FP and all other scores.

Two additional disjoint counts cover **O/S/N/H/R only**, per option-metric cell:
`omission_count` (遗漏次数) counts nonzero GT with no usable predicted direction
(missing, zero, unknown, or invalid); `reversal_count` (反向次数) counts opposite
nonzero signs. Uncertainty preserves direction; same-direction magnitude errors
and no-effect false positives are excluded. D and W are excluded. Reports and
SQLite retain per-event counts and affected cells, with totals in the run result.
Resume and offline rescore use the same definitions.

Existing ledgered responses can be rescored without Jiuwen:

```bash
uv run python -m career_sim_runner.translation_benchmark --rescore-existing
```

Pass `--run-id <id>` with `--rescore-existing` to update just one run, which is
useful when the scoring weights change during dictionary iteration.
