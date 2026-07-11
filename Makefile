PYTHON ?= ./.venv/bin/python
PIP ?= $(PYTHON) -m pip
PYTEST ?= $(PYTHON) -m pytest
RUFF ?= $(PYTHON) -m ruff

.PHONY: help install-dev lint test test-web test-visual test-scoring ci web db-up history-dry-run history-migrate history-import

help:
	@printf "%s\n" "B3S development commands"
	@printf "%s\n" ""
	@printf "%s\n" "  make install-dev   Install local dev dependencies"
	@printf "%s\n" "  make lint          Run Ruff gate"
	@printf "%s\n" "  make test          Run full pytest suite"
	@printf "%s\n" "  make test-web      Run web route/UI tests"
	@printf "%s\n" "  make test-visual   Run Visual Signature tests"
	@printf "%s\n" "  make test-scoring  Run scoring/report core tests"
	@printf "%s\n" "  make ci            Run local CI gate"
	@printf "%s\n" "  make web           Start local FastAPI/Jinja app"
	@printf "%s\n" "  make db-up         Start local PostgreSQL"
	@printf "%s\n" "  make history-dry-run  Validate file-backed reports"
	@printf "%s\n" "  make history-migrate  Apply PostgreSQL history schema"
	@printf "%s\n" "  make history-import   Import reports into PostgreSQL"

install-dev:
	$(PIP) install -e ".[dev]"

lint:
	$(RUFF) check .

test:
	$(PYTEST) -q

test-web:
	$(PYTEST) tests/test_web_lab.py tests/test_report_view_model.py -q

test-visual:
	$(PYTEST) tests/test_visual_signature*.py -q

test-scoring:
	$(PYTEST) tests/test_scoring_engine.py tests/test_reports_derivation.py tests/test_reports_renderer.py -q

ci: lint test

web:
	scripts/run_web_dev_macos.sh

db-up:
	docker compose up -d db

history-dry-run:
	$(PYTHON) scripts/import_b3s_reports_postgres.py --dry-run

history-migrate:
	$(PYTHON) scripts/import_b3s_reports_postgres.py --migrate-only

history-import:
	$(PYTHON) scripts/import_b3s_reports_postgres.py
