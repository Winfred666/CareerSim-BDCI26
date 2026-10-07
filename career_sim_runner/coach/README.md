# Coach controls

`career_sim_runner.coach` is a side-channel controller for a running CareerSim game. It is
not imported by `career-emulator-mcp`, is not added to the Jiuwen MCP config,
and therefore does not appear as an Agent tool.

Start JiuwenSwarm first with `make start-jiuwen`. Then start a coach run:

```bash
uv run python -m career_sim_runner.coach start
```

The JSON result contains two IDs:

- `jiuwen_player_session_id` is the Jiuwen player session ID. Pass this exact
  value to every later `step`, `inspect`, and `withdraw` call. The
  old `session_id` field and `--session-id` option remain compatibility aliases.
- `game_session_id` is the Career Emulator game ID. It remains stable while a
  solution is reloaded.

For example:

```bash
uv run python -m career_sim_runner.coach step --jiuwen-player-session-id <player-session-id>
uv run python -m career_sim_runner.coach inspect --jiuwen-player-session-id <player-session-id>
uv run python -m career_sim_runner.coach withdraw --jiuwen-player-session-id <player-session-id>
```

Interactive single-step control remains the default. For an explicitly requested
whole-run postmortem, use one blocking process:

```bash
uv run python -m career_sim_runner.coach auto-postmortem --solution-keyword mostroles
```

Add `--fresh-events` for a disposable Jiuwen conversation per event after the
initial team setup. The foreground controller freezes one loaded prefix, keeps
the game and notebooks, and deletes retired sessions after review. Rotation
failures are infrastructure exits; this mode does not recover or retry them.

`auto-postmortem` loops the checkpointed event path until a verified 48-month ending,
actual elimination or the Player's own exit. It has no promotion stopping rule,
review horizon, time or event budget. Missed promotions and expensive/incorrect
translation are evidence for the final report. Player wait retries are observed
without external coach correction. Read-only transcript/cost monitoring is allowed;
no concurrent controller, withdrawal, reload, solution edit or recovery runs.
External controller/connection failures are infrastructure exits, never fabricated endings.

`inspect` shows the complete current question, the latest decision, its result,
the full post-decision state, and the state migration. With no range it shows
the current decision only. Use `-i N -j M` for inclusive decision rounds N
through M; `-i N` starts at N and `-j M` ends at M. The JSON response includes
the same unified emulator log path used by the runner.

For the Make target, pass the range through `INSPECT_ARGS`, for example
`make coach-inspect JIUWEN_PLAYER_SESSION_ID=<player-session-id> INSPECT_ARGS="-i 2 -j 4"`.

`step` grants one `take_action` attempt at the MCP execution boundary. A
persistent worker keeps the same WebSocket and player conversation across
steps; only the initial start sends a prompt. Further steps neither add role
instructions nor rebuild the conversation. The adapter blocks the next
`observe` or `take_action` until another permit is granted. Read-only teammate
work may continue after the command returns; consult the live transcript.
Failed actions are not automatically retried. Each completed action
records an incremental entry in a single per-game checkpoint ledger. `withdraw`
rewinds Jiuwen to immediately before that event’s first `observe` tool call, restores the selected
game checkpoint, and returns the withdrawn record together with the current
`jiuwen_player_session_id`, `seed_id`, `withdrawn_question`,
`withdrawn_option`, and `state_transition`. The seed is derived from the game ID and month used by the
emulator's event sampler, so the next step starts from the same event. An
optional `--seed <seed_id>` on `step` can assert that the state is still the
expected one.

Game restoration uses the controller-only `rewind_game_session` interface in
`career_sim_runner.emulator_adapter`. It atomically restores the payload and
logs under the existing `game_session_id` and is not registered as an MCP tool,
so the Player cannot invoke it.

To restore an earlier checkpoint, pass its 1-based round number:

```bash
uv run python -m career_sim_runner.coach withdraw --jiuwen-player-session-id <id> --checkpoint-round 12
```

Later checkpoints are truncated and the game is restored to the state before
that round. Jiuwen is rewound before the game snapshot changes and is reloaded
automatically before the command returns. Both agent and team modes reload the
installed solution and begin a complete loop from `observe`, using the restored
notebooks for event numbering. Withdrawn translations and action status are not
reused. This differs from an ordinary hot reload, which preserves conversation
context and does not rewind an event.

