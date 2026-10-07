---
name: simcareer-coach
description: Use only when the user explicitly asks for stateful CareerSim main-coach supervision toward the 48-month objective, using interactive event-by-event correction by default or auto-postmortem only when explicitly named. The active Codex agent must do all coaching work itself and must never delegate to a sub-agent. Do not use for ordinary make play failures, continuation-limit errors, transcript/log diagnosis, API or model configuration, or one-off runner troubleshooting.
---

# CareerSim main coach

Use this skill only for an explicitly requested live, stateful CareerSim coaching workflow. Interactive event-by-event supervision is always the default. Use `auto-postmortem` only when the user explicitly requests that mode; never infer it from a general request to automate, save tokens, wait, or finish the game.

## Mode selection

- **Interactive (default):** read [references/coach_protocol.md](references/coach_protocol.md), inspect every committed transition, and repair demonstrated defects during the run.
- **Auto-postmortem (explicit only):** read [references/auto_postmortem.md](references/auto_postmortem.md) and the shared evidence contract. Run one foreground controller to the structured ending or the Player's own exit. Missed promotions, translation/policy defects, high token usage and unsettled attribution are observations only: never stop, correct, withdraw, reload, retry or message the Player for them. Monitor the cumulative transcript/cost evidence read-only and write brief, evidence-linked anomalies to the run's single `postmortem.md` as they occur. No review horizons or coach time/event limits apply in this mode.

The skill never selects or constrains the Codex main-coach model. Use the model and reasoning effort already chosen for the active Codex session.

## Sole-coach invariant

The active Codex agent is the one and only main coach.

- Never call `spawn_agent`, dispatch a sub-coach, delegate a coaching window, or ask any sub-agent to play, inspect, judge, edit, validate, wait, or report.
- Do not create a second “main coach” or any nested supervision layer.
- Execute every coach command, perform every interactive acceptance decision, conduct every postmortem, perform every withdrawal/reload, and make every authorized solution edit in the active agent.
- This prohibition applies to Codex sub-agents. It does not disable the CareerSim submission's own Jiuwen team: that in-game team is the system under test, and the main coach should verify its real teammate/task activity when the installed solution uses `mode: team`.
- Use at most one blocking CareerSim command at a time. Do not run a second step, auto-postmortem, inspect, reload, or recovery command concurrently with an active `coach.py step` or `coach.py auto-postmortem`.

Do not hard-code a particular submission name or manifest mode. Preserve the installed submission's identity and mode unless the user explicitly asks to change them. In this workspace, `solution/manifest.json` field `"team": "winfred_v2"` is immutable during coaching: never rename it to `coach` or modify it as part of a repair, install, withdrawal, or reload. `coach` is only the controller's output namespace.

Read the reference selected above before the first command of a run. The shared protocol defines controller IDs, evidence paths, and rollback/reload invariants.

## Interactive operating loop

1. Resolve the active solution path and current run state. Start a new run with `python -m career_sim_runner.coach start` only when no resumable player/game session was supplied or intentionally preserved. Consume the returned JSON directly; do not save a separate per-command or per-decision JSON file.
2. Run exactly one `python -m career_sim_runner.coach step --jiuwen-player-session-id ... --seed ...` for one event. Treat `--timeout-s 600` as the whole command budget. If the execution tool yields a running session, wait on that same process; never delegate the wait or start a duplicate command.
3. Read the returned `inspection` immediately. Use a separate read-only `inspect` only when the step JSON lacks needed detail, after a failed/no-action step, or to locate an earlier checkpoint. Judge the exact question, selected option, state transition, flags, time, level, and ending score.
4. If the transition is acceptable, retain the returned `jiuwen_player_session_id` and copy its next `seed` byte-for-byte into the following step. A long-chain or fixed event may keep the same month and seed.
5. If the transition is unsafe or clearly wrong, follow the failure workflow below before any retry.
6. Continue event by event until the requested horizon or the verified structured terminal state. There are no delegated windows or sub-coach handoffs.

A short polling timeout or lack of new output is not evidence that the blocking command failed. Wait for the same process up to its controller boundary. Once `coach.py step` returns `timeout`, `no_action`, or another failure without a committed action, inspect once to prove whether state advanced, record the infrastructure outcome, and stop or perform a narrowly justified main-coach recovery. Do not edit the solution for an infrastructure-only failure.

## Failure and repair workflow

For an unacceptable committed transition:

1. Use `python -m career_sim_runner.coach inspect` with `-i/-j` when needed to identify the exact failed round.
2. Make one minimal coherent change only in the active solution path. Preserve unrelated or user-owned worktree changes. Never patch the emulator, database, runner behavior, MCP package, credentials, or `.env` to force a game outcome. Do not run another player step before withdrawal.
3. Save a patch-style diff and one-line diagnosis under `debugs/main_<MMDD-HHMMSS>_<short-title>/`.
4. Run `python -m career_sim_runner.coach withdraw --checkpoint-round <round> --jiuwen-player-session-id ... --solution <active-solution> --reason "<diagnosis>"`. This single command stops the player, rewinds Jiuwen to the decision turn, restores the CareerSim checkpoint under the same game session ID, installs the changed solution, and hot reloads the rewound Jiuwen session. Retain the returned `seed_id`, player/game session IDs, and withdrawn evidence in context; do not create a separate JSON file.
5. Retry with the exact `seed_id` returned by withdrawal, then inspect the result.

