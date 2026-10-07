# Auto-postmortem protocol

Use only when explicitly requested. The active Codex agent remains the sole coach; the default interactive protocol is unchanged.

## Uninterrupted run

Start one foreground controller with the selected seed:

```bash
uv run python -m career_sim_runner.coach auto-postmortem --solution-keyword <short-keyword> --random-seed <seed>
```

When the requested evaluation uses a fresh Jiuwen conversation for each event,
add `--fresh-events`. The same foreground controller freezes the loaded prefix
after enrollment and the first normal event, then rotates and deletes disposable
sessions at completed review boundaries. The game and script notebooks persist;
previous event conversations and repeated handbook initialization do not.

Continue an explicitly supplied existing session using its exact returned seed:

```bash
uv run python -m career_sim_runner.coach auto-postmortem --jiuwen-player-session-id <player-session> --seed <returned-seed>
```

The command grants one action at a time and maintains the same Player/game until a verified structured ending or the Player's own exit. There are no promotion checks that stop execution, review horizons, event limits or coach deadlines. A missed promotion, translation error, unsafe decision, large cost or uncertain cause never permits the coach to stop or interfere. Do not change the solution, questionnaire, model or runtime during the run; do not message, withdraw, reload or recover the Player.

Wait on the same foreground process when the tool yields. Run no other controller command concurrently. Read-only static transcript and cost monitoring is allowed; it must not alter player state. Player-level wait retries remain part of the solution under test.

## Live anomaly notes

Create only `<run>/postmortem.md` for prose analysis. While the foreground controller runs, read its already-written `events.jsonl`, `career.log` and `benchmark.md` directly; do not run `inspect` or send any Player message. For each demonstrated mistake, collaboration failure or excessive cost, add a short paragraph with the attempt/month, event or sequence reference, actual evidence, cause (or explicitly pending attribution), and the smallest general improvement for a future run. Normal events need no prose. Update a pending attribution in place when new evidence resolves it; distinguish raw option gains from caps and monthly settlement. Never feed these findings or offline oracle deltas to the Player, edit its solution/runtime, or stop for a missed promotion. Continue the same process to the structured ending or Player exit, then finalize this same report with verified outcome and costs.

A structured 48-month ending or elimination ends the game. If the Player exits earlier, record the actual nonterminal state and leave the score blank. An external connection/controller failure is recorded honestly as an infrastructure exit, never as completion, and is not recovered automatically. Cleanup of the owned worker/service occurs only after the Player or controller has exited.

## Postmortem

After exit, verify `terminal`, `terminal_outcome`, `ending_score`, the checkpoint state and the active `comparison.csv` row. Use cumulative `events.jsonl`, `events.md`, `career.log`, `benchmark.md` and the immutable starting `solution/` snapshot. Keep anomaly notes together; normal events need no prose review. Explain missed promotions and translation defects after the run, not as stop conditions. Separate predictions, actual deltas and any offline counterfactual evidence.

Report verified score/month/level, all reported Player tokens and context, elapsed seconds and Coach correction count. Describe only demonstrated losses and feasible alternatives; leave uncertain effects conditional. Do not invent a terminal score or promised 48-month result. Keep the installed submission identity/mode and Player model unchanged unless the user separately requests changes.
