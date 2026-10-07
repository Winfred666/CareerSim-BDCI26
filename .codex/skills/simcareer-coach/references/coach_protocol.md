# Interactive coach protocol

This is the default coaching mode. Use `auto-postmortem` only when the user explicitly names it; that mode is defined in [auto_postmortem.md](auto_postmortem.md).

Run commands from `/home/openclaw-svc/Desktop/CareerSim/CareerSim-BDCI26`.
Use the repository-level evidence root `/home/openclaw-svc/Desktop/CareerSim/debugs/`.

## Command sequence

```bash
uv run python -m career_sim_runner.coach start --solution-keyword <short-keyword>
uv run python -m career_sim_runner.coach step --jiuwen-player-session-id <player-session> --seed <seed> --timeout-s 600
uv run python -m career_sim_runner.coach inspect --jiuwen-player-session-id <player-session>
uv run python -m career_sim_runner.coach withdraw --checkpoint-round <round> --jiuwen-player-session-id <player-session> --solution <active-solution> --reason "<diagnosis>"
```

The active Codex agent is the sole main coach. It runs these commands itself and must not use sub-agents, sub-coaches, delegated windows, or parallel coaching workers. The CareerSim submission's internal Jiuwen teammates are part of the player being evaluated and are not prohibited.

`start` creates a new emulator game and performs exactly its first decision. Do not call it when a resumable Jiuwen player session already exists.

`step` waits for exactly one Jiuwen player `take_action` and returns the live `inspection` in the same JSON. Each invocation controls one short event node, long-chain node, or fixed event action. Use one invocation at a time with a total limit of 600 seconds. If the execution environment yields a running process/session, wait on that same process. Do not start another step or run a concurrent inspect. Once the command returns a timeout or no-action result, do not silently rerun it: inspect once to establish whether state advanced, record the outcome, then choose a narrowly justified recovery.

Use a separate `inspect` only for additional disclosure, after a failed/no-action step, or to locate an earlier round. A successful step's embedded inspection is normally sufficient.

## IDs and state

- `jiuwen_player_session_id`: stable coach control handle used by the next command.
- `game_session_id`: emulator state identity; it remains stable across reloads.
- `agent_session_id`: internal Jiuwen runtime identity; it may rotate.
- `seed`/`seed_id`: deterministic current-event seed. Copy it byte-for-byte; never manufacture or increment it.

A long-chain or fixed event may keep the same seed across several actions. Use the returned player session on every subsequent command. The legacy `session_id` field is only a compatibility alias.

`withdraw` restores the state before a checkpoint. It defaults to the latest checkpoint; use `--checkpoint-round N` for a specific 1-based round. Before changing the emulator snapshot it retires the active worker and calls Jiuwen `session.rewind` at the user turn containing that decision. It then calls the Coach-only CareerSim `rewind_game_session` interface, which atomically restores the emulator payload and logs under the same `game_session_id`; this interface is not exposed as an MCP tool. Both rewind results must retain their original IDs. Several supervised decisions can share one Jiuwen turn, so the retained structured coach records are the finer-grained source used to rebuild the valid pre-decision context. If Jiuwen rewind cannot be verified, withdrawal fails before touching the game snapshot. By default the same command installs the selected solution, hot reloads Jiuwen on the rewound session ID, and starts the next turn with the retained decision ledger. Retry only with its returned seed.

The emulator game ID and Jiuwen session ID are independent. Prefer restoring the emulator snapshot under the same game ID. If a recovery ever produces a different game ID, recursively rewrite the old ID in every retained question, decision, action result, consequence, state snapshot, next question, context file and bootstrap prompt before the new turn starts. Bind the MCP Gate only to the restored game ID; never expose a context containing the old playable ID.

## Evidence layout

A new coach run uses `.career_sim_runner/career_emu/outputs/coach/<MMDDHHMM>-<solutionkeyword>/`.
Choose a short meaningful keyword (for example `mostroles`); do not change the
submission identity to obtain the directory name. Steps and reloads keep this
same directory. The default `--random-seed career-sim-benchmark-v1` fixes monthly
sampling for identical datasets and eligibility; copy returned event seeds exactly.

The run contains:

- `solution/`: immutable starting solution snapshot. Reload revisions and control state live in private runtime storage.
- `events.jsonl`: **one cumulative Player event/call/usage log**, appended across all steps and reloads. Preserve it.
- `events.md`: readable rendering of that total log, updated using `scripts/read_events.py` as events arrive.
- `career.log`: UTF-8 human-readable game-state history, automatically refreshed from the durable attempt ledger. Each committed event includes its question/options, actual choice, before/after state and changes, full resulting state (including ending score when present), and next question. Withdrawn attempts remain explicitly marked; retries are separate attempts. Failed or pending attempts without committed state are labelled rather than inventing a state. Historical runs without recorded state remain unknown; never restart a game to populate this file.
- `benchmark.md`: concise cost report, with one combined table and the role tables below. No decision/result dumps.
- `notebooks/<skill>/`: latest live `*keeps.*` debug mirror after each committed event; excludes `translation-keeps`.

