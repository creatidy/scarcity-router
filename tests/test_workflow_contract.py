"""Offline consistency guards for command text, not proof of runtime execution."""

import re
import fnmatch
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import cast

import yaml

ROOT = Path(__file__).resolve().parents[1]


def text(path: str) -> str:
    return " ".join((ROOT / path).read_text().split())


def eligibility_transitions() -> dict[str, tuple[str, str, str]]:
    """Decode the command's classification table, not a product/runtime controller."""
    section = (ROOT / ".kilo/command/loop.md").read_text().split(
        "## Post-Selection Eligibility Revalidation", 1
    )[1].split("## IMPLEMENT", 1)[0]
    rows: dict[str, tuple[str, str, str]] = {}
    for line in section.splitlines():
        if not line.startswith("| "):
            continue
        cells = tuple(cell.strip() for cell in line.strip("|").split("|"))
        if cells[0] in ("Evidence Class", "---"):
            continue
        if len(cells) != 4 or cells[0] in rows:
            raise AssertionError("classification rows must be unique four-column rules")
        rows[cells[0]] = (cells[1], cells[2], cells[3])
    return rows


def dependency_assessments() -> dict[str, tuple[str, str, str]]:
    """Decode normative dependency actions, not synthetic orchestration behavior."""
    section = (ROOT / ".kilo/command/loop.md").read_text().split(
        "## Dependency Evidence Before Inactivity", 1
    )[1].split("## Post-Selection Eligibility Revalidation", 1)[0]
    rows: dict[str, tuple[str, str, str]] = {}
    for line in section.splitlines():
        if not line.startswith("| "):
            continue
        cells = tuple(cell.strip() for cell in line.strip("|").split("|"))
        if cells[0] in ("Evidence Case", "---"):
            continue
        if len(cells) != 4 or cells[0] in rows:
            raise AssertionError("dependency rows must be unique four-column rules")
        rows[cells[0]] = (cells[1], cells[2], cells[3])
    return rows


