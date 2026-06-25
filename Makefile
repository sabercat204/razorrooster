# Razor-Rooster development targets.
# Use `make help` to list available targets.

PYTHON := /opt/homebrew/bin/python3.12
VENV := .venv
VENV_BIN := $(VENV)/bin

.PHONY: help
help:
	@echo "Razor-Rooster development targets:"
	@echo "  make venv         Create the virtual environment."
	@echo "  make install      Install package + dev deps, pinned via constraints.txt."
	@echo "  make upgrade      Re-resolve deps to latest (ignores the pins)."
	@echo "  make lock         Refreeze constraints.txt from the current venv."
	@echo "  make test         Run unit + integration tests."
	@echo "  make test-unit    Run unit tests only (excludes integration + smoke)."
	@echo "  make lint         Run ruff."
	@echo "  make typecheck    Run mypy."
	@echo "  make smoke        Run smoke tests against live services (requires .env)."
	@echo "  make bootstrap    Run scripts/bootstrap.sh to init schema + run every"
	@echo "                    pipeline stage available in the current environment."
	@echo "  make clean        Remove caches and build artifacts."

$(VENV)/bin/activate:
	$(PYTHON) -m venv $(VENV)
	$(VENV_BIN)/python -m pip install --upgrade pip

.PHONY: venv
venv: $(VENV)/bin/activate

.PHONY: install
install: venv
	$(VENV_BIN)/pip install -e ".[dev]" -c constraints.txt

# Intentionally re-resolve every dependency to the latest version allowed by
# pyproject.toml, ignoring the pins. Run this when you mean to take new deps;
# follow with `make test lint typecheck` then `make lock` to capture the result.
.PHONY: upgrade
upgrade: venv
	$(VENV_BIN)/pip install -e ".[dev]" --upgrade

# Refreeze constraints.txt from whatever is currently installed in the venv,
# preserving the documentation header above the pins. Only the verified-green
# venv should be locked — run the gates first.
.PHONY: lock
lock:
	@sed -n '1,/^# ----/p' constraints.txt > constraints.txt.tmp
	@$(VENV_BIN)/python -m pip freeze --exclude-editable >> constraints.txt.tmp
	@mv constraints.txt.tmp constraints.txt
	@echo "constraints.txt refrozen: $$(grep -c '==' constraints.txt) pins."

.PHONY: test
test:
	$(VENV_BIN)/pytest -m "not smoke"

.PHONY: test-unit
test-unit:
	$(VENV_BIN)/pytest -m "not integration and not smoke"

.PHONY: lint
lint:
	$(VENV_BIN)/ruff check src tests
	$(VENV_BIN)/ruff format --check src tests

.PHONY: format
format:
	$(VENV_BIN)/ruff format src tests
	$(VENV_BIN)/ruff check --fix src tests

.PHONY: typecheck
typecheck:
	$(VENV_BIN)/mypy src/razor_rooster/data_ingest src/razor_rooster/polymarket_connector src/razor_rooster/pattern_library

.PHONY: smoke
smoke:
	$(VENV_BIN)/pytest -m smoke

.PHONY: bootstrap
bootstrap:
	bash scripts/bootstrap.sh

.PHONY: clean
clean:
	rm -rf build dist *.egg-info src/*.egg-info
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
