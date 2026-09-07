# Validation Rules

## Required Checks

- Run the repository's final validation before finalizing changes: `make
  check` (full test suite plus typecheck).
- Python changes require `basedpyright` invoked through the uv-managed
  environment (`uv run basedpyright`); plain `pyright` is not an acceptable
  substitute.
- Provider/capacity work requires redacted fixture and contract tests;
  selection work requires deterministic policy tests and explanation
  assertions.

## Test Quality

- Start with focused tests while iterating, then finish with the full
  repo-level validation.
- Keep tests deterministic and behavior-focused. Mock external boundaries
  such as network, credentials, subprocesses and clocks; never mock the
  behavior being tested.
- Use fixtures shaped like the real producer output, structurally
  representative and redacted; do not prove behavior only with invented
  fields, and never record real credentials or personal quota values.

## Runtime Safety

- Keep imports safe without requiring runtime secrets or provider access at
  import time.
- Preserve safe behavior when provider credentials or access are
  unavailable: operational provider states remain normalized status data and
  tests must not depend on live quota.
