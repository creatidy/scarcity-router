# Repository gate. Tooling is repo-managed and reproducible: basedpyright is
# declared as a dev dependency in pyproject.toml, pinned in uv.lock, and
# invoked through the uv-managed environment, never a global executable.
# Plain `pyright` is never substituted for basedpyright.
#
#   make test       -> full unit-test suite (auto-discovers tests/)
#   make typecheck  -> repo-managed basedpyright (default "recommended" gate)
#   make check      -> test + typecheck; must exit 0 before every commit/PR

.PHONY: check typecheck test

check: test typecheck

typecheck:
	uv run basedpyright

test:
	uv run python -m unittest discover -s tests -p "test_*.py"
