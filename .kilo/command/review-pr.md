---
description: Fresh read-only whole-PR review through the native pr-reviewer subagent
---

Review the exact Forgejo PR: $ARGUMENTS

Run the primary packet-preparation procedure in this context; do not require a
separate owner-opened session. Fetch the canonical Repo, selected PR, linked Issue
and directly linked owner-decision objects through exact Forgejo MCP getters. Verify
the open/unmerged canonical BioMedical-IT/scarcity-router PR, exact IDs/URLs,
authors/timestamps/body digests, current HEAD/base/merge-base and local Git blob IDs.
Do not edit the PR. Apply `.kilo/rules/35-technical-recovery.md`; repair review
infrastructure, not implementation, with bounded secret-safe preparation.
Preserve the issue-delivery review and technical-recovery counters in the excluded
ledger. Preflight Tasks are not whole-PR reviews and do not reserve an ordinal. Keep
the default-ten ceiling; D-069A cannot authorize an exception on its own unmerged
branch, and exceptions never transfer or renew. Any greater finite ceiling requires
D-069A to be integrated in freshly fetched `develop` before the primary verifies its
exact canonical owner authority.

Use one designated source checkout for primary-side Git inspection only. Require clean
status and exact frozen HEAD/base/merge-base; refuse local divergence rather than
rewriting a branch. Never stash/reset unrelated work. The reviewer must not receive
that checkout, its path, a branch/environment path or Git object store. The primary
prepares the canonical evidence packet, production packet-only workspace and pinned
external source bytes; use a separate evidence checkout only when needed to retrieve
an exact public pin. No source checkout is mounted or passed to the reviewer.

Before any native Task, run `tools/reviewer_packet.py prepare` with the sealed packet,
manifest, exact source checkout, pinned Kilo-source checkout, isolated workspace and
config parents, installed Kilo 7.8.8 binary and sanitized HOME/TMP paths. It verifies
the actual effective `code` and `pr-reviewer` tools/prompts using Kilo debug under the
same `KILO_CONFIG_DIR`, `KILO_CONFIG_CONTENT` and `KILO_DISABLE_PROJECT_CONFIG=1`
environment that will be used for dispatch. Require parent Task-only, reviewer
Read-only on exact packet paths, canonical prompt hash match and deny winners for
all other tools/paths. Then run a real native Task preflight against the complete
production packet plus harmless sibling, absolute, traversal and symlink canaries.
Require full allowed reads and denial before content; audit the actual native trace
and unchanged source packet inventory. Preflight Tasks consume no whole-PR ordinal.
Any missing/ambiguous/overbroad effective rule or failed preflight aborts before
reviewer dispatch. Never self-review, substitute a profile/model or widen access.

After every preflight passes, reserve and persist the next whole-PR ordinal and exact
head/base/merge-base before reviewer dispatch. Start a fresh native Kilo CLI parent
with `--pure --agent code` in the packet-only workspace, using exactly the verified
environment/config returned by `prepare`. The parent effective tool set is Task only;
it makes exactly one foreground native Task call with `subagent_type: pr-reviewer`,
`background: false`, no `task_id`, no model override and no other tool use. The
reviewer receives only packet identity and `packet/index.md` instructions, never a
checkout/environment path, PR-body text, previous findings or parent conclusions.
The child effective tool set is native Read only on exact packet paths.

Require the structured JSON result fields and exact frozen SHAs as defined in
`.kilo/agents/pr-reviewer.md`. After Task, audit the parent/child IDs and tools,
every Read path/range and winning permission rule, complete packet coverage, packet
post-inventory and fresh canonical currentness. A changed HEAD/base, dirty source
checkout, malformed result, incomplete/unauthorized read or audit mismatch cannot
support approval. Preserve every ordinal; classify concrete findings and limitations
without treating intentionally denied direct MCP/Git access as a gap when the packet
bindings are complete. Return findings by severity and exactly one verdict.
Do not remediate, commit, push, publish a formal review, merge or modify Forgejo
state. The returned task result is the handoff, not an owner copy or PR comment.