class WorkflowContractTests(unittest.TestCase):
    def test_acceptance_only_context_preserves_exact_provenance_and_excludes_history(self) -> None:
        contract = text(".kilo/rules/35-technical-recovery.md").split(
            "## Reviewer Context and Result Capture", 1)[1].split("## Terminal Contracts", 1)[0]
        for requirement in (
            "Start with neutral canonical issue/PR links and exact frozen base/HEAD/merge-base",
            "complete canonical issue body", "full original text/context of applicable owner decisions",
            "canonical URL, object/comment ID, author, timestamp, retrieval path and content digest",
            "local sources also carry exact Git revision/path", "No blanket comment history",
            "previous reviews/findings, implementer summaries", "Missing decision provenance is an evidence gap",
            "post-Task tool routes/arguments against the manifest", "self-report alone does not close it",
            "supported no-tools execution needs a complete source packet",
            "`forgejo-mcp_get_repo` for the canonical owner/repository",
        ):
            self.assertIn(requirement, contract)
        # A synthetic parent packet checks exact text and route auditing. This is
        # not proof that Kilo dispatch/runtime permission enforcement performed it.
        owner_text = "Original owner decision.\nFull constraints, not an implementer summary."
        packet = {
            "first_metadata": {"issue": "https://forgejo.example/issues/7", "pr": "https://forgejo.example/pulls/8",
                               "base": "a" * 40, "head": "b" * 40, "merge_base": "a" * 40},
            "owner_decision": {"id": 9, "author": "synthetic-owner", "timestamp": "2026-10-10T00:00:00Z",
                               "body": owner_text, "sha256": hashlib.sha256(owner_text.encode()).hexdigest()},
        }
        self.assertEqual(packet["owner_decision"]["body"], owner_text)
        self.assertEqual(packet["owner_decision"]["sha256"], hashlib.sha256(owner_text.encode()).hexdigest())
        self.assertNotIn("verdict", packet["first_metadata"])
        manifest = (("forgejo-mcp_get_repo", ("synthetic-owner", "synthetic-repo")),
                    ("forgejo-mcp_get_issue_by_index", 7), ("forgejo-mcp_get_pull_request_by_index", 8),
                    ("forgejo-mcp_get_issue_comment", 9))
        self.assertIn(("forgejo-mcp_get_repo", ("synthetic-owner", "synthetic-repo")), manifest)
        self.assertNotIn(("forgejo-mcp_get_repo", ("synthetic-owner", "other-repo")), manifest)
        self.assertIn(("forgejo-mcp_get_issue_comment", 9), manifest)
        self.assertNotIn(("forgejo-mcp_get_issue_comment", 10), manifest)
        self.assertNotIn(("forgejo-mcp_list_issue_comments", 7), manifest)
        for path in ("review-pr", "finish-pr", "loop"):
            self.assertIn("reviewer-context-and-result-capture", text(f".kilo/command/{path}.md"))

    def test_reviewer_exact_getters_and_single_command_shell_fixture_policy(self) -> None:
        raw = (ROOT / ".kilo/agents/pr-reviewer.md").read_text().split("---", 2)[1]
        frontmatter = cast(dict[str, object], yaml.safe_load(raw))
        permissions = cast(dict[str, object], frontmatter["permission"])
        getters = {name for name in permissions if name.startswith("forgejo-mcp_")}
        self.assertEqual(getters, {"forgejo-mcp_get_repo", "forgejo-mcp_get_pull_request_by_index", "forgejo-mcp_get_issue_by_index",
                                  "forgejo-mcp_get_issue_comment"})
        for key in getters:
            self.assertEqual(permissions[key], "allow")
        shell = cast(dict[str, str], permissions["bash"])
        self.assertEqual(shell["*"], "deny")
        for pattern in ("*>*", "*<*", "*|*", "*;*", "*&*", "*$(*", "*`*", "*\n*"):
            self.assertEqual(shell[pattern], "deny")
        # Offline glob-fixture check, explicitly not actual Kilo enforcement.
        def allowed(command: str) -> bool:
            decision = "deny"
            for pattern, value in shell.items():
                if fnmatch.fnmatchcase(command, pattern):
                    decision = value
            return decision == "allow"
        for command in ("git status --short", "git rev-parse HEAD", "git rev-parse --abbrev-ref HEAD", "git diff BASE HEAD",
                        "PYTHONPATH=. UV_OFFLINE=1 UV_NO_SYNC=1 uv run --no-sync python -m unittest "
                        + "tests.test_workflow_contract.WorkflowContractTests.test_private_repository_review_uses_exact_mcp_and_local_evidence -v"):
            self.assertTrue(allowed(command))
        for command in ("git status --short && git show HEAD", "git diff BASE HEAD; python -c 'pass'",
                        "git show HEAD | python", "git show HEAD > output", "git show $(python)",
                        "git status --short\ngit show HEAD", "python -c 'pass'",
                        "git status --short --branch", "git status --porcelain=v1", "git branch --show-current",
                        "git rev-parse HEAD < input", "git show `python`", "git rev-parse HEAD & git show HEAD",
                        "git remote -v", "git ls-remote https://forgejo.creatidy.com/BioMedical-IT/scarcity-router",
                        "git config user.name", "git credential fill",
                        "PYTHONPATH=. UV_OFFLINE=1 UV_NO_SYNC=1 uv run --no-sync python -m unittest "
                        + "tests.test_workflow_contract.WorkflowContractTests.test_persisted_prospective_capacity_counts_all_exact_object_gates -v"):
            self.assertFalse(allowed(command))
        guidance = text(".kilo/agents/pr-reviewer.md")
        self.assertIn("one allowlisted command", guidance)
        self.assertIn("use exactly `git status --short`", guidance)
        self.assertIn("use `git rev-parse --abbrev-ref HEAD`", guidance)
        self.assertIn("unchanged permission boundary", guidance)

    def test_persisted_prospective_capacity_counts_all_exact_object_gates(self) -> None:
        section = (ROOT / ".kilo/rules/35-technical-recovery.md").read_text().split(
            "## Prospective Review Capacity", 1)[1].split("No blind retries", 1)[0]
        rows: dict[str, tuple[tuple[str, ...], int]] = {}
        for line in section.splitlines():
            if not line.startswith("| "):
                continue
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if cells[0] in ("Round State", "---"):
                continue
            self.assertEqual(len(cells), 3)
            self.assertNotIn(cells[0], rows)
            rows[cells[0]] = (() if cells[1] == "none" else tuple(cells[1].split(",")), int(cells[2]))
        self.assertEqual(rows, {
            "security_patch_or_both_missing": (("ordinary", "restricted"), 2),
            "ordinary_patch_or_missing": (("ordinary",), 1),
            "stable_ordinary_approved": (("restricted",), 1),
            "stable_restricted_approved": (("ordinary",), 1),
            "stable_all_approved": ((), 0),
        })
        # Read a persisted synthetic ledger for each case, applying the published
        # math/table only. No live reservation, authority or runtime enforcement.
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / ".task_progress.md"

            def assess(used: object, ceiling: object = 10, *, canonical_exception: object = False,
                       patch_objects: bool = True, security: bool = True,
                       ordinary_current: bool = False, restricted_current: bool = False,
                       ordinary_stale: bool = False, restricted_stale: bool = False, base_stale: bool = False,
                       ordinary_audited: bool = True, ordinary_empty: bool = True,
                       restricted_audited: bool = True, restricted_empty: bool = True,
                       recovery: str = "intact") -> str:
                reservations: list[dict[str, object]] = ([{"ordinal": ordinal, "outcome": ("COMMENT", "FAILED", "POLICY_VIOLATION")[ordinal % 3]}
                                 for ordinal in range(1, used + 1)] if type(used) is int and used >= 0 else [])
                if len(reservations) >= 10:
                    reservations[9]["outcome"] = "REQUEST_CHANGES"
                if len(reservations) >= 12 and isinstance(canonical_exception, dict):
                    reservations[11]["outcome"] = "UNSAFE_TEXT"
                if reservations and ordinary_current and (not isinstance(canonical_exception, dict) or len(reservations) > 12):
                    reservations[-1]["outcome"] = "APPROVE"
                if reservations and recovery in ("gap", "duplicate", "bool_ordinal", "float_ordinal"):
                    reservations[-1]["ordinal"] = {
                        "gap": 100, "duplicate": 1, "bool_ordinal": True, "float_ordinal": 9.0,
                    }[recovery]
                _ = ledger.write_text(json.dumps({"used": used, "reservations": reservations,
                                                  "claimed_ceiling": ceiling,
                                                  "head": "a" * 40, "base": "b" * 40,
                                                  "ordinary_head": ("c" if ordinary_stale else "a") * 40
                                                  if ordinary_current else None,
                                                  "ordinary_base": ("c" if base_stale else "b") * 40
                                                  if ordinary_current else None,
                                                  "ordinary_verdict": "APPROVE" if ordinary_current else "REQUEST_CHANGES",
                                                  "ordinary_findings": [] if ordinary_empty else ["synthetic finding"],
                                                  "ordinary_audited": ordinary_audited,
                                                  "restricted_head": ("c" if restricted_stale else "a") * 40
                                                  if restricted_current else None,
                                                  "restricted_base": ("c" if base_stale else "b") * 40
                                                  if restricted_current else None,
                                                  "restricted_verdict": "APPROVE" if restricted_current else "UNSAFE_TEXT",
                                                  "restricted_findings": [] if restricted_empty else ["synthetic finding"],
                                                  "restricted_audited": restricted_audited}))
                if recovery == "missing":
                    ledger.unlink()
                try:
                    persisted = cast(dict[str, object], json.loads(ledger.read_text()))
                except FileNotFoundError:
                    return "BLOCKED"
                count = persisted["used"]
                # Authority here is a synthetic independently attested input,
                # never obtained from this ledger or a live platform operation.
                if canonical_exception is True:
                    bound = 12  # Preserved historical 16179/16250 fixture.
                elif canonical_exception is False:
                    bound = 10
                elif isinstance(canonical_exception, dict):
                    expected = {"issue": 205, "pr": 206, "comment": 16611, "ceiling": 15}
                    attested = cast(dict[str, object], canonical_exception)
                    if (set(attested) != set(expected)
                            or any(type(attested[key]) is not int
                                   or attested[key] != value for key, value in expected.items())):
                        return "BLOCKED"
                    bound = 15
                else:
                    return "BLOCKED"
                if (recovery == "ambiguous" or type(count) is not int or count < 0
                        or type(persisted["claimed_ceiling"]) is not int or persisted["claimed_ceiling"] < 1
                        or canonical_exception is not False and persisted["claimed_ceiling"] != bound):
                    return "BLOCKED"
                records = cast(list[dict[str, object]], persisted["reservations"])
                if (any(type(record["ordinal"]) is not int for record in records)
                        or [record["ordinal"] for record in records] != list(range(1, count + 1)) or count > bound):
                    return "BLOCKED"
                ordinary = (persisted["ordinary_head"] == persisted["head"]
                            and persisted["ordinary_base"] == persisted["base"] and not patch_objects
                            and persisted["ordinary_verdict"] == "APPROVE"
                            and persisted["ordinary_audited"] is True and persisted["ordinary_findings"] == [])
                restricted = (persisted["restricted_head"] == persisted["head"]
                              and persisted["restricted_base"] == persisted["base"] and not patch_objects
                              and persisted["restricted_verdict"] == "APPROVE"
                              and persisted["restricted_audited"] is True and persisted["restricted_findings"] == [])
                state = ("stable_all_approved" if ordinary and (restricted or not security)
                         else "ordinary_patch_or_missing" if not security
                         else "stable_ordinary_approved" if ordinary
                         else "stable_restricted_approved" if restricted
                         else "security_patch_or_both_missing")
                roles, needed = rows[state]
                self.assertEqual(len(roles), needed)
                if bound - count < needed or needed and count + 1 > bound:
                    return "STOP_REVISE"
                # Prospective roles are dependency-ordered, not fixed ordinal
                # allocations. Restricted dispatch still awaits ordinary approval.
                self.assertTrue(not security or ordinary or roles[0:1] == ("ordinary",))
                persisted["prospective_roles"] = list(roles)
                persisted["prospective_ordinals"] = list(range(count + 1, count + needed + 1))
                _ = ledger.write_text(json.dumps(persisted))
                return "PASS"

            self.assertEqual(assess(9), "STOP_REVISE")
            self.assertEqual(assess(9, 12, canonical_exception=True), "PASS")
            self.assertEqual(assess(9, 12), "STOP_REVISE")  # Ledger is not authority.
            self.assertEqual(assess(10, 12, canonical_exception=True), "PASS")
            regression = cast(dict[str, object], json.loads(ledger.read_text()))
            self.assertEqual(cast(list[dict[str, object]], regression["reservations"])[9]["outcome"], "REQUEST_CHANGES")
            self.assertEqual(regression["prospective_roles"], ["ordinary", "restricted"])
            self.assertEqual(regression["prospective_ordinals"], [11, 12])
            self.assertEqual(assess(11, 12, canonical_exception=True), "STOP_REVISE")
            self.assertEqual(assess(11, 12, canonical_exception=True, patch_objects=False,
                                    ordinary_current=True), "PASS")
            regression = cast(dict[str, object], json.loads(ledger.read_text()))
            self.assertEqual(regression["prospective_roles"], ["restricted"])
            self.assertEqual(regression["prospective_ordinals"], [12])
            self.assertEqual(cast(list[dict[str, object]], regression["reservations"])[10]["outcome"], "APPROVE")
            for audited, empty in ((False, True), (True, False)):
                self.assertEqual(assess(11, 12, canonical_exception=True, patch_objects=False,
                                        ordinary_current=True, ordinary_audited=audited,
                                        ordinary_empty=empty), "STOP_REVISE")
            self.assertEqual(assess(12, 12, canonical_exception=True, patch_objects=False,
                                    ordinary_current=True), "STOP_REVISE")
            self.assertEqual(assess(12, 12, canonical_exception=True, patch_objects=False,
                                    ordinary_current=True, restricted_current=True), "PASS")
            self.assertEqual(assess(9, patch_objects=False, restricted_current=True), "PASS")
            self.assertEqual(assess(11, 12, canonical_exception=True, patch_objects=False,
                                    ordinary_current=True, ordinary_stale=True), "STOP_REVISE")
            self.assertEqual(assess(10, patch_objects=False, ordinary_current=True, restricted_current=True), "PASS")
            self.assertEqual(assess(10, patch_objects=False, ordinary_current=True,
                                    restricted_current=True, base_stale=True), "STOP_REVISE")
            self.assertEqual(assess(9, security=False), "PASS")
            self.assertEqual(assess(10, security=False), "STOP_REVISE")
            self.assertEqual(assess(11), "BLOCKED")
            for invalid in (None, True, 9.0, -1):
                self.assertEqual(assess(invalid), "BLOCKED")
            for invalid in (True, 12.0, 0, -1):
                self.assertEqual(assess(9, invalid, canonical_exception=True), "BLOCKED")
            for recovery in ("ambiguous", "missing", "gap", "duplicate", "bool_ordinal", "float_ordinal"):
                self.assertEqual(assess(9, recovery=recovery), "BLOCKED")

            # Comment16611 changes future capacity, not historical results. A
            # stale base and a source patch both require two newly current gates.
            authority = {"issue": 205, "pr": 206, "comment": 16611, "ceiling": 15}
            for patch in (False, True):
                self.assertEqual(assess(12, 15, canonical_exception=authority, patch_objects=patch,
                                        ordinary_current=True, restricted_current=True, base_stale=True), "PASS")
                reconciled = cast(dict[str, object], json.loads(ledger.read_text()))
                self.assertEqual(reconciled["prospective_roles"], ["ordinary", "restricted"])
                self.assertEqual(reconciled["prospective_ordinals"], [13, 14])
                history = cast(list[dict[str, object]], reconciled["reservations"])
                self.assertEqual(len(history), 12)
                self.assertEqual(history[9]["outcome"], "REQUEST_CHANGES")
                self.assertEqual(history[11]["outcome"], "UNSAFE_TEXT")
            self.assertEqual(assess(12, 15), "BLOCKED")  # A claimed15 is not authority.
            self.assertEqual(assess(12, 15, canonical_exception=True), "BLOCKED")  # Old authority cannot extend.
            self.assertEqual(assess(13, 15, canonical_exception=authority), "PASS")
            contingency = cast(dict[str, object], json.loads(ledger.read_text()))
            self.assertEqual(contingency["prospective_roles"], ["ordinary", "restricted"])
            self.assertEqual(contingency["prospective_ordinals"], [14, 15])
            self.assertEqual(cast(list[dict[str, object]], contingency["reservations"])[:12], history)
            self.assertEqual(assess(13, 15, canonical_exception=authority, patch_objects=False,
                                    ordinary_current=True), "PASS")
            next_security = cast(dict[str, object], json.loads(ledger.read_text()))
            self.assertEqual(next_security["prospective_roles"], ["restricted"])
            self.assertEqual(next_security["prospective_ordinals"], [14])
            self.assertEqual(cast(list[dict[str, object]], next_security["reservations"])[:12], history)
            self.assertEqual(assess(14, 15, canonical_exception=authority), "STOP_REVISE")
            self.assertEqual(assess(14, 15, canonical_exception=authority, patch_objects=False,
                                    ordinary_current=True), "PASS")
            final_security = cast(dict[str, object], json.loads(ledger.read_text()))
            self.assertEqual(final_security["prospective_roles"], ["restricted"])
            self.assertEqual(final_security["prospective_ordinals"], [15])
            self.assertEqual(cast(list[dict[str, object]], final_security["reservations"])[:12], history)
            for stale, stale_base, audited, empty in ((True, False, True, True), (False, True, True, True),
                                                      (False, False, False, True), (False, False, True, False)):
                self.assertEqual(assess(14, 15, canonical_exception=authority, patch_objects=False,
                                        ordinary_current=True, ordinary_stale=stale, base_stale=stale_base,
                                        ordinary_audited=audited, ordinary_empty=empty), "STOP_REVISE")
            self.assertEqual(assess(15, 15, canonical_exception=authority, patch_objects=False,
                                    ordinary_current=True, restricted_current=True), "PASS")
            closed = cast(dict[str, object], json.loads(ledger.read_text()))
            self.assertEqual(closed["prospective_roles"], [])
            self.assertEqual(closed["prospective_ordinals"], [])
            self.assertEqual(cast(list[dict[str, object]], closed["reservations"])[:12], history)
            self.assertEqual(assess(15, 15, canonical_exception=authority, patch_objects=False,
                                    ordinary_current=True), "STOP_REVISE")
            for stale, stale_base, audited, empty in ((True, False, True, True), (False, True, True, True),
                                                      (False, False, False, True), (False, False, True, False)):
                self.assertEqual(assess(15, 15, canonical_exception=authority, patch_objects=False,
                                        ordinary_current=True, restricted_current=True,
                                        restricted_stale=stale, base_stale=stale_base,
                                        restricted_audited=audited, restricted_empty=empty), "STOP_REVISE")
            self.assertEqual(assess(16, 15, canonical_exception=authority), "BLOCKED")
            self.assertEqual(assess(15, 16, canonical_exception=authority, patch_objects=False,
                                    ordinary_current=True, restricted_current=True), "BLOCKED")
            for field, invalid in (("issue", 174), ("pr", 204), ("comment", 16250),
                                   ("ceiling", 16), ("ceiling", True), ("ceiling", 15.0)):
                self.assertEqual(assess(12, 15, canonical_exception={**authority, field: invalid}), "BLOCKED")
            for malformed in (None, 1, {}, {"ceiling": 15}, {**authority, "unattested": True}):
                self.assertEqual(assess(12, 15, canonical_exception=malformed), "BLOCKED")
            for invalid in (None, True, 12.0, -1):
                self.assertEqual(assess(invalid, 15, canonical_exception=authority), "BLOCKED")
            for recovery in ("ambiguous", "missing", "gap", "duplicate", "bool_ordinal", "float_ordinal"):
                self.assertEqual(assess(12, 15, canonical_exception=authority, recovery=recovery), "BLOCKED")
        contract = " ".join(section.split())
        for requirement in ("persisted, Git-locally excluded `.task_progress.md`", "BEFORE mutating or reserving",
                            "Missing/ambiguous ledger recovery is BLOCKED, never zero", "failed, COMMENT",
                            "tool-policy-violating reviews", "Ledger claims alone never authorize an exception",
                            "Capacity alone is NOT approval/currentness", "chronological counters, not immutable roles",
                            "Superseding owner correction comment16250", "all consumed ordinals1-10 and ceiling12",
                            "If11 requests changes", "If12 does not approve, STOP_REVISE, never13",
                            "ordinary-before-restricted order", "contiguous unique ordinals",
                            "Historical #205/PR206 authority", "decisions16179/16250 as history",
                            "comment16611", "workspace2026-10-11T19:48:05Z", "ceiling15",
                            "preserving ALL consumed ordinals1-12", "next13 is fresh ordinary review",
                            "next14 is direct-Codex `gpt-daybreak-blue-latest` restricted review",
                            "Role15 is bounded contingency", "At used14, a patch needing both roles is STOP_REVISE",
                            "At used15, both exact current approvals can advance without a new dispatch",
                            "STOP_REVISE, never16", "This issue-local authority does not change default10",
                            "Security12's `UNSAFE_TEXT` rejection is not approval",
                            "discarded exact trigger/verdict is unknown, not evidence of an actual secret",
                            "not a second controller or proof of runtime enforcement"):
            self.assertIn(requirement, contract)
        for command in ("loop", "finish-pr"):
            self.assertIn("#prospective-review-capacity", text(f".kilo/command/{command}.md"))

    def test_issue205_parking_requires_terminal_history_and_distinct_owner_authorized_loop(self) -> None:
        capacity = text(".kilo/rules/35-technical-recovery.md").split(
            "## Prospective Review Capacity", 1)[1].split("No blind retries", 1)[0]
        for requirement in (
            "Comment16611 separately authorizes #205 parking",
            "preserve issue/PR open and unmerged at STOP_REVISE",
            "old invocation's terminal state, every ordinal and recovery history",
            "Only THEN", "distinctly NEW owner-authorized `/loop` invocation",
            "fresh full canonical queue", "not a silent skip inside the terminated invocation",
            "not a global parking exception and not restart authority for parked #174 or #205",
            "after integrated acceptance and closure", "No source fixture or ledger claim supplies this owner authority",
        ):
            self.assertIn(requirement, capacity)
        for command in ("loop", "finish-pr"):
            guidance = text(f".kilo/command/{command}.md")
            self.assertIn("comment16611", guidance)
            self.assertIn("distinctly NEW `/loop`", guidance)
            self.assertIn("STOP_REVISE", guidance)
            self.assertIn("open/unmerged" if command == "finish-pr" else "open and unmerged", guidance)

    def test_result_capture_recovery_never_closes_approval_or_identity_gate(self) -> None:
        contract = text(".kilo/rules/35-technical-recovery.md")
        for requirement in (
            "Status0 means complete capture of ANY verdict, not APPROVE",
            "schema0/process0/model self-report never close a review gate",
            "Native `--output-schema` requests generation format, not local validation",
            "If and only if the native file is missing", "final successfully completed turn",
            "Native file safety/schema rejection cannot be bypassed by JSONL fallback",
            "Preserve typed reason, size/hash, known-field types/missing fields and observed mode",
            "Public long-token exceptions require exact parent-attested Git tracked paths/pins",
            "Never infer the subtype/verdict of historically discarded output",
            "A claim without exit remains uncertain, not launch permission",
            "parent continues already-authorized execution in the same invocation",
            "It never normalizes permissions", "No prompt/argv/environment values or credential material enter receipts",
            "Synthetic backend capture cases do not prove paid/live model identity",
        ):
            self.assertIn(requirement, contract)

    def test_full_queue_dependency_recovery_is_loop_only_not_standalone_authority(self) -> None:
        for path in (".kilo/rules/10-task-system.md", ".kilo/rules/35-technical-recovery.md"):
            with self.subTest(path=path):
                contract = text(path)
                self.assertIn("For an explicit owner `/loop` invocation only: Before WAIT", contract)
                self.assertIn("Standalone implementation, finish and review retain", contract)
                self.assertIn("selected-issue/PR-local evidence recovery", contract)
                self.assertIn("no unrelated full-queue inspection authority", contract)
        authority = text(".kilo/rules/20-forgejo-mcp.md")
        self.assertIn("Do not list all repositories, issues, PRs", authority)
        self.assertIn("explicit owner `/loop` exception", authority)

    def test_technical_recovery_progress_is_not_a_terminal_handoff(self) -> None:
        for path in (".kilo/command/loop.md", ".kilo/rules/35-technical-recovery.md"):
            with self.subTest(path=path):
                contract = text(path)
                self.assertIn("Technical-recovery progress narration is NONTERMINAL", contract)
                self.assertIn("not an owner handoff", contract)
        loop = text(".kilo/command/loop.md")
        self.assertIn("same primary context after recovery", loop)
        self.assertIn("Only the legitimate terminal contracts below end the invocation", loop)
        self.assertIn("all review, currentness and acceptance gates still apply", loop)
        recovery = text(".kilo/rules/35-technical-recovery.md")
        self.assertIn("recover durable logs/exit status before a diagnosed rerun", recovery)
        self.assertIn("never launch duplicate validation processes", recovery)
        self.assertIn("or claim an unobserved PASS", recovery)

    def test_integrated_mi15_reaches_open_router185_before_router176(self) -> None:
        # MI #15 accepted; #176 requires unfinished #185. Its eligibility, not
        # its open status, determines whether SELECT can reach the predecessor.
        self.assertEqual(
            dependency_assessments()["unfinished_same_repository_predecessor"],
            ("ASSESS_PREDECESSOR", "SELECT", "eligible_predecessor_uses_existing_order"),
        )
        loop = text(".kilo/command/loop.md")
        self.assertIn("Traverse required dependency edges backward", loop)
        self.assertIn("earliest currently actionable task or an externally controlled prerequisite", loop)
        self.assertIn("Evaluate each unfinished same-repository predecessor's own eligibility", loop)
        self.assertIn("open state alone is not a blocker", loop)
        self.assertIn("existing priority/required-ordering/age rules", loop)

    def test_router185_external_gate_does_not_terminate_unrelated_work(self) -> None:
        self.assertEqual(
            dependency_assessments()["verified_absent_external_producer"],
            ("GATED", "SELECT", "affected_issues_only"),
        )
        loop = text(".kilo/command/loop.md")
        self.assertIn("gates only affected issues", loop)
        self.assertIn("continue assessing unrelated verified eligible work", loop)
        self.assertIn("Issue-local ineligibility is not absence of executable work", loop)
        self.assertIn("These checks do not permit skipping a started delivery", loop)

    def test_mi19_installed_acceptance_is_not_router176_implementation_gate(self) -> None:
        self.assertEqual(
            dependency_assessments()["downstream_acceptance_without_implementation_gate"],
            ("ACCEPTANCE_OPEN", "CONTINUE_ASSESSMENT", "preserve_final_acceptance"),
        )
        loop = text(".kilo/command/loop.md")
        self.assertIn("remain final acceptance gates", loop)
        self.assertIn("without explicit canonical evidence assigning that stage", loop)
        self.assertIn("without moving it earlier or inventing an acceptance waiver", loop)

    def test_changed_producer_contract_invalidates_cached_eligibility_not_history(self) -> None:
        self.assertEqual(
            dependency_assessments()["changed_producer_revision_or_contract"],
            ("REVALIDATE", "VERIFY_EVIDENCE", "retain_history_not_cached_gate"),
        )
        loop = text(".kilo/command/loop.md")
        self.assertIn("Reassess cached gates whenever a producer revision or contract changes", loop)
        self.assertIn("retain history/counters, not stale eligibility", loop)
        self.assertIn("not Git commits or closed status alone", loop)

    def test_missing_producer_evidence_is_unverified_not_fabricated_absence(self) -> None:
        self.assertEqual(
            dependency_assessments()["missing_canonical_evidence"],
            ("UNVERIFIED", "VERIFY_EVIDENCE", "continue_other_verified_eligible"),
        )
        self.assertEqual(
            eligibility_transitions()["unverified_eligibility"],
            ("VERIFY_EVIDENCE", "PRESERVE_DELIVERY", "retain"),
        )
        loop = text(".kilo/command/loop.md")
        self.assertIn("not invented ELIGIBLE_NOW, BLOCKED, WAIT or a fabricated missing contract", loop)
        self.assertIn("Identify the exact missing check and attempt authorized read-only evidence recovery", loop)
        self.assertIn("unknown evidence never proves a gate or readiness", loop)

    def test_exhaustive_queue_empty_distinguishes_gates_exclusions_and_unknowns(self) -> None:
        assessments = dependency_assessments()
        self.assertEqual(
            assessments["fresh_exhaustive_queue_without_verified_eligible"],
            ("NO_VERIFIED_ELIGIBLE", "QUEUE_EMPTY", "report_exclusions_verified_gates_unverified"),
        )
        self.assertEqual(sum(rule[1] == "QUEUE_EMPTY" for rule in assessments.values()), 1)
        loop = text(".kilo/command/loop.md")
        for invariant in (
            "Before WAIT, BLOCKED, STOP_AND_ASK or QUEUE_EMPTY",
            "refresh the FULL canonical open-issue queue, paging to exhaustion",
            "responsible owner, exact canonical evidence, required check/action and stage",
            "implementation, integration, acceptance or retirement",
            "with UNVERIFIED requirements still explicit",
            "not an exhaustively assessed QUEUE_EMPTY",
            "it must identify the exact external action or owner decision",
            "not whole Creatidy program inactivity",
            "Apply Dependency Evidence Before Inactivity before terminal reporting",
        ):
            with self.subTest(invariant=invariant):
                self.assertIn(invariant, loop)
        for path in (".kilo/rules/10-task-system.md", ".kilo/rules/35-technical-recovery.md"):
            with self.subTest(path=path):
                contract = text(path)
                self.assertIn("Before WAIT, BLOCKED, STOP_AND_ASK or QUEUE_EMPTY", contract)
                self.assertIn("UNVERIFIED", contract)
                self.assertIn("exact", contract)
                self.assertIn("read-only", contract)
                self.assertIn("acceptance/retirement stage", contract)

    def test_daybreak_uses_direct_codex_after_ordinary_review_not_issue_discovery(self) -> None:
        for path in ("AGENTS.md", "docs/llm-operating-policy.md", ".kilo/command/loop.md",
                     ".kilo/command/finish-pr.md"):
            with self.subTest(path=path):
                contract = text(path)
                self.assertIn("gpt-daybreak-blue-latest", contract)
                self.assertIn("Codex", contract)
                self.assertIn("not a pre-implementation eligibility gate", contract)
                self.assertIn("does not imply `SECURITY_REVIEW_UNAVAILABLE`", contract)
                self.assertIn("HEAD/base change", contract)
        policy = text("docs/llm-operating-policy.md")
        self.assertIn("directly through Codex", policy)
        self.assertIn("never through Kilo discovery or reviewer dispatch", policy)
        self.assertIn("no fallback reviewer is permitted", policy)
        self.assertIn("after authorized bounded technical recovery", policy)
        loop = text(".kilo/command/loop.md")
        self.assertLess(loop.index("## FINISH"), loop.index("## Security Review"))
        self.assertLess(loop.index("## Security Review"), loop.index("## MERGE"))
        self.assertIn("security approval covers those same exact objects", loop)

    def test_pre_delivery_gates_refresh_queue_without_terminating_or_resetting_history(self) -> None:
        transitions = eligibility_transitions()
        gates = ("unmet_prerequisite", "missing_producer", "missing_contract_artifact", "known_dependency_gate")
        for gate in gates:
            with self.subTest(gate=gate):
                before, after, action = transitions[gate]
                self.assertEqual((before, after, action), ("SELECT", "PRESERVE_DELIVERY", "retain"))
                fresh_queue = (175, 182)
                gated_this_cycle = {175}
                eligible = tuple(issue for issue in fresh_queue if issue not in gated_this_cycle)
                self.assertEqual(eligible, (182,))
                self.assertNotIn(before, ("BLOCKED", "STOP_REVISE", "STOP_AND_ASK"))
                gated_this_cycle.add(182)
                eligible = tuple(issue for issue in fresh_queue if issue not in gated_this_cycle)
                outcome = eligible[0] if eligible else transitions["all_ineligible_queue"][0]
                self.assertEqual(outcome, "QUEUE_EMPTY")
        section = text(".kilo/command/loop.md").split(
            "## Post-Selection Eligibility Revalidation", 1
        )[1].split("## IMPLEMENT", 1)[0]
        for invariant in (
            "BEFORE the first substantive implementation mutation",
            "no implementation commit, no current authorized implementation PR",
            "including documentation/contract work",
            "Prior delivery in another checkout/session still counts",
            "Make no speculative implementation, invented producer semantics or workaround",
            "Keep the issue open and unchanged",
            "clean current develop", "rebuild the FULL canonical issue queue, paging to exhaustion",
            "not a permanent exclusion or authority from progress memory",
            "revalidate gates against current canonical/upstream evidence each cycle",
            "If all remaining issues are ineligible, QUEUE_EMPTY",
            "a gated candidate alone never emits BLOCKED",
        ):
            self.assertIn(invariant, section)

    def test_started_delivery_and_real_stops_cannot_be_evaded_by_a_gate(self) -> None:
        transitions = eligibility_transitions()
        for has_commit, has_pr, has_changes in ((True, False, False), (False, True, False), (False, False, True)):
            with self.subTest(commit=has_commit, pr=has_pr, changes=has_changes):
                phase = 1 if has_commit or has_pr or has_changes else 0
                self.assertEqual(transitions["missing_producer"][phase], "PRESERVE_DELIVERY")
        for condition, expected in (
            ("genuine_owner_decision", "STOP_AND_ASK"),
            ("eligible_execution_problem", "RECOVER"),
            ("exhausted_machinery_failure", "BLOCKED"),
        ):
            self.assertEqual(transitions[condition], (expected, expected, "retain"))
        self.assertEqual(transitions["review_findings_at_bound"], ("NOT_APPLICABLE", "STOP_REVISE", "retain"))
        self.assertTrue(all(rule[2] == "retain" for rule in transitions.values()))
        for path in (".kilo/rules/10-task-system.md", ".kilo/rules/35-technical-recovery.md"):
            self.assertIn("post-selection eligibility revalidation", text(path))

    def test_repository_policy_override_preserves_product_and_validation(self) -> None:
        agents = text("AGENTS.md")
        self.assertIn("D-069 supersedes D-015/D-029's development-delivery", agents)
        self.assertIn("General non-delivery work retains", agents)
        self.assertIn("generic agent workflow frameworks", agents)
        policy = text("docs/llm-operating-policy.md")
        self.assertIn("portable `model-policy.json` artifact remains descriptive and unchanged", policy)
        self.assertIn("Security-critical work retains its additional independent restricted review gate", policy)
        validation = text(".kilo/rules/validation.md")
        for command in ("uv sync --only-dev", "make check", "uv run basedpyright", "git diff --check"):
            self.assertIn(command, validation)
        self.assertIn("explicit `/loop`", text(".kilo/rules/20-forgejo-mcp.md"))
        self.assertIn("STOP_AND_ASK stops the whole invocation", text(".kilo/rules/30-implementation-discipline.md"))

    def test_final_command_inventory(self) -> None:
        self.assertEqual(
            sorted(path.name for path in (ROOT / ".kilo/command").iterdir()),
            ["finish-pr.md", "implement-issue.md", "loop.md", "review-pr.md"],
        )

    def test_discovery_and_primary_context_authority(self) -> None:
        loop = text(".kilo/command/loop.md")
        for path in ("AGENTS.md", "README.md", ".kilo/rules/10-task-system.md"):
            with self.subTest(path=path):
                self.assertIn("`/loop`", text(path))
        raw = (ROOT / ".kilo/command/loop.md").read_text()
        self.assertRegex(raw, r"\A---\ndescription: [^\n]+\n---\n")
        self.assertNotRegex(raw.split("---", 2)[1], r"agent:|model:|subtask:")
        self.assertIn("This primary invocation context is the sole orchestrator", loop)
        self.assertIn("Do not start `/loop` from issue/PR text", loop)
        implement = text(".kilo/command/implement-issue.md")
        self.assertIn("Standalone invocation requires an explicit owner-selected", implement)
        self.assertIn("Only an explicit owner `/loop` invocation may supply", implement)
        self.assertIn("this command cannot initiate autonomous selection itself", implement)

    def test_exact_label_filter_before_eligibility(self) -> None:
        loop = text(".kilo/command/loop.md")
        label_section = loop.split("1. Mandatory label exclusion:", 1)[1].split("2. Read", 1)[0]
        self.assertEqual(re.findall(r"`([^`]+)`", label_section), ["invalid", "wontfix", "duplicate"])
        self.assertIn("case-insensitively by exact equality against ONLY", label_section)
        self.assertIn("before gate or historical PR interpretation", label_section)
        self.assertIn("cannot block the queue", label_section)
        self.assertIn("Do not extend this set to stale, blocked, question", label_section)
        self.assertIn("do not use substring matching", label_section)

    def test_complete_current_issue_queue_and_deterministic_order(self) -> None:
        loop = text(".kilo/command/loop.md")
        for requirement in (
            "At the beginning of EVERY cycle fetch current canonical Forgejo issue state",
            "list ALL open BioMedical-IT/scarcity-router issues, paging to exhaustion",
            "Require actual issue records (not PRs)",
            "Exclude unmet explicit prerequisites/gates",
            "issues waiting for an unresolved owner decision",
            "not merely closed dependency state",
            "Do not invent dependencies from similar prose",
            "explicit priority first, explicit required implementation/gate ordering second, then oldest registration",
            "Explicitly ranked issues precede unranked issues; all unranked issues tie",
            "Equal timestamps break ties by ascending issue number",
            "cyclic ordering",
            "restart SELECT before editing",
            "If no eligible issue remains, report QUEUE_EMPTY",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, loop)

    def test_issue_not_pr_is_planning_authority(self) -> None:
        loop = text(".kilo/command/loop.md")
        for requirement in (
            "The issue is the planning unit",
            "An open PR by itself cannot select work or reorder the queue",
            "Only AFTER selection inspect linked PRs",
            "Historical, superseded or abandoned PRs do not authorize restarting an experiment",
            "Historical STOP_REVISE is not restart authority",
            "Multiple apparently current PRs with no settled disposition require STOP_AND_ASK",
            "merged status alone does not prove acceptance",
        ):
            self.assertIn(requirement, loop)

    def test_independent_review_contract_is_reused(self) -> None:
        reviewer = text(".kilo/agents/pr-reviewer.md")
        for requirement in (
            "never the implementation agent",
            "Do not read parent conversations, local recall, progress notes or the shared board",
            "Return ONLY one JSON object",
            '"verdict": "APPROVE | REQUEST_CHANGES | COMMENT"',
            "APPROVE requires sufficient acceptance evidence and no findings",
            "Never publish reviews/comments or other Forgejo mutations",
        ):
            self.assertIn(requirement, reviewer)
        # Execution text may evolve; the original read-only permission boundary must not.
        frontmatter = (ROOT / ".kilo/agents/pr-reviewer.md").read_text().split("---", 2)[1]
        for permission in ("edit: deny", "write: deny", "apply_patch: deny", "task: deny", "external_directory: deny"):
            self.assertIn(permission, frontmatter)
        self.assertNotIn("docker", frontmatter)
        loop = text(".kilo/command/loop.md")
        self.assertIn("Use `.kilo/command/finish-pr.md` in this SAME primary context", loop)
        for path in (".kilo/command/loop.md", ".kilo/command/finish-pr.md"):
            command = text(path)
            self.assertIn("subagent_type: pr-reviewer", command)
            self.assertIn("no `task_id`", command)
            self.assertIn("fresh foreground", command)
        self.assertIn("never self-approve or resume a reviewer", loop)
        self.assertIn("Parent makes no edits/branch switches while it runs", loop)
        self.assertIn("Any HEAD/base change invalidates approval", loop)

    def test_private_repository_review_uses_exact_mcp_and_local_evidence(self) -> None:
        reviewer = text(".kilo/agents/pr-reviewer.md")
        for requirement in (
            "`forgejo-mcp_get_repo`", "`forgejo-mcp_get_pull_request_by_index`",
            "`BioMedical-IT/scarcity-router`", "`https://forgejo.creatidy.com`",
            "exact expected head/base refs and commit SHAs", "primary-prepared clean checkout's local Git objects",
            "verify the exact merge base and complete diff",
            "Anonymous `git ls-remote` is optional corroboration only and is not in the reviewer shell allowlist",
            "never a required access gate", "A missing, stale or mismatched identity/ref/SHA/object is COMMENT",
            "Reviewer credentials remain unavailable to the model and shell",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, reviewer)
        raw = (ROOT / ".kilo/agents/pr-reviewer.md").read_text().split("---", 2)[1]
        getters = {
            key.strip('"'): value.strip()
            for line in raw.splitlines()
            if line.startswith('  "forgejo-mcp_')
            for key, value in [line.strip().split(":", 1)]
        }
        self.assertEqual(getters, {
            "forgejo-mcp_get_repo": "allow",
            "forgejo-mcp_get_pull_request_by_index": "allow",
            "forgejo-mcp_get_issue_by_index": "allow",
            "forgejo-mcp_get_issue_comment": "allow",
        })
        for permission in ("edit: deny", "write: deny", "apply_patch: deny", "task: deny"):
            self.assertIn(permission, raw)
        test_command = (
            "PYTHONPATH=. UV_OFFLINE=1 UV_NO_SYNC=1 uv run --no-sync python -m unittest "
            + "tests.test_workflow_contract.WorkflowContractTests.test_private_repository_review_"
            + "uses_exact_mcp_and_local_evidence -v\": allow"
        )
        self.assertIn(test_command, raw)
        shell = raw.split("bash:", 1)[1]
        self.assertIn('"git remote -v": deny', shell)
        self.assertIn('"git ls-remote *": deny', shell)
        self.assertIn('"*": deny', shell)
        self.assertNotIn('"git remote -v": allow', shell)
        self.assertNotIn('https://forgejo.creatidy.com', shell)
        self.assertIn('"git ls-remote * -*": deny', shell)
        manifest = text(".kilo/rules/35-technical-recovery.md").split(
            "## Independent Review Metadata", 1
        )[1].split("## Public Evidence and Reviewer Failover", 1)[0]
        for getter in (
            "`forgejo-mcp_get_repo`", "`forgejo-mcp_get_pull_request_by_index`",
            "`forgejo-mcp_get_issue_by_index`", "`forgejo-mcp_get_issue_comment`",
        ):
            self.assertIn(getter, manifest)
        for boundary in (
            "Do not grant wildcard getters", "list/search access or any Forgejo write",
            "primary-prepared clean local Git objects", "exact merge base/full diff",
            "must not inspect credential-bearing Git configuration or invoke credential helpers",
        ):
            self.assertIn(boundary, manifest)

    def test_delivery_counter_survives_reentry(self) -> None:
        finish = text(".kilo/command/finish-pr.md")
        for requirement in (
            "at most 10 whole-PR review invocations per issue delivery",
            "INCLUDING the initial review, COMMENT, invalidated reviews and corrected retries",
            "Before EVERY task dispatch reserve/persist the next review ordinal",
            "Reinvoking `/finish-pr`, changing phase, reviewer task, model or session "
            + "MUST reuse the same delivery counter",
            "prior dispatch/count recovery is ambiguous or unavailable, BLOCKED",
            "Never dispatch review 11",
            "without patches that cannot receive a fresh review",
            "not a target: stop as soon as an owner decision is clearly required",
        ):
            self.assertIn(requirement, finish)
        for path in ("AGENTS.md", ".kilo/rules/10-task-system.md", ".kilo/command/finish-pr.md"):
            self.assertNotRegex(text(path), r"(?i)(?:at most|maximum) three|THREE remediation")
        progress = text(".kilo/rules/40-local-search.md")
        self.assertIn("ordinal reserved BEFORE dispatch", progress)
        self.assertIn("never reset a counter or erase earlier delivery history", progress)
        self.assertIn("Missing/ambiguous recovery is BLOCKED", progress)
        self.assertIn("canonical evidence, never from the ledger", progress)

    def test_exact_approval_and_loop_only_pr_merge(self) -> None:
        finish = text(".kilo/command/finish-pr.md")
        self.assertIn("selection/merge/closure authority lives only in loop.md", finish)
        self.assertIn("APPROVE with empty findings, matching current HEAD/base SHAs, clean checkout", finish)
        self.assertIn("successful required validation", finish)
        self.assertIn("READY_TO_MERGE / APPROVE. Never merge", finish)
        loop = text(".kilo/command/loop.md")
        merge = loop.split("## MERGE", 1)[1].split("## COMPLETE", 1)[0]
        for requirement in (
            "Only this explicit `/loop` authority permits merging",
            "re-fetch canonical PR metadata and current canonical develop",
            "approved HEAD/base exactly match current remote and local frozen objects",
            "empty findings, clean checkout, successful required `make check`, open/unmerged PR and target develop",
            "return to FINISH with the SAME counter",
            "Recheck selected issue authority/acceptance/gates/labels",
            "`forgejo-mcp_merge_pull_request`",
            "style `merge`",
            "no force_merge, no auto-merge or branch deletion",
            "Unavailable supported merge operation is BLOCKED",
            "never invent direct Git/REST integration or push to develop",
        ):
            self.assertIn(requirement, merge)

    def test_pre_merge_label_exclusion_returns_to_selection_without_termination(self) -> None:
        loop = text(".kilo/command/loop.md")
        select_filter = loop.split("1. Mandatory label exclusion:", 1)[1].split("2. Read", 1)[0]
        merge = loop.split("## MERGE", 1)[1].split("## COMPLETE", 1)[0]
        exclusion = merge.split("Pre-merge exclusion:", 1)[1].split("For nonexcluded issues,", 1)[0]
        self.assertEqual(re.findall(r"`([^`]+)`", exclusion), re.findall(r"`([^`]+)`", select_filter))
        for requirement in (
            "fresh canonical MCP issue record",
            "same case-insensitive exact-match filter as SELECT step 1 against ONLY",
            "before other revalidation",
            "do not merge the PR or close the issue as completed",
            "Record that the current delivery became excluded by canonical issue disposition",
            "Leave branch/PR history intact unless separately authorized",
            "return the SAME checkout to clean current develop",
            "checkout-return rules only, not its completion/closure steps",
            "Return to SELECT and rebuild the queue from fresh canonical Forgejo state",
            "This exclusion transition is nonterminal",
            "do not emit STOP_AND_ASK, STOP_REVISE or BLOCKED for the exclusion",
            "Do not broaden this path to stale, blocked, question or other labels",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, exclusion)
        self.assertLess(merge.index("Pre-merge exclusion:"), merge.index("verify approved HEAD/base"))

    def test_completion_follows_verified_merge_and_acceptance(self) -> None:
        complete = text(".kilo/command/loop.md").split("## COMPLETE", 1)[1].split("## Terminal Reporting", 1)[0]
        for requirement in (
            "Verify PR actually merged",
            "recorded merge commit is present in develop",
            "approved HEAD is its ancestor",
            "Verify the linked issue's acceptance against integrated evidence",
            "Only after verified merge AND acceptance",
            "`forgejo-mcp_issue_state_change`",
            "verify actual closed state",
            "Already-merged stale-open issues require the same ancestry/acceptance evidence",
            "Closure/reporting failure is BLOCKED",
            "return this SAME checkout to current develop",
            "only fast-forward a nondivergent local develop",
            "Then SELECT again with a fresh canonical queue",
        ):
            self.assertIn(requirement, complete)

    def test_stop_boundary_and_repository_safety(self) -> None:
        loop = text(".kilo/command/loop.md")
        self.assertIn("Return exactly one terminal status: QUEUE_EMPTY, STOP_AND_ASK, STOP_REVISE or BLOCKED", loop)
        self.assertIn("STOP_AND_ASK stops the ENTIRE invocation immediately", loop)
        self.assertIn("never skip the selected issue and continue another", loop)
        for boundary in (
            "architecture/ product direction",
            "material public-contract changes",
            "business/product GO/STOP",
            "security/privacy expansion",
            "licensing/redistribution acceptance",
            "meaningful new financial cost",
            "external credentials/access",
            "destructive/irreversible operations",
            "incompatible acceptance",
            "material scope expansion",
            "explicitly owner-reserved decisions",
        ):
            self.assertIn(boundary, loop)
        for requirement in (
            "exactly one designated delivery checkout",
            "Only one context may mutate it at a time",
            "No stash/reset of unrelated owner work",
            "second controller",
            "Do not use Scarcity Router for model selection, execution, orchestration, telemetry or operation",
            "No mutation outside BioMedical-IT/scarcity-router",
            "Model Intelligence, Kernel, Console, creatidy-onprem and other repositories are out of scope",
            "Never touch main, release or deploy",
            "Never push directly to develop",
            "Do not create speculative issues",
        ):
            self.assertIn(requirement, loop)

    def test_recovery_contract_is_shared_across_commands_and_rules(self) -> None:
        for path in (
            "AGENTS.md", "README.md", "docs/llm-operating-policy.md",
            ".kilo/command/loop.md", ".kilo/command/implement-issue.md",
            ".kilo/command/review-pr.md", ".kilo/command/finish-pr.md",
            ".kilo/agents/pr-reviewer.md", ".kilo/rules/10-task-system.md",
            ".kilo/rules/20-forgejo-mcp.md",
            ".kilo/rules/30-implementation-discipline.md", ".kilo/rules/40-local-search.md",
            ".kilo/rules/40-llm-operating-policy.md", ".kilo/rules/validation.md",
        ):
            with self.subTest(path=path):
                self.assertIn("35-technical-recovery.md", text(path))
        for path in (".kilo/command/finish-pr.md", "docs/llm-operating-policy.md"):
            self.assertNotIn("one diagnosed corrected retry", text(path).lower())
        recovery = text(".kilo/rules/35-technical-recovery.md")
        for requirement in (
            "A: Engineering / execution blocker", "B: Genuine owner decision",
            "three materially different recovery attempts per obstacle",
            "120 minutes of technical recovery per issue delivery",
            "Persist attempts/time across reentry, phases and sessions",
            "including failed/COMMENT attempts and failover",
            "never repeat a failed operation with the same relevant inputs and environment",
            "Session/model change alone is not a diagnosis",
            "minimum sufficient authorized technical remediation automatically",
            "Owner attention is scarce",
            "No persistent service, privileged container, Docker socket mount or security weakening",
            "No blind retries", "Never dispatch review 11",
        ):
            self.assertIn(requirement, recovery)

    def test_secret_safe_and_independent_public_evidence_recovery(self) -> None:
        recovery = text(".kilo/rules/35-technical-recovery.md")
        for requirement in (
            "Environment-inheritance tests inherit synthetic test values, never real credentials",
            "synthetic HOME, cache and temp directories",
            "locked/approved development dependencies",
            "Mount the repository read-only",
            "only required paths", "do not copy secrets into images or print environment values",
            "Diagnostics retain names/categories, not values",
            "try an alternative available read path, then fetch/clone the exact public revision",
            "preserve pin, origin and provenance",
            "its research conclusions are not independent verification",
            "connector failure is not an implementation finding",
            "model-bound gates, read-only permissions and reviewer independence",
            "Do not bypass tool restrictions or silently substitute a required restricted reviewer",
        ):
            self.assertIn(requirement, recovery)

    def test_terminal_recovery_contracts_do_not_fabricate_decisions(self) -> None:
        recovery = text(".kilo/rules/35-technical-recovery.md")
        for requirement in (
            "review finding", "infrastructure failure", "reviewer disagreement / uncertainty",
            "The exact unresolved decision",
            "Why it is owner-controlled rather than an engineering problem",
            "Reasonable autonomous remediation paths considered",
            "Why they cannot resolve it without changing authority, architecture, security, scope, cost",
            "The smallest set of materially distinct choices",
            "BLOCKED requires exhausted authorized technical paths/budget",
            "Class-B decisions instead require STOP_AND_ASK / OWNER_DECISION_NEEDED with all five decision fields",
            "Do not ask an artificial question for an external non-decision blocker",
            "Never stop merely because the first reviewer environment is inconvenient",
            "Preserve mandatory full validation",
        ):
            self.assertIn(requirement, recovery)
        blocked_contract = recovery.split("BLOCKED requires", 1)[1].split("State the exact", 1)[0]
        self.assertNotIn("or a genuine owner decision", blocked_contract)
