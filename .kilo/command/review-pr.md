---
description: Fresh read-only whole-PR review through the native pr-reviewer subagent
---

Review the exact Forgejo PR: $ARGUMENTS

Run in the primary context; do not require a separate owner-opened session.
Fetch actual PR metadata and its linked issue through `forgejo-mcp`. Require an
open, unmerged canonical BioMedical-IT/scarcity-router PR targeting `develop`.
Read AGENTS.md and its rules, verify canonical remote, fetch current Git objects,
freeze exact HEAD/base/merge-base and inspect status/branches. Prepare an immutable
parent evidence packet; do not edit the PR while the reviewer is active.
Apply `.kilo/rules/35-technical-recovery.md`: repair review infrastructure, not the
implementation, using bounded secret-safe preparation and changed reviewer strategy.
After the exact synthetic access preflight passes and immediately before the whole-PR
Task dispatch, reserve the next ordinal in the existing delivery ledger when present;
otherwise create an excluded standalone ledger with D-069's default ten-review
ceiling and technical recovery budget. Access-preflight Tasks are not whole-PR reviews
and consume no ordinal. A higher exact cap is permitted only by D-069A after independent
canonical owner/repo/issue/PR/source verification; the ledger records counts but cannot
grant or extend that cap. Every prior failed, COMMENT or invalidated whole-PR attempt
remains consumed; standalone review never resets it.

Use ONE designated delivery checkout: safely switch to the fetched PR branch if needed,
creating its local tracking branch at fetched HEAD only if absent. Require clean status
and exact frozen HEAD; refuse local divergence rather than rewriting a branch. Never
stash/reset unrelated changes; if they prevent safe switching, prepare a temporary
exact-revision parent evidence checkout under the recovery rule. Prepare the offline
locked sanitized environment, verify effective `pr-reviewer` Kilo permissions, and
freeze the immutable packet. The reviewer never accesses the checkout. The
`pr-reviewer` profile has no static Read grants. Default to a per-invocation
`KILO_CONFIG_CONTENT` overlay with Read deny and `allow` entries for every exact
relative regular-file path in the flat packet; do not use directories or path/filename
globs. For an ordinary review of issue #210 / PR #211 only, the owner-authorized
exception replaces those file entries with the single exact rule
`.review-evidence-210/pr-reviewer-input/**`; it does not apply to another issue, PR,
reviewer role or delivery. In either case restrict `agent.code` to `* -> deny`,
`task -> allow`. Launch a fresh Kilo CLI parent with `--pure --agent code` and preserve
the current provider/model/variant exactly. That fresh context is a one-call Task
transport only; this primary retains canonical, ledger, packet and audit authority.
For #210, before use validate a 48-file flat packet at the exact frozen refs, reject
credentials, extras and every symlink/hardlink/non-regular/multi-link entry, record
each relative path and SHA-256, and make the packet read-only (directory `0555`, files
`0444`). No writer may run concurrently. Verify that full inventory before and after
the Task; any difference fails the audit. Run a fresh native 48-file synthetic Task
preflight with the same directory-rule shape and prove complete reads and denial before
content for sibling, absolute, traversal and symlink escapes. The preflight consumes no
ordinal and demonstrates Kilo permissions only, not OS-level isolation. This owner
instruction authorizes no #10 or Daybreak dispatch. In every case the reviewer remains
Read-only and the independent post-execution audit/full-PR coverage stay unchanged.
If native task/agent or required access is unavailable, diagnose and try another
authorized independent native reviewer/tool path before a finite blocker; never
self-review, weaken model-bound gates or broaden permissions instead.

Invoke exactly one native `task` from the fresh Kilo parent with
`subagent_type: pr-reviewer`, `background: false`, no `task_id`. Pass only the
parent-prepared immutable packet path/digest, expected HEAD/base/merge-base and fresh
whole-PR instructions. The reviewer has native Read only and no Bash/Git,
Forgejo, search/list, memory/board or prior-finding access. Do not pass implementation
reasoning, previous findings, PR-body history or a desired verdict. The agent definition
owns the JSON result contract and inherited reviewer binding. Never resume a past reviewer.

Require the JSON fields and exact frozen SHAs defined in `.kilo/agents/pr-reviewer.md`.
Audit the actual native task trace before accepting a verdict: only permitted Read
calls, exact flat packet files/ranges/bytes, complete changed-file/source coverage,
and no Bash/Git/Forgejo/other tool calls. Re-fetch MCP/Git currentness in the primary
before reporting. A changed HEAD/base invalidates prior approval; a dirty checkout,
malformed packet/result, mismatch or failed audit cannot support approval. Classify
limitations and attempt bounded changed-condition recovery before COMMENT if possible.
Preserve every ordinal. Present findings in severity order and one verdict.
Do not remediate, commit, push, publish a formal review, merge or modify Forgejo
state. The returned task result is the handoff, not an owner copy or PR comment.
An ordinary APPROVE does not satisfy a security gate. For security-critical Router
work, the implementation agent must never invoke Codex/Daybreak automatically. After
ordinary approval and validation, the primary freezes exact refs and reserves/persists
the next owner-manual ordinal before handing off `gpt-daybreak-blue-latest` through
`READY_FOR_OWNER_SECURITY_REVIEW`. The reservation is not approval and cannot be reused;
once launched, it is consumed for every outcome. Verify the owner result per D-069A.
This Router-only manual path accepts `RUNTIME_UNOBSERVABLE` only with all other specified
invocation/provenance/ref/verdict/context controls and Adrian's explicit acceptance;
never claim `RUNTIME_VERIFIED` or infer backend identity. Keep the PR open/unmerged.
