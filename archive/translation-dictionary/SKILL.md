---
name: simcareer-translation-benchmark
description: Evaluate the current observe-stage translation rules and dictionary against official hidden event deltas. Supports fixed-seed samples and the complete dev event pool, with FN, direction cosine, dictionary compression, and per-line event activation evidence. Never use for live coach supervision.
---

# CareerSim Observation Translator benchmark

Run from the CareerSim repository root. The benchmark is independent of the
stateful coach controller: never start, step, inspect, withdraw, or reload a
coach game as part of this workflow.

## Mandatory responsibility boundary

**We use the benchmark to analyze the translator's simplest translation work.
The translator is the subject being measured, never the benchmark analyst.**

- **Translator:** read the supplied public event, current solution translation
  rules and dictionary; translate each option once; return only the required
  JSON. Dev mode may add compact actually-used row IDs in that same response.
- **Benchmark code:** validate JSON, retain the original response, compare it
  deterministically with hidden GT, count errors, and persist evidence.
- **The active Codex agent:** interpret automated checks and review saved evidence; inspect omissions
  and reversals, verify dictionary attribution, diagnose policy/dictionary
  problems, assess uncertainty, and write the analysis. Do this from existing
  evidence; do not send those tasks back to the translator.

Never ask the translator to review, self-check, fill omissions in a prior
answer, explain its reasoning, score itself, analyze failures, compare against
GT, suggest fixes, or iterate until its answer improves. In particular, disable
**sparse-option review**: few effects, all-zero output, low scores, omissions,
and reversals are observations to analyze, not reasons for another model call.
Do not delegate analysis to another translator/model call.

## Minimal prompt and runtime contract

Use only the public observation needed for translation, the current solution's
translation rules and dictionary, and a short JSON schema. Include each once.
The benchmark wrapper specifies output format only; do not layer on stronger
translation heuristics, repeated per-metric checklists, corrective examples,
review instructions, or findings from previous benchmark analysis. The current
solution is the sole source of translation policy. Exclude observe/decide
workflow navigation and instructions to load files or take game actions.

Reuse live public-text candidate hints only if needed for fidelity to the live
translator, compactly and without duplicating whole dictionary rows. Keep dev
row attribution structural; do not request a written justification. Hidden GT,
expected deltas, correctness hints, and analytical conclusions must never enter
the translator context.

Use a bounded fresh translation context, without inherited conversations,
coach history, notebook maintenance, planning, analysis roles, or unrelated
tools/skills. Inspect the actual request and token usage; a prompt that says
"only translate" is insufficient if the runtime still injects a large general
agent workspace. Do not silently change the evaluated model or solution to
reduce cost; record any benchmark prompt/runtime change in the evidence.

## No duplicate translation; controlled failures and cost

- Keep one benchmark process. On a tool yield or user instruction to continue,
  wait on that process; do not launch another run.
- Before any call, check saved results and in-flight requests. Reuse every
  completed valid translation, including incorrect ones. Never retranslate it
  for review, nicer JSON, a better score, or because a neighboring case failed.
- Persist each completed case promptly. If a batch partly succeeds, retain its
  successful cases and retry only missing/unusable responses. Do not restart
  an entire group and discard finished work.
- Retry only a transport failure or a response that cannot be used as the
  required JSON. Permit at most one automatic retry per failed request, in a
  fresh context. Record every attempt and its failure reason. If it still
  fails, the active Codex agent diagnoses the prompt/runtime before sending
  more requests; no nested or unbounded retry loops.
- Empty responses, malformed JSON, excessive output, or repeated retries first
  require inspection of benchmark prompt complexity, schema, runtime context,
  and transport. They do not establish a translator policy/dictionary defect.
  Simplify the harness before a targeted retry; do not ask the translator to
  diagnose the harness. Do not score service failures as semantic omissions.
- Before scaling out, inspect one unfinished batch's JSON, actual prompt size,
  calls, and input/output/reasoning tokens (when available). Keep those results
  as part of the run; do not rerun that batch after this check.
- Account for all attempts, including rejected, failed and interrupted calls.
  Report known token usage separately from unavailable usage, rather than
  treating missing usage as zero. Distinguish model time from parsing and
  Codex analysis. If token use is abnormal, fix the cause before dispatching
  further batches.
- Preserve existing results when simplifying the prompt. Record the boundary
  and retain old/new prompt cohort provenance. If the user requests one combined
  table, aggregate all retained cases into that table; keep revision details in
  evidence rather than splitting the user-facing results. A clean rerun of already translated cases
  requires an explicit user request.

These requirements override historical driver behavior described below. Before
running, make the driver comply; do not invoke a legacy path that still asks
the translator to do review or analysis. This is implementation work within the
benchmark task, not a new user approval requirement.

## Run

The default minimal transport calls the configured Player model directly with
translation-only context, JSON response format and thinking disabled. It does
not load general Agent tools/workspace. This is a recorded runtime change;
retain provenance for historical Jiuwen Agent/self-review results without
splitting the final table when the user requests aggregation. Structural
checks, deterministic scores, combined per-metric error tables, attempt accounting
and incremental persistence are automatic code, never translator tasks. The
active agent interprets the generated evidence rather than manually checking
every event before the driver can proceed.


Use one benchmark process at a time. For a comparable sample:

```bash
uv run python -m career_sim_runner.translation_benchmark --limit 20
```

