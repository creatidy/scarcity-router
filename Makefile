# Gate for the in-task normalized-capacity contract.
#
#   make check      → run the unit tests, then the basedpyright gate.
#   make typecheck  → basedpyright gate only (exit 0 on 0 errors).
#   make test       → unit tests only.
#
# The typechecker gate is `basedpyright` (NOT plain pyright): it runs at its
# full default strict ruleset and is configured to fail on *errors* only —
# warnings are still detected and reported, but do not block the build.
# `python` is not assumed to be on PATH; `python3` is used explicitly.

PYTHON := python3

.PHONY: check typecheck test

check:
	$(PYTHON) -m unittest tests.test_capacity
	basedpyright

typecheck:
	basedpyright

test:
	$(PYTHON) -m unittest tests.test_capacity
