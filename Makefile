# Repository gate. Tooling is repo-managed and reproducible: basedpyright is
# declared as a dev dependency in pyproject.toml, pinned in uv.lock, and
# invoked through the uv-managed environment, never a global executable.
# Plain `pyright` is never substituted for basedpyright.
#
# Bare `make` (or `make help`) lists the targets; the descriptions below the
# `##` markers on each target are the single source of that listing.

.DEFAULT_GOAL := help
.PHONY: check help install guardrails typecheck test package-check

help: ## list available targets
	@awk 'BEGIN { FS = ":.*?## " } \
		/^[a-zA-Z_-]+:.*?## / { printf "  make %-16s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

check: test typecheck ## run the full gate (test + typecheck); must exit 0 before every commit/PR

guardrails: ## M08 per-module gate (issue #93): frozen-interface guardrail + parity suite only
	uv run python -m unittest discover -s tests -p "test_interfaces_guardrails.py"

typecheck: ## repo-managed basedpyright (default "recommended" gate)
	uv run basedpyright

test: ## full unit-test suite (auto-discovers tests/)
	uv run python -m unittest discover -s tests -p "test_*.py"

package-check: ## build wheel/sdist, inspect artifacts, isolated temporary tool install and installed-surface smoke
	uv run python tools/package_check.py

install: ## install/upgrade the console commands into the isolated uv tool environment (idempotent: reinstalls from the current checkout state, issue #74)
	uv tool install --force .
