# Parallel questionnaire benchmark

Run `uv run python -m career_sim_runner.translation_benchmark --limit 20 --pilot`,
inspect the saved first event, then resume with `--resume-run <run-id>`.

Five fresh, independent API conversations answer O/N/S/HW/R per event using the
solution's questionnaire scripts. The scripts own event state and aggregate six
metrics; D is excluded. Only the deterministic scorer sees hidden GT.

Full procedure and evidence contract:
[benchmark skill](../../.codex/skills/simcareer-translation-benchmark/SKILL.md).
Dictionary retrieval, attribution, compression and legacy drivers are in
[archive](archive/README.md); compatibility imports serve old evidence/tests.
