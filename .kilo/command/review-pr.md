---
description: Fresh read-only whole-PR review through the native pr-reviewer subagent
---

Review the exact Forgejo PR: $ARGUMENTS

Run in the primary context; do not require a separate owner-opened session.
Fetch actual PR metadata and its linked issue through `forgejo-mcp`. Require an
open, unmerged canonical BioMedical-IT/scarcity-router PR targeting `develop`.
Read AGENTS.md and its rules, verify canonical remote, fetch current Git objects,
freeze exact HEAD/base/merge-base and inspect status/branches. Do not edit the PR.
Apply `.kilo/rules/35-technical-recovery.md`: repair review infrastructure, not the
implementation, using bounded secret-safe preparation and changed reviewer strategy.
Before dispatch, reserve ordinals in the existing delivery ledger when present;
otherwise create an excluded standalone ledger with the same ten-review ceiling
and technical recovery budget. Standalone review cannot reset a delivery counter.

Use ONE designated delivery checkout: safely switch to the fetched PR branch if needed, creating
its local tracking branch at fetched HEAD only if absent. Require clean status and
exact frozen HEAD; refuse local divergence rather than rewriting a branch.
Never stash/reset unrelated changes;
if they prevent safe switching, prepare a temporary exact-revision worktree/checkout
under the recovery rule without disturbing owner work. Prepare the offline locked,
sanitized environment; review frozen Git objects/current clean branch read-only. The
parent must not edit/switch while the reviewer is active. Locate `pr-reviewer`.
If native task/agent or required access is unavailable, diagnose and try another
authorized independent native reviewer/tool path before a finite blocker; never
self-review, weaken model-bound gates or broaden permissions instead.

Invoke `task` with `subagent_type: pr-reviewer`, `background: false`, no `task_id`.
Pass only the PR number/URL, expected HEAD/base and fresh whole-PR review
instructions including exact checkout, prepared environment/source paths and provenance.
Prepare and audit the acceptance-only object manifest and result capture under
`.kilo/rules/35-technical-recovery.md#reviewer-context-and-result-capture`.
Do not pass implementation
reasoning, previous findings or desired verdict. The agent definition owns the
JSON result contract and reviewer binding. Never resume a past reviewer.

Require JSON fields reviewed_head, reviewed_base, verdict, findings, limitations,
checks_run as defined in `.kilo/agents/pr-reviewer.md`. Validate their types and
exact frozen SHAs. Recheck local HEAD/clean status and MCP before reporting. A
changed HEAD/base, dirty checkout, malformed result or mismatch cannot support
approval; classify the limitation and attempt bounded changed-condition recovery
before the final COMMENT if possible. Preserve every ordinal. Present findings in severity
order and exactly one verdict.
Do not remediate, commit, push, publish a formal review, merge or modify Forgejo
state. The returned task result is the handoff, not an owner copy or PR comment.
