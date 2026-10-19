---
description: Fresh read-only whole-PR review through the standalone Kilo primary profile
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
ledger. Synthetic permission preflights are not whole-PR reviews and do not reserve
an ordinal. The default ceiling is ten. For the exact #212/PR #213 delivery only,
the direct D-069/D-077 owner amendment authorizes a finite maximum of 12: #11 is the
final ordinary review and #12 is owner-manual Daybreak only after valid #11 APPROVE.
Verify the direct owner source receipt and exact Issue/PR scope/currentness before
the exceptional reservation; this is not D-069A and cannot transfer. Every other
greater finite ceiling requires D-069A integrated in freshly fetched `develop` before
the primary verifies its exact canonical owner authority. The standalone reviewer is a
separate Kilo CLI primary process/session, never a native Task child.

Use one designated source checkout for primary-side Git inspection only. Require clean
status and exact frozen HEAD/base/merge-base; refuse local divergence rather than
rewriting a branch. Never stash/reset unrelated work. The standalone reviewer must not
receive that checkout, its path, a branch/environment path or Git object store. The
primary prepares the canonical evidence packet, packet-only workspace and pinned
external source bytes; use a separate evidence checkout only when needed to retrieve
an exact public pin. The reviewer workspace contains only the immutable packet; no
source checkout is mounted or passed to the reviewer.

Before any whole-PR ordinal, derive the sorted changed-path list from the frozen
base-to-HEAD Git delta. This path list is not the evidence packet. Use
`tools/reviewer_packet.py synthetic-packet --parent <private-temp-dir> --head <sha>
--base <sha> --merge-base <sha> --changed-path <path>` repeated for each sorted
changed path to create harmless files whose inventory follows
`standalone_packet_path_plan` (index, patch, pinned Kilo sources, owner-source/receipt
files and one numbered source file per changed path).
Then run `tools/reviewer_packet.py prepare --synthetic --preflight` with that fixture,
`--profile` set to the tracked standalone profile, `--profile-source-root` set to the
clean checkout and no `--source-root`. Use the installed Kilo 7.8.8 binary, synthetic
HOME/XDG_CONFIG/state/cache/TMP paths, existing authenticated `XDG_DATA_HOME` and
approved provider/model/variant. The tracked profile frontmatter must declare
`mode: primary`; Kilo debug must report a CLI-runnable mode (`primary` or `all`,
never `subagent`), the exact canonical prompt hash and Read as the sole enabled tool,
with only exact Kilo worktree-relative rules for indexed `packet/<file>` files under
wildcard deny. The prompt supplies only exact indexed relative `packet/<file>` values;
Kilo 7.8.8 resolves them against the session directory before exact permission matching.
Use `KILO_NO_DAEMON=1` so `run` cannot attach
to a server with another configuration. Keep `kilo.json`/`KILO_CONFIG_CONTENT` empty
and `KILO_DISABLE_PROJECT_CONFIG=1` for both static verification and dispatch.

Run one real standalone Kilo CLI preflight against the complete fixture plus harmless
sibling, parent, absolute, traversal and symlink probes. Use exactly the reported
`standalone_run_argv` and environment. Pipe the raw `--format json` event stream to
`tools/reviewer_packet.py capture-session-id` under `set -o pipefail`; never print or
save raw events or returned packet bytes. Audit the actual session identity/mode,
`parentID: null`, requested model dispatch, full allowed reads, deny-before-content
probes, absence of other tools/prior context and unchanged packet inventory. The
preflight is not a whole-PR review and consumes no ordinal. Any fallback, broad rule,
returned unauthorized bytes or incomplete/unverifiable trace aborts before a review.
Do not read `~/.config/kilo/kilo.jsonc`, copy/print provider credentials or infer
OS-level isolation; the Kilo CLI may use its existing authenticated path only for
the configured model request.

Only after the synthetic preflight passes, freshly re-fetch canonical currentness and
build the complete real immutable packet at the exact HEAD/base/merge-base. Include the
canonical issue body, raw owner decisions, full patch, all changed files, exact Git
blob/range bindings and pinned Kilo source excerpts; exclude PR-body contents, prior
review findings, credentials, symlinks/hardlinks and extras. Run normal
`tools/reviewer_packet.py prepare` with `--profile`, `--source-root` and
`--profile-source-root` bound to the clean source and pinned-source checkouts.
Require the real packet path plan to equal the synthetic plan and the final exact
permissions/profile to resolve as primary/Read-only. Only then reserve/persist the
next whole-PR ordinal and frozen refs. Launch one fresh standalone
`kilo --pure run --agent pr-reviewer-standalone --format json` session using its
reported argv/environment; no `--continue`, `--session`, `--fork`, Task or other agent.
Its only context is the generated prompt with packet identity/refs and its sealed
packet workspace, never prior findings or implementation conclusions.

Require the structured JSON result fields and exact frozen HEAD/base/merge-base as
defined in `.kilo/agents/pr-reviewer-standalone.md`, including explicit UX impact.
After the CLI exits, export the exact session in memory for audit; do not print or
persist raw packet contents. Audit session identity/parent, model dispatch, all
native tool calls, every Read path/range/permission decision, complete packet
coverage, reviewer result, packet post-inventory and fresh canonical currentness. A
changed HEAD/base, dirty source checkout, default fallback, malformed result,
incomplete/unauthorized read or trace mismatch cannot support approval. Preserve
every ordinal; classify concrete findings and limitations without treating
intentionally denied direct MCP/Git access as a gap when packet bindings are
complete. Return findings by severity and exactly one verdict.
Do not remediate, commit, push, publish a formal review, merge or modify Forgejo
state. The standalone session export/audit and exact reviewer JSON are the handoff,
not an owner copy or PR comment.
