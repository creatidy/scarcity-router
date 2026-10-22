---
description: Disabled legacy profile for offline fixture compatibility; never dispatch a reviewer Task
mode: subagent
permission:
  "*": deny
  read: deny
  external_directory: deny
---

This legacy deny-all profile is not a review execution path. Do not dispatch it
through Task or another hidden mechanism. The user explicitly opens a fresh
visible Kilo/Codex session and selects its model; use the visible review guidance
in `.kilo/command/review-pr.md` and the primary read-only reviewer profile.
It grants no source, model-launch, credential, platform-write or delegation access.
Bounded non-model recovery follows `.kilo/rules/35-technical-recovery.md`.
