# TTS Pipeline — make targets.
#
# `make verify` is the BINDING QUALITY GATE (DIAGNOSIS H0): the machine-checkable
# definition of "done" that the project previously lacked (the listen-test gate
# kept getting skipped, so regressions shipped). It is deterministic, needs no
# live daemon / Ollama (live_model tests are deselected), and FAILS on:
#   - markdown leaking to the voice (R1)            tests/test_spoken_render.py
#   - the inline-code deletion bug / non-idempotence (R1)
#   - markup in the real shadow.log corpus (R1)     tests/test_shadow_replay.py
#   - path-humanization regressions                 tests/test_text_utils.py
#   - SPEAK/SKIP classification regressions         tests/test_router_corpus.py
#
# Wire this into any "is it done?" check instead of relying on human memory.

PYTHON ?= python3

.PHONY: verify verify-all verify-install sweep-logs restart manifests help

help:
	@echo "make verify      - binding quality gate (deterministic, no live deps)"
	@echo "make verify-all  - everything, including live_model tests (needs Ollama)"
	@echo "make verify-install - install-readiness dry-run (layout, perms, setup modules)"
	@echo "make sweep-logs  - idempotent TTS log retention sweep (R3 hygiene)"
	@echo "make restart     - safe daemon restart (picks up on-disk code; resets health uptime)"
	@echo "make manifests   - sync plugin manifest versions from pyproject.toml"

verify:
	$(PYTHON) tests/fixtures/spoken_corpus/_generate.py
	$(PYTHON) -m pytest -q tests -o asyncio_mode=auto -m 'not live_model'

verify-all:
	$(PYTHON) -m pytest -q tests -o asyncio_mode=auto

verify-install:
	bash tests/test-install-dry-run.sh

sweep-logs:
	sh scripts/sweep_tts_logs.sh

manifests:
	$(PYTHON) scripts/build_manifests.py

restart:
	sh restart_daemon.sh