For dictionary iteration, use `--mode dev --limit 20`; it adds source line IDs
to the dictionary supplied to Jiuwen and asks which rows each option actually
used. For a faster sample with the same bounded fresh-batch behavior as full
mode, use `--mode dev-batch --limit 64`. Set `--batch-size 1` to check the
single-event live translation context. To probe a later fixed-seed slice,
add `--offset 128 --limit 64`; `dev-full` always uses offset zero. For the
**complete dev pool**, use:

```bash
uv run python -m career_sim_runner.translation_benchmark --mode dev-full
```

`dev-full` forces all installed dev decision nodes and rejects `--unseen-only`.
Preserve the default seed `career-sim-translation-v1` and batch size for direct
comparisons. Wait on the same
process after an execution-tool yield; never launch a duplicate. A plain
`--limit 0` also selects the full pool but lacks attribution.
If a `dev-full` process ends after a WebSocket disconnect, resume the saved
prefix instead of retranslating it:

```bash
uv run python -m career_sim_runner.translation_benchmark --mode dev-full --resume-run <run-id>
```

Normal resume rejects changed seed, dictionary, observe rules, solution skill,
batch size, or case pool. When the user explicitly wants the updated solution
for remaining cases, `--continue-current-source` preserves prior results and
records a new source/prompt cohort; it does not authorize retranslating them. A failed unfinished request may retry within the limit above on a fresh
WebSocket and session; preserve completed cases in SQLite and their exact
prompts in `events.md`. Record prompt/runtime revisions as separate cohorts.

When the user asks for an entirely new event stream, add `--unseen-only`. It
excludes every case ID already recorded in `benchmark.sqlite3` before applying
the seed ordering and limit, and records the exclusion count and set hash in
the generated reports.

Minimal mode uses independent bounded model requests with no conversation
history. Historical Jiuwen mode used fresh sessions and up to four batches in
flight; do not run the legacy analysis/review behavior. The driver follows
the current translation
link or registered role in `solution/skills/observe-decide-review/SKILL.md`,
extracts the dictionary-based step in `stages/observe.md` regardless of its
number, and reads the current `notebooks/event-translator-dictionary.tsv`.
Only the current translation rules belong in the prompt; omit promotion,
cap-notebook maintenance and workflow paragraphs. Do not restate or augment
those rules in this skill or in the wrapper. In dev attribution, a bundled row
may appear under each actually applied metric; count the line once per event.
A mechanism estimate without a matching row has no row ID. Count current
mapping rows from source rather than assuming a historical count.
Explicit metric `=` means no change. Repeated signs (`++`) and equivalent
numbered signs (`+2`) are accepted; uncertainty preserves direction for scoring.

The benchmark never writes to the solution or takes a game action.
Override `--solution` or `--skill-id` only when the user identifies another
candidate.

## Evidence

Inspect the returned JSON and the generated `benchmark.md` under
`.career_sim_runner/translation_benchmark/<run>/`. Report:

- metric score, fully-correct-option score, FP, FN (excluding D), and mean
  signed weighted direction-cosine score (`综合分`) across options (D×0, R×5,
  every other metric ×1); above 65% is acceptable;
- 遗漏次数 (`omission_count`) and 反向次数 (`reversal_count`), counted per
  option×metric cell for **O/S/N/H/R only**: nonzero GT with no usable predicted
  direction (missing/zero/unknown/invalid) is an omission; opposite nonzero signs
  are a reversal. These counts are disjoint, ignore same-direction magnitude
  differences, and exclude D/W. Include per-metric breakdown in diagnosis;
- for dev mode: mapping-line count, option count, compression rate = dictionary
  mapping lines / options, active line count, and mean distinct events per line;
- the fixed seed and Jiuwen session ID;
- each failing event shorttitle and its wrong option numbers;
- the report path;
- prompt/runtime cohort, completed/remaining cases, model call and retry counts,
  and input/output/reasoning token usage, with unavailable usage clearly marked.

`events.md` contains the exact per-case observation, prompt, raw Jiuwen output,
and parsed deterministic comparison. `events.jsonl` retains low-level stream
events. Historical `review-attempts.jsonl` files are read-only evidence of the
old self-review workflow; new runs must not generate translator review calls.
Retain translation-attempt failures and available usage for cost accounting.
`benchmark.sqlite3` is the structural cross-run ledger. The run
directory does not emit transcript, result JSONL, or summary JSON files. Dev
runs emit `dictionary-activation.tsv`: the original two columns plus a third
`activation time` column **only in benchmark evidence**. Keep the live
dictionary two-column. Each line counts once per event even if several options
cite it. Attribution is claimed by the translator and validated against source
line IDs; it describes use, not correctness. Do not edit scores manually or
copy hidden expected deltas into the translator prompt or dictionary.

Compare candidates on the same seed and event selection. The full dev pool
has already been used for iteration, so a gain there is in-sample and does
not establish generalization. Activation count measures use, not correctness.
The historical 198-row atomic version scored 65.33% cosine and FN 1189 versus
the 217-row packed version's 68.51% and FN 856. The current hybrid format
combines reusable atomic rows with compact bundled special scenarios; its
accuracy and activation counts have not been remeasured. Re-evaluate after
any prompt or dictionary change before claiming the old score still holds.
To add or refresh direction scores and FP/FN metric-cell counts on already
ledgered translations without calling Jiuwen or changing their responses, run
the command below. Same-direction magnitude differences up to and including
0.5 levels are tolerated. FN and weighted cosine exclude D; D still
participates in FP and metric/option matching. Add `--run-id <id>` to rescore
only one run.

```bash
uv run python -m career_sim_runner.translation_benchmark --rescore-existing
```
