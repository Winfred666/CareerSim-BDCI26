.PHONY: format lint docstring check test sync setup play score start-jiuwen service-install service-start service-stop service-restart service-status service-logs coach-start coach-auto-postmortem coach-step coach-inspect coach-withdraw translation-benchmark
.DEFAULT_GOAL := setup

-include .instance.mk
UV_RUN ?= uv run $(if $(wildcard parallel_session/.career-instance.json),--no-sync,)
INSTANCE_NAME ?= career_emu
SERVICE_NAME ?= career-emu
# Shared-venv parallel worktrees must not update dependencies or datasets mid-game.
UPDATE_EMULATOR ?= $(if $(wildcard parallel_session/.career-instance.json),0,1)

# ----- Useful for participants of the competition -----

# Dataset split used by `play`; development remains the safe default. Formal
# judging overrides this with `EMULATOR_SPLIT=test`.
EMULATOR_SPLIT ?= dev
PLAY_ARGS ?=
TRANSLATION_BENCHMARK_ARGS ?=

# Install dependencies
sync:
	uv sync

# Setup environment variables and mcp config
setup:
	$(UV_RUN) python -m career_sim_runner setup

# Play a game with current solution
play:
ifeq ($(UPDATE_EMULATOR),1)
	@$(UV_RUN) python -m pip install -U career-emulator-bdci26
	@$(UV_RUN) career-emulator update --source distribution --split $(EMULATOR_SPLIT)
endif
	@$(UV_RUN) python -m career_sim_runner reset-team
	$(UV_RUN) python -m career_sim_runner play --submission solution $(PLAY_ARGS)

# Check last run's score
score:
	$(UV_RUN) python -m career_sim_runner score

# Canonical Jiuwen player handle; SESSION_ID remains a compatibility alias.
JIUWEN_PLAYER_SESSION_ID ?= $(SESSION_ID)

# Render readable markdown from the last run's events log
replay:
	$(UV_RUN) python -m career_sim_runner replay --live

# Start JiuwenSwarm instance in current terminal
start-jiuwen:
	$(UV_RUN) jiuwenswarm-start --name $(INSTANCE_NAME) all

# Install the named JiuwenSwarm instance as a resilient user service.
service-install: setup
	install -Dm644 systemd/$(SERVICE_NAME).service "$(HOME)/.config/systemd/user/$(SERVICE_NAME).service"
	systemctl --user daemon-reload
	-$(UV_RUN) jiuwenswarm-start --stop $(INSTANCE_NAME)
	systemctl --user enable --now $(SERVICE_NAME).service

service-start:
	systemctl --user start $(SERVICE_NAME).service

service-stop:
	systemctl --user stop $(SERVICE_NAME).service

service-restart: setup
	systemctl --user restart $(SERVICE_NAME).service

service-status:
	systemctl --user --no-pager --full status $(SERVICE_NAME).service

service-logs:
	journalctl --user -u $(SERVICE_NAME).service -f

# ----- Coach-only checkpointed controls (not exposed through the MCP server) -----
coach-start:
	$(UV_RUN) python -m career_sim_runner.coach start

coach-auto-postmortem:
	$(UV_RUN) python -m career_sim_runner.coach auto-postmortem $(AUTO_POSTMORTEM_ARGS)

coach-step:
	@test -n "$(JIUWEN_PLAYER_SESSION_ID)" || (echo "Usage: make coach-step JIUWEN_PLAYER_SESSION_ID=<id>" >&2; exit 2)
	$(UV_RUN) python -m career_sim_runner.coach step --jiuwen-player-session-id "$(JIUWEN_PLAYER_SESSION_ID)"

coach-inspect:
	@test -n "$(JIUWEN_PLAYER_SESSION_ID)" || (echo "Usage: make coach-inspect JIUWEN_PLAYER_SESSION_ID=<id>" >&2; exit 2)
	$(UV_RUN) python -m career_sim_runner.coach inspect --jiuwen-player-session-id "$(JIUWEN_PLAYER_SESSION_ID)" $(INSPECT_ARGS)

coach-withdraw:
	@test -n "$(JIUWEN_PLAYER_SESSION_ID)" || (echo "Usage: make coach-withdraw JIUWEN_PLAYER_SESSION_ID=<id>" >&2; exit 2)
	$(UV_RUN) python -m career_sim_runner.coach withdraw --jiuwen-player-session-id "$(JIUWEN_PLAYER_SESSION_ID)" $(WITHDRAW_ARGS)

# ----- Observation Translator benchmark (fresh Jiuwen session, no game actions) -----
translation-benchmark:
	$(UV_RUN) python -m career_sim_runner.translation_benchmark $(TRANSLATION_BENCHMARK_ARGS)

# ----- Please ignore, this is only used by internal developers -----

# Resume an unfinished game
resume:
	$(UV_RUN) python -m career_sim_runner play --submission solution --continue

# Format code with ruff
format:
	$(UV_RUN) ruff check --select I --select E --select F --fix || true
	$(UV_RUN) ruff format || true

# Type check with mypy
lint:
	@$(UV_RUN) --group format mypy -p career_sim_runner

# Check docstring convention (with pydocstyle rules in ruff)
docstring:
	@$(UV_RUN) ruff check --select D career_sim_runner/

# Check format, docstring, typing
check: format docstring lint

# Run unit tests
test:
	@$(UV_RUN) pytest tests/