The same checkpoint restores all runtime `notebooks/` files, including redline,
hidden state, promotion records, learned dictionaries, stat caps and session
identity. Capture happens at the first `observe` of an event, after the
previous review and before new observation feedback. Notebook deltas share the
game checkpoint ledger; no translation files or player handoffs are required.
`--defer-reload` also restores notebooks immediately. Ordinary `reload` preserves
the current notebooks while installing updated instructions and scripts. Source
solution notebooks are never overwritten. Checkpoints without notebook snapshots
remain inspectable but cannot be withdrawn safely; new runs capture them automatically.

All Jiuwen Player entrypoints, including coach, play and translation benchmarks,
use `deepseek-flash`, selected centrally by `JIUWEN_PLAYER_MODEL`.

Pass `--solution PATH` to `withdraw` when applying a repair. The same command
rewinds both sessions, installs the selected solution, reloads Jiuwen, and
returns the unchanged player and game IDs. The next call must use the returned
player ID:

```bash
uv run python -m career_sim_runner.coach step --jiuwen-player-session-id <returned-player-session-id>
```

Withdrawal revokes execution, stops the old runtime and waits for history writes
before truncating. Checkpoints bind the first observe snapshot to its tool call
ID; repeated observe calls stay in the same event until an action commits.
The prefix retains completed tool pairs, removes old skill/stage reads and
associated task/prose/compaction summaries, and rebuilds both live and persisted
context. Team mode resets member/task/message state without deleting the host
history or rotating the Player ID. Both context and history persistence must be
explicitly verified.

Every restore phase is journaled. A context, notebook, emulator or reload failure
keeps execution paused; repeat the same `withdraw` to finish recovery. Checkpoints
are truncated only after verification succeeds. `--defer-reload` intentionally
leaves the restored session paused until an explicit reload. Legacy checkpoints
require a unique first-observe boundary recoverable from raw action evidence;
missing or ambiguous evidence is an error, never a user-turn fallback.

Normal reloads also preserve the same Jiuwen
`agent_session_id`, Leader, team and canonical conversation history; the JSON
path is injected for an explicit replacement or a reset team runtime, and
contains only decisions retained before the selected event. If
recovery changes the emulator game ID, every
occurrence of the old ID in this context is rewritten before the next turn and
the execution gate accepts only the restored ID. The conversation receives the
file path for that bootstrap, not an inline history dump; current
state remains authoritative through `observe`.
Legacy ungated conversations require an explicit reload before stepping.
Use `--solution PATH` to
install a different solution directory; without it, reload uses `solution/`.

The registry contains only lightweight routing metadata. The checkpoint ledger
stores one base payload plus compact state deltas and remains compatible with
older JSONL checkpoints; `reload` does not create a separate full game or
Jiuwen-context dump. Explicit
IDs allow several coach run records to be addressed without guessing which
global “last session” is active. The installed Jiuwen skill workspace is still
shared by one running Jiuwen instance; truly simultaneous experiments with
different solutions require separate Jiuwen instances or workspaces.

The MCP adapter automatically saves each complete public observation to an
immutable JSON file and returns `observe_json_path`. Leader forwards that path
without rewriting the observation and does not need `write_file`.
Ordinary `make play` uses the same adapter without a coach execution gate.

## Benchmark

`start --solution-keyword mostroles` writes the solution snapshot and all costs
under `outputs/coach/<MMDDHHMM>-mostroles/` (local timezone; collision suffixes
preserve existing runs). The default `--random-seed career-sim-benchmark-v1`
fixes monthly event sampling independently of each game's unique storage ID.
This is a coach-only sampling adapter, not a formal competition run. Identical
sampling also requires the same dataset and eligibility; different decisions
can unlock different events. Pass `--random-seed ''` for native UUID sampling.

Every attempt updates `benchmark.md` and the shared `outputs/coach/comparison.csv`.
The same directory contains `career.log`, a readable UTF-8 history of questions,
choices, state transitions, full resulting state and ending scores. Withdrawals
mark old attempts without removing them; retries keep separate entries. Pending
or failed attempts without state evidence are labelled explicitly.
The run keeps one cumulative `events.jsonl` across steps and reloads;
`scripts/read_events.py` refreshes `events.md` as events arrive. No per-decision
JSON, `benchmark.json`, or per-run `benchmark.csv` is produced. Private SQLite
accounting and reload context live under `outputs/coach/.coach-runtime/`.
After each committed event, live `notebooks/*keeps.*` state is copied to
`notebooks/<skill>/` in the run directory; `translation-keeps` is excluded.

