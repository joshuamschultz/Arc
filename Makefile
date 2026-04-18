# Arc Monorepo — Development Makefile
#
# Targets for M1 Acceptance Gates (SPEC-018)
#
# Usage:
#   make help            — show this help
#   make install         — install all packages in editable mode (canonical setup)
#   make architecture-tests  — run architecture regression tests
#   make loc-budgets     — check LOC budgets (G1.5 + G1.6)
#   make coverage        — run coverage report (G1.7)
#   make race-stress     — run 100-run race regression stress test (G1.3)
#   make m1-gates        — run ALL M1 acceptance gate checks

.PHONY: help install architecture-tests loc-budgets coverage race-stress m1-gates \
        test lint typecheck

# ---------------------------------------------------------------------------
# Default Python executable — use uv run for environment consistency
# ---------------------------------------------------------------------------
PYTHON := python
UV_RUN := uv run

# Repository root (directory containing this Makefile)
ROOT := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))

# ---------------------------------------------------------------------------
# help
# ---------------------------------------------------------------------------
help:
	@echo ""
	@echo "Arc Monorepo — M1 Acceptance Gate Targets"
	@echo ""
	@echo "  make install           Install all packages in editable mode (canonical setup)"
	@echo "  make architecture-tests Run architecture regression tests (TX.1)"
	@echo "  make loc-budgets       Check LOC budgets (G1.5 arcagent core, G1.6 arcgateway core)"
	@echo "  make coverage          Run coverage report with thresholds (G1.7)"
	@echo "  make race-stress       Run 100-run race regression stress test (G1.3)"
	@echo "  make m1-gates          Run ALL M1 acceptance gate checks"
	@echo ""
	@echo "  make test              Run full test suite (excluding slow)"
	@echo "  make lint              Run ruff linter across all packages"
	@echo "  make typecheck         Run mypy --strict on arcgateway"
	@echo ""

# ---------------------------------------------------------------------------
# install — canonical editable install (fixes venv-drift issue from SPEC-018 M1)
# ---------------------------------------------------------------------------
install:
	@echo "Installing all Arc packages in editable mode..."
	uv pip install \
		-e packages/arcgateway \
		-e packages/arccli \
		-e packages/arcagent \
		-e packages/arcllm \
		-e packages/arcrun
	@echo "Done. All packages installed."

# ---------------------------------------------------------------------------
# architecture-tests — TX.1 architecture regression guards
# ---------------------------------------------------------------------------
architecture-tests:
	@echo ""
	@echo "Running architecture tests (TX.1)..."
	@echo ""
	$(UV_RUN) pytest tests/architecture/ -v --tb=short
	@echo ""
	@echo "Architecture tests complete."

# ---------------------------------------------------------------------------
# loc-budgets — G1.5 + G1.6 LOC budget checks
# ---------------------------------------------------------------------------
loc-budgets:
	@echo ""
	@echo "Checking LOC budgets (G1.5 + G1.6)..."
	@echo ""
	$(UV_RUN) python scripts/check_loc_budgets.py
	@echo ""

# ---------------------------------------------------------------------------
# coverage — G1.7 coverage thresholds
# ---------------------------------------------------------------------------
coverage:
	@echo ""
	@echo "Running coverage report (G1.7)..."
	@echo "Thresholds: line >= 80%, branch >= 75%"
	@echo ""
	$(UV_RUN) python scripts/coverage_report.py
	@echo ""

# ---------------------------------------------------------------------------
# race-stress — G1.3: 100-run race regression stress test
# ---------------------------------------------------------------------------
race-stress:
	@echo ""
	@echo "Running 100-run race regression stress test (G1.3)..."
	@echo "This may take 5-10 seconds."
	@echo ""
	$(UV_RUN) pytest \
		packages/arcgateway/tests/integration/test_race_regression_stress.py \
		-m slow \
		-v \
		--tb=short
	@echo ""

# ---------------------------------------------------------------------------
# m1-gates — run ALL M1 acceptance gate checks in sequence
# ---------------------------------------------------------------------------
m1-gates: architecture-tests loc-budgets race-stress
	@echo ""
	@echo "========================================================"
	@echo "M1 Acceptance Gate Summary"
	@echo "========================================================"
	@echo "G1.2  Architecture tests:         see above"
	@echo "G1.3  Race regression x100:       see above"
	@echo "G1.5  arcagent core LOC budget:   see above"
	@echo "G1.6  arcgateway core LOC budget: see above"
	@echo ""
	@echo "G1.7 Coverage report runs separately (requires full test suite):"
	@echo "  make coverage"
	@echo ""
	@echo "G1.4 Federal vault-unreachable test:"
	@echo "  uv run pytest packages/arcgateway/tests/integration/test_federal_vault_unreachable.py -v"
	@echo ""
	@echo "All synchronous gates complete."

# ---------------------------------------------------------------------------
# test — standard test run (excludes slow-marked tests)
# ---------------------------------------------------------------------------
test:
	$(UV_RUN) pytest \
		packages/arcgateway/tests/ \
		tests/ \
		-v \
		--tb=short \
		-m "not slow"

# ---------------------------------------------------------------------------
# lint — ruff linter
# ---------------------------------------------------------------------------
lint:
	$(UV_RUN) ruff check packages/arcgateway/src/ tests/ scripts/
	$(UV_RUN) ruff check packages/arcagent/src/arcagent/

# ---------------------------------------------------------------------------
# typecheck — mypy strict on arcgateway
# ---------------------------------------------------------------------------
typecheck:
	$(UV_RUN) mypy packages/arcgateway/src/arcgateway/ --strict