The combined table has exactly:

`| 事件 shorttitle | 标志 token | 累计 token | Agent 新增上下文 | Agent 累计上下文 | 本轮用时 | 累计用时 |`

Use `M` for tokens and Agent context, and `s` for elapsed seconds. Derive Agent
context from Jiuwen's per-call `chat.llm_usage.usage_metadata.input_tokens`: the
last primary-agent call in an attempt is that attempt's cumulative context, and its
difference from the preceding reported Agent context is the new context. In
`mode: agent`, accept single-agent usage with an `agent` label or no role label;
in `mode: team`, use the leader's context. A
negative value exposes reload or compaction instead of being clamped. Missing
Agent usage stays unknown. Follow the combined table with
`### 事件〈shorttitle〉：各角色用量` for each attempt, with role/input/output/total
columns in M. Keep Coach correction count in a single line. Short titles identify
the event; retries may repeat that event with a suffix, never collapse their costs.
Token usage is the **Jiuwen game Player's Agent plus any reporting teammates**;
exclude the external Codex coach. Missing usage stays unknown, never fabricate zero.
Withdrawn attempts and failed attempts keep their costs. Exclude idle editing and
inspection from elapsed time; `withdraw --defer-reload` pauses, reload resumes.

`outputs/coach/comparison.csv` is updated automatically on every benchmark update,
with one row per run, token values in `total_tokens_M`, seconds in `elapsed_s`,
Coach correction count, `final_score`, and `final_score_withdrawn`. `final_score`
is the emulator's `ending_score.quantitative_score` at the most recent verified
ending (completed 48 months or actual elimination). Before any verified ending
it is blank. Withdraw, reload, and partial retries retain that score;
`final_score_withdrawn` marks whether its ending was withdrawn. A later verified
ending replaces it, even if lower. A retained score is historical evidence, not
proof that the current branch has ended or met the coach target. At handoff,
verify both fields in the active CSV row against the recorded ending evidence.

`comparison.csv` is the sole source of truth for the cross-run ranking. Never
store or reconstruct its membership from SQLite. A run update may add or replace
only that run's CSV row. If a user deletes an old row, later updates and
`coach.py compare` must not restore it. `compare` refreshes rows still present in
an existing CSV; it scans run directories only when creating a missing CSV.

Do not emit `benchmark.json`, per-run `benchmark.csv`, or `command-*.json`/per-event
JSON reports. The global comparison CSV and the total event JSONL are intentional.
Per-run runtime accounting uses private SQLite so process restarts do not lose
exact totals; that ledger is not the cross-run ranking and does not control CSV
membership.
CLI responses are read directly; do not redirect each one into its own JSON file.

A normal headless play still writes to `outputs/<submission-name>/<timestamp>/`.
The emulator session log and checkpoint ledger remain runtime evidence. Neither
`events.jsonl` nor its Markdown is an archive of hidden model reasoning.

The `coach` directory label must never be copied into `solution/manifest.json`. Preserve `"team": "winfred_v2"` in this workspace.

For each failed event, create an append-only directory:

```text
/home/openclaw-svc/Desktop/CareerSim/debugs/<UTC timestamp>_<short-Chinese-title>/
```

Refer to the cumulative events and checkpoint ledger for raw withdrawal evidence. Keep the diagnosis and title concise; do not create `withdraw.json`.

Each main-coach solution repair gets its own directory:

```text
/home/openclaw-svc/Desktop/CareerSim/debugs/main_<MMDD-HHMMSS>_<short-Chinese-title>/
```

It contains:

- `solution.diff`: exact patch-style diff for that attempt;
- `note.md`: one-line diagnosis plus validation/retry outcome.

For a controller or Jiuwen infrastructure incident, use the same `main_...` prefix and record the process/session IDs, proof that no action committed, recovery performed, and post-recovery state. Do not label it as a solution repair.

Do not overwrite an existing debug directory. Do not include credentials, `.env` contents, database dumps, or an entire solution copy.

## Exact-state rules

- Structured JSON state and `ending_score` are authoritative; model prose is only explanation.
- A seed mismatch means stop and inspect.
- If an unsafe action committed, withdraw it before any solution edit.
- If a failed/no-action command did not advance state, do not withdraw a nonexistent action.
- Before choosing a non-latest checkpoint, inspect the relevant `-i/-j` range.
- Resolve and record the active solution path. Never assume source `solution/` when an isolated solution is installed.
- Preserve unrelated/user-owned changes. Edit only the active solution files needed for the demonstrated defect.