`benchmark.md` has one combined table (event shorttitle, per-attempt/cumulative
tokens in M, per-attempt/cumulative seconds in s), followed by
`事件〈shorttitle〉：各角色用量` tables with input/output/total tokens in M.
It shows Coach correction count without decision/result dumps. Usage comes from
all Jiuwen Player roles, excluding the external coach. Repeated envelopes and
final summaries do not duplicate individual-call usage; missing usage is unknown.

The global CSV upserts one row per run, with `total_tokens_M`, `elapsed_s`,
`coach_corrections`, `final_score`, and `final_score_withdrawn`. Final score is the
most recent authoritative `ending_score.quantitative_score` at verified completion
or elimination, retained across withdraw, reload, and partial retries. It stays
blank until a verified ending exists. `final_score_withdrawn` marks whether that
ending was withdrawn; a retained score does not mean the current branch has ended
or met the coach target. A later verified ending replaces it, even if lower.

Time includes drive/reload duration and reported post-action activity, not
idle coach inspection or editing. Parallel agents share elapsed wall time;
their wall times are not summed. Retries have separate attempt IDs even when
the decision round is unchanged. Withdrawn costs remain in cumulative totals.
Reloads retain the benchmark directory and save immutable solution revisions in private runtime storage.

`withdraw` automatically records one policy correction, with `--reason TEXT`.
Use `--correction-kind infrastructure` for runtime recovery, or `experiment`
for comparison rewinds; these do not inflate policy correction counts.
`--defer-reload` is reserved for an intentional paused rewind that will not
immediately retry a changed solution.
For a policy correction that does not rewind a decision, record it explicitly:

```bash
uv run python -m career_sim_runner.coach correction --round 4 --reason 'Ignored health floor'
uv run python -m career_sim_runner.coach compare
```

The report shows policy correction count; infrastructure and experimental recovery remain separate in runtime accounting. `compare` refreshes
`outputs/coach/comparison.csv`. Correction counts describe observed coach
interventions, not a proof of optimal decisions or complete policy coverage.

Archive and select a solution without playing:

```bash
uv run python -m career_sim_runner.coach snapshot --solution-keyword mostroles --activate
```

An already-running worker must be reloaded to acquire newly added telemetry;
historical token counts that were never recorded cannot be reconstructed.

The coach-only `fresh_loop` helper freezes the loaded Leader instructions before
the first gated observe, then rotates disposable Jiuwen sessions between accepted
events. The game, workflow notebooks and learned questionnaire JSON remain in the
same workspace. Only the six fixed member configurations are retained in the
Jiuwen roster fixture; messages, tasks and previous event contexts are excluded.

```bash
CAREER_SIM_INSTANCE_NAME=career_coach_isolated uv run python -m career_sim_runner.coach.fresh_loop prepare --player <stable-coach-handle>
# After the warm stream reaches its first observe, with no execution permit:
CAREER_SIM_INSTANCE_NAME=career_coach_isolated uv run python -m career_sim_runner.coach.fresh_loop freeze --player <stable-coach-handle>
CAREER_SIM_INSTANCE_NAME=career_coach_isolated uv run python -m career_sim_runner.coach.fresh_loop rotate --player <stable-coach-handle>
# Run the ordinary single-event step; inspect it before the next rotate.
```

Rotation refuses an outstanding action or incomplete review, verifies that the
game and immutable template are unchanged, seals cost evidence, and deletes the
retired Jiuwen session. It does not perform game withdrawal. Team directories
are first renamed on the same filesystem, so slow directory
collection cannot exhaust the native deletion RPC deadline. Native deletion still
releases checkpoints and database state; the coach then finishes local collection
before starting the next event. Cleanup journals remain in the private runtime
directory, while game skills and notebooks stay outside the retired tree.
Earlier disposable
histories are gone, so an ordinary history-based withdraw cannot be assumed to
work; restoring such a checkpoint requires the official coach game rewind plus
its notebook snapshot and a new disposable context. Validate the first copied
event's tool calls and each member's input usage before treating the context
fixture as a verified rollout environment; host-session fork alone does not copy
the team runtime's member definitions or conversation context.
