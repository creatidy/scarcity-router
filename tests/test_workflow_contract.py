"""Workflow policy checks, including an opt-in real Kilo permission-resolver probe."""

import json
import os
import re
import subprocess
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
    def test_changed_kilo_frontmatter_remains_valid_yaml(self) -> None:
        for path in (
            ".kilo/agents/pr-reviewer.md",
            ".kilo/command/finish-pr.md",
            ".kilo/command/loop.md",
            ".kilo/command/review-pr.md",
        ):
            with self.subTest(path=path):
                source = (ROOT / path).read_text()
                self.assertTrue(source.startswith("---\n"))
                parsed: object = cast(object, yaml.safe_load(source.split("---", 2)[1]))
                if not isinstance(parsed, dict):
                    self.fail(f"{path} frontmatter must be a YAML mapping")
                metadata = cast(dict[str, object], parsed)
                self.assertIsInstance(metadata.get("description"), str)

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

    def test_d069_exception_contract_is_finite_canonical_and_default_ten(self) -> None:
        raw_decision = (ROOT / "docs/decisions.md").read_text().split("### D-069A", 1)[1].split(
            "### D-070", 1
        )[0]
        decision = " ".join(raw_decision.split()).lower()
        for requirement in (
            "default remains ten whole-pr reviews",
            "explicit direct owner decision plus a canonical issue/pr record",
            "positive finite integer maximum",
            "local delivery ledger records evidence/counters, not authority",
            "not from permanent ordinal-to-role rules",
            "direct owner message is present in the primary root context",
            "ea6f2d1236827075a1dd853ae1275ad178e62b63761eaa557e943176594a2049",
            "991c4c550557411334fde1dfdb3a89328fa4e165811cad7eff8946914a01bd57",
            "runtime_verified",
            "runtime_unobservable",
            "dispatch_verified",
            "not cryptographic certainty",
            "adrian explicitly accepts that limitation",
            "cli exit-zero",
            "mandatory independent official",
            "manual router security gate",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, decision)
        cases: dict[str, tuple[str, str]] = {}
        for line in raw_decision.splitlines():
            if not line.strip().startswith("|"):
                continue
            cells = tuple(cell.strip() for cell in line.strip().strip("|").split("|"))
            if cells[0] in ("Case", "---"):
                continue
            self.assertEqual(len(cells), 3, msg=f"malformed D-069A case row: {line}")
            cases[cells[0].lower()] = (cells[1].lower(), cells[2].lower())
        expected_cases = {
            "normal delivery": ("no exception record", "default maximum remains 10"),
            "ledger-only extension": (
                "`.task_progress.md` says `max=12`; no canonical owner/source record",
                "deny the exception",
            ),
            "unverified transcript": ("no matching direct owner source/message digest", "deny the exception"),
            "scope mismatch": ("different repository, issue, linked pr or bounded task", "deny the exception"),
            "malformed or superseded authority": ("missing/noninteger/zero maximum", "deny/stop"),
            "verified finite exception": ("exact canonical linked repo/issue/pr record", "use only that maximum"),
            "existing pr209 exception": (
                "canonical #185 comment #16956 plus direct owner source sha-256",
                "#185/pr #209 only, maximum12; #11 ordinary, #12 owner-manual security, no #13",
            ),
            "owner-manual reservation": (
                "the ordinary gate passed but the next security ordinal is not reserved against the exact current objects",
                "do not return the owner handoff or launch codex; reserve/persist first",
            ),
            "currentness change": ("differs from the exact current pr", "approval becomes stale"),
            "role change": ("mandatory security evidence is not", "currently required security role"),
            "incomplete manual security evidence": (
                "official binary provenance, requested dispatch, invocation/turn/output provenance",
                "keep security gate open",
            ),
            "owner-accepted unobservable backend": (
                "supported path exposes no independent backend identity",
                "record `runtime_unobservable`; the manual gate may pass",
            ),
            "parent-only generated receipt": ("outside the immutable pre-dispatch source catalog", "do not authorize the reviewer to read generated output"),
        }
        self.assertEqual(set(cases), set(expected_cases))
        for case, (evidence, result) in expected_cases.items():
            with self.subTest(case=case):
                actual_evidence, actual_result = cases[case]
                self.assertIn(evidence, actual_evidence)
                self.assertIn(result, actual_result)
        for path in (
            "AGENTS.md", "docs/llm-operating-policy.md", ".kilo/command/loop.md",
            ".kilo/command/finish-pr.md", ".kilo/command/review-pr.md",
            ".kilo/rules/10-task-system.md", ".kilo/rules/35-technical-recovery.md",
            ".kilo/rules/40-llm-operating-policy.md",
        ):
            with self.subTest(path=path):
                contract = text(path)
                self.assertIn("D-069A", contract)
                self.assertIn("default", contract.lower())
                self.assertRegex(contract, r"(?i)\b(?:10|ten)\b")
                self.assertNotRegex(contract, r"(?i)never dispatch review\s*11")

    def test_router_daybreak_is_owner_manual_and_independently_verified(self) -> None:
        for path in ("AGENTS.md", "docs/llm-operating-policy.md", ".kilo/command/loop.md",
                     ".kilo/command/finish-pr.md", ".kilo/command/review-pr.md",
                     ".kilo/rules/40-llm-operating-policy.md"):
            with self.subTest(path=path):
                contract = text(path)
                self.assertIn("gpt-daybreak-blue-latest", contract)
                self.assertIn("Codex", contract)
                self.assertTrue(
                    any(phrase in contract.lower() for phrase in ("never invoke", "must not invoke")),
                    msg=f"{path} must prohibit automatic Daybreak invocation",
                )
                self.assertIn("READY_FOR_OWNER_SECURITY_REVIEW", contract)
                self.assertIn("HEAD/base", contract)
        policy = text("docs/llm-operating-policy.md")
        self.assertIn("the owner personally invokes", policy)
        self.assertIn("requested `gpt-daybreak-blue-latest`", policy)
        self.assertIn("official Codex executable", policy)
        self.assertIn("CLI exit zero", policy)
        self.assertIn("RUNTIME_VERIFIED", policy)
        self.assertIn("RUNTIME_UNOBSERVABLE", policy)
        self.assertIn("DISPATCH_VERIFIED", policy)
        self.assertIn("explicit acceptance of that limitation", policy)
        self.assertNotIn("Require `RUNTIME_VERIFIED`", policy)
        loop = text(".kilo/command/loop.md")
        self.assertLess(loop.index("## FINISH"), loop.index("## Security Review"))
        self.assertLess(loop.index("## Security Review"), loop.index("## MERGE"))
        self.assertIn("owner-manual security approval covers those same exact objects", loop)
        self.assertIn("READY_FOR_OWNER_SECURITY_REVIEW", loop)
        self.assertIn("implementation agent must never invoke Daybreak/Codex automatically", loop)

    def test_owner_manual_review_ordinal_is_reserved_before_handoff(self) -> None:
        sections = {
            "docs/decisions.md": ("Manual Scarcity Router security gate:", "Manual result evidence:"),
            "AGENTS.md": ("Security-critical work carries an additional", "The detailed operating policy is"),
            "docs/llm-operating-policy.md": (
                "After required validation and ordinary whole-PR approval",
                "Classify `SECURITY_REVIEW_UNAVAILABLE`",
            ),
            ".kilo/command/finish-pr.md": ("For `security_critical` work", "Return one final report"),
            ".kilo/command/loop.md": ("## Security Review", "## MERGE"),
            ".kilo/command/review-pr.md": ("An ordinary APPROVE", None),
            ".kilo/rules/10-task-system.md": (
                "Current APPROVE with empty findings satisfies only ordinary",
                "At the effective authorized bound",
            ),
            ".kilo/rules/40-llm-operating-policy.md": (
                "For Scarcity Router `security_critical` work",
                "Completed PASS gates",
            ),
        }
        for path, (start_marker, end_marker) in sections.items():
            contract = text(path)
            section = contract.split(start_marker, 1)[1]
            if end_marker is not None:
                section = section.split(end_marker, 1)[0]
            with self.subTest(path=path):
                lowered = section.lower()
                ready = lowered.index("ready_for_owner_security_review")
                self.assertIn("reserve", lowered[:ready])
                self.assertTrue("reserved ordinal" in lowered or "owner-manual ordinal" in lowered)
                self.assertTrue(any(phrase in lowered for phrase in (
                    "cannot be reused", "cannot be silently reused", "never silently reuse",
                )))
                self.assertIn("consumed", lowered)

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
            "Do not read prior findings, parent conversations, local ledgers, manifests or generated receipts",
            "Return only one JSON object",
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

    def test_private_reviewer_is_packet_read_only_and_has_no_remote_or_shell_tools(self) -> None:
        reviewer_source = (ROOT / ".kilo/agents/pr-reviewer.md").read_text()
        reviewer = " ".join(reviewer_source.split())
        metadata: object = cast(object, yaml.safe_load(reviewer_source.split("---", 2)[1]))
        if not isinstance(metadata, dict):
            self.fail("reviewer frontmatter must be a YAML mapping")
        permissions_value = cast(dict[str, object], metadata).get("permission")
        if not isinstance(permissions_value, dict):
            self.fail("reviewer permission frontmatter must be a mapping")
        permissions = cast(dict[str, object], permissions_value)
        self.assertEqual(permissions.get("*"), "deny")
        for tool in (
            "bash", "glob", "grep", "list", "semantic_search", "external_directory",
            "edit", "write", "apply_patch", "task", "board_read", "board_post",
            "kilo_local_recall", "kilo_memory_recall", "agent_manager", "webfetch", "websearch",
        ):
            with self.subTest(tool=tool):
                self.assertEqual(permissions.get(tool), "deny")
        self.assertEqual(permissions.get("forgejo-mcp_*"), "deny")
        for tool in (
            "forgejo-mcp_get_repo", "forgejo-mcp_get_pull_request_by_index",
            "forgejo-mcp_get_issue_by_index", "forgejo-mcp_get_issue_comment",
        ):
            self.assertEqual(permissions.get(tool), "deny")
        self.assertTrue(permissions.get("read"))
        self.assertIn("immutable parent-prepared packet", reviewer)
        self.assertIn("Do not call Forgejo, Git, Bash", reviewer)
        self.assertIn("prior findings", reviewer)
        self.assertIn("parent audits the actual native task export", reviewer)

        read_permissions = cast(dict[str, str], permissions["read"])
        self.assertEqual(read_permissions.get("**/scarcity-router/**"), "deny")
        self.assertEqual(read_permissions.get("/home/**"), "deny")
        self.assertEqual(read_permissions.get("/tmp/**"), "deny")
        allowed = {pattern for pattern, action in read_permissions.items() if action == "allow"}
        self.assertTrue(allowed)
        self.assertTrue(all(".review-evidence-*/pr-reviewer-input/" in pattern for pattern in allowed))
        self.assertTrue(all("/../" not in pattern for pattern in allowed))
        self.assertIn("**/.review-evidence-*/pr-reviewer-input/**", read_permissions)
        self.assertEqual(read_permissions["**/.review-evidence-*/pr-reviewer-input/**"], "deny")
        for forbidden in (
            "**/*.py", "**/*.md", "**/*.json", "AGENTS.md", "README.md",
            "**/.review-evidence-*/**/source-index.md", "**/.review-evidence-*/**/audit.py",
        ):
            self.assertNotIn(forbidden, read_permissions)

    def test_kilo_runtime_resolves_reviewer_tool_denies_with_synthetic_home(self) -> None:
        binary = os.environ.get("KILO_PERMISSION_RUNTIME_TEST_BIN")
        if not binary:
            self.skipTest("set KILO_PERMISSION_RUNTIME_TEST_BIN to exercise Kilo's effective policy")
        with tempfile.TemporaryDirectory(prefix="kilo-reviewer-runtime-home-") as home:
            completed = subprocess.run(
                [binary, "debug", "agent", "pr-reviewer"],
                cwd=ROOT,
                env={
                    "PATH": "/usr/bin:/bin",
                    "HOME": home,
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
        decoded = cast(object, json.loads(completed.stdout))
        if not isinstance(decoded, dict):
            self.fail("Kilo resolver output must be a JSON object")
        resolved = cast(dict[str, object], decoded)
        tools_value = resolved.get("tools")
        if not isinstance(tools_value, dict):
            self.fail("Kilo resolver output must include a tool mapping")
        tools = cast(dict[str, object], tools_value)
        for tool in (
            "bash", "glob", "grep", "list", "semantic_search", "external_directory",
            "task", "board_read", "board_post", "kilo_local_recall", "kilo_memory_recall",
        ):
            with self.subTest(tool=tool):
                self.assertFalse(tools.get(tool, False))
        self.assertTrue(tools.get("read", False))
        self.assertFalse(any(name.startswith("forgejo-mcp_") for name in tools))
        rules_value = resolved.get("permission")
        if not isinstance(rules_value, list):
            self.fail("Kilo resolver output must include a permission list")
        rules = [
            cast(dict[str, object], item)
            for item in cast(list[object], rules_value)
            if isinstance(item, dict)
        ]
        self.assertTrue(any(rule.get("permission") == "bash" and rule.get("action") == "deny"
                            for rule in rules))
        self.assertTrue(any(rule.get("permission") == "forgejo-mcp_*" and rule.get("action") == "deny"
                            for rule in rules))
        self.assertTrue(any(rule.get("permission") == "read"
                            and rule.get("pattern") == "**/scarcity-router/**"
                            and rule.get("action") == "deny" for rule in rules))

    def test_reviewer_packet_and_parent_audit_contract_prevent_checkout_or_receipt_access(self) -> None:
        reviewer = " ".join(text(".kilo/agents/pr-reviewer.md").split())
        for requirement in (
            "complete canonical issue acceptance evidence",
            "exact base-to-HEAD patch/source bundle",
            "no workspace checkout access",
            "source bytes or provenance are missing",
            "Previous review findings are intentionally absent",
            "`checks_run` remains an empty array",
            "parent audits the actual native task export",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, reviewer)
        manifest = " ".join(text(".kilo/rules/35-technical-recovery.md").split())
        for requirement in (
            "reviewer receives no direct Forgejo getter/writer",
            "immutable packet supplies complete acceptance evidence",
            "exact Git commit/blob/patch bytes",
            "denies Bash/Git, all direct Forgejo MCP tools",
            "before any call can execute",
            "Post-execution trace audit checks that only permitted Read calls occurred",
        ):
            self.assertIn(requirement, manifest)
        for path in (".kilo/command/review-pr.md", ".kilo/command/finish-pr.md", ".kilo/command/loop.md"):
            contract = " ".join(text(path).split())
            self.assertIn("immutable", contract)
            self.assertIn("native Read", contract)
            self.assertIn("parent", contract)

    def test_delivery_counter_survives_reentry(self) -> None:
        finish = " ".join(text(".kilo/command/finish-pr.md").split())
        for requirement in (
            "default at most 10 whole-PR review invocations per issue delivery",
            "INCLUDING the initial review, COMMENT, invalidated reviews and corrected retries",
            "Only an exact, canonical owner-authorized D-069A exception",
            "Before EVERY task dispatch reserve/persist the next review ordinal",
            "Reinvoking `/finish-pr`, changing phase, reviewer task, model or session "
            + "MUST reuse the same delivery counter",
            "prior dispatch/count recovery is ambiguous or unavailable, BLOCKED",
            "at the exact effective maximum return STOP_REVISE",
            "without patches that cannot receive a fresh review in the remaining capacity",
            "READY_FOR_OWNER_SECURITY_REVIEW",
            "not a target: stop as soon as an owner decision is clearly required",
        ):
            self.assertIn(requirement, finish)
        self.assertNotIn("Never dispatch review 11", finish)
        self.assertIn("default is at most 10", text("AGENTS.md"))
        self.assertIn("D-069A", text("docs/decisions.md"))
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
        self.assertIn("APPROVE with empty findings, matching current refs, parent-audit PASS", finish)
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
        loop = " ".join(text(".kilo/command/loop.md").split())
        self.assertIn(
            "Return exactly one terminal status: QUEUE_EMPTY, STOP_AND_ASK, STOP_REVISE, BLOCKED or `READY_FOR_OWNER_SECURITY_REVIEW`",
            loop,
        )
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
            "No blind retries", "exact verified maximum",
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