The main coach may perform narrowly scoped infrastructure recovery only after proving no game action was committed. Examples include terminating its own stuck controller process, refreshing a disposable Jiuwen team view, or fixing a demonstrated coach-controller defect. Preserve the emulator state and active solution, archive the evidence, and never describe an infrastructure change as a solution-policy fix.

## Solution repair discipline

- `solution/design/` is judge-facing design and review material; Jiuwen Player does not use it during competition. Player-visible instructions live in `solution/skills/`, including its `SKILL.md` and stage documents. Put behavioral fixes and execution boundaries there; editing design documentation alone does not change Player behavior.
- Express every promotion ratio, including initial targets, in the two-pair form `S:O` and `S:N`; never use `O:S:N` in instructions, notebook forecasts, reports or design documentation. The initial target is `S:O=3.00，S:N=2.00`; raw O/S/N state values remain separate from ratio forecasts.
- Diagnose decisions from the strategy and evidence actually visible to the player. First check whether existing instructions, dictionary entries, or evidence rankings led it astray; remove or correct those before adding text. Keep every solution edit brief, precise, and generally reusable. Add only the smallest missing principle or boundary needed to change the demonstrated decision.
- Never write a one-run-specific strategy, event workaround, target, or prior into `solution/`. Keep case-specific findings in the coach's debug note and final analysis.
- Match solution instructions to what the player can access in the official competition. Do not require real dataset distributions or examples unavailable during play; the competition database can change. Player-visible emulator rules and the current game's observations are valid evidence.

## Decision standard

Judge the transition, not the player's prose. Reject immediate elimination, a terminal ending before the requested horizon, severe health/dignity/wealth/risk damage, an action materially contradicted by a safer dominating alternative, or a transition that makes the next promotion or 48-month objective materially unreachable.

Before accepting an option, estimate the next promotion conditions from the installed solution and current structured state. Compare legal repeated combinations for energy menus, use authoritative deltas over predicted labels, and preserve health, dignity, wealth, and hidden-risk buffers while keeping promotion reachable. When evidence has limited directional or magnitude uncertainty, still give the current best definite decision; use `?` only beside the uncertain metric.

Treat structured game state, `alive`/`failed`, time, and `ending_score` as authoritative. Agent text such as `DONE` is never proof of completion. At the requested endpoint, verify the month/completion flag and terminal score directly.

Keep debug artifacts append-only and reviewable. The cumulative event stream and checkpoint ledger retain raw evidence; save only the repair diff and concise diagnosis separately. Never overwrite an earlier failure directory or expose credentials.

## Promotion postmortem

At the endpoint or a requested re-review, explain the score and final level using these five categories. Mark each as demonstrated, plausible, or unsupported; do not force every category to explain the result.

1. **Decide execution:** the player received a usable promotion-ratio forecast and option table but chose against their indicated shortfall, despite a feasible alternative and adequate safety buffers.
2. **Observe execution:** the runtime miscomputed, omitted, or failed to update the forecast required by the policy then installed. An inaccurate forecast produced faithfully by that policy belongs primarily to category 4.
3. **Seed opportunity:** the event mix constrained promotion even under good choices. A low score alone is not evidence. Claims of near-impossibility require an opportunity bound or isolated counterfactual evaluation that accounts for branch-dependent events, fixed actions, caps, eligibility and promotion windows; the same seed need not preserve the same event path.
4. **Observe policy:** the prescribed inference itself is unreliable, such as transferring a previous level's success ratio, restricting cap adjustments to an uninformative historical range, or treating a ratio as sufficient promotion progress.
5. **Other bottlenecks:** translation, action/review synchronization, infrastructure, or other demonstrated losses. Separate coaching recovery from solution improvement; investigate rewind/context/notebook alignment before blaming solution execution.

For each promotion window, tabulate the forecast available beforehand, estimated pre-review O/S/N, result, and supported shortfall. Distinguish public estimates, actual state and any offline oracle evidence; never treat observed caps or a successful ratio as exact promotion thresholds. Trace representative decisions from the actual tool result to the committed action and delta, including feasible alternatives. Report adherence with a defined audited denominator, or explicitly label examples as a partial audit.

Use retained checkpoints to explain the final outcome; withdrawn failures demonstrate defects but contribute no retained loss. Avoid double-counting a policy defect as runtime noncompliance. Identify the best-supported improvement priorities and the evidence missing for the target level, without inventing causal percentages, score gains, or guarantees of reaching it. Preserve the original report and add the new review with evidence references; do not restart or alter the game merely to write it.

## Output contract

Use the format in `outputs/coach/09160956-mostroles-2` as the reference, not as
an output destination for a new run. Follow the exact artifact and unit
contract in [references/coach_protocol.md](references/coach_protocol.md#evidence-layout).
One run uses one `<MMDDHHMM>-<solutionkeyword>` directory across steps,
withdrawals and reloads: `solution/`, `benchmark.md`, `events.jsonl`, `events.md`, `career.log`.
The controller updates these reports and the shared `comparison.csv` itself.
Treat `comparison.csv` itself as the total ranking ledger: update only the active
run's row, never repopulate deleted rows from the private per-run database.
The benchmark's combined table also records each attempt's Jiuwen-reported Agent
context delta and current context in M, labelled `Agent 新增上下文` and
`Agent 累计上下文`, as defined by the evidence-layout contract.
Do not create `benchmark.json`, `benchmark.csv`, `command-*-step.json` or other
per-decision JSON files, and never delete the cumulative events to simplify a
report. Inspect the generated files before handing off; do not restart a game
just to regenerate or reformat an existing report.
