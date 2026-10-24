---
description: Disabled legacy profile for offline fixture compatibility; never dispatch a reviewer Task
mode: subagent
permission:
  "*": deny
  read: deny
  external_directory: deny
---

This legacy deny-all profile is not a review execution path. Do not dispatch it
through Task or another hidden mechanism. Authorized ordinary reviews use a fresh
user-visible Kilo session started by the orchestrator through native Agent Manager;
use `.kilo/command/review-pr.md` and `.kilo/agents/pr-reviewer-standalone.md` for
that role. The owner does not manually start or relay ordinary review sessions.
It grants no source, model-launch, credential, platform-write or delegation access.
Bounded non-model recovery follows `.kilo/rules/35-technical-recovery.md`.
