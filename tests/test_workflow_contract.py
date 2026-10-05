"""Offline consistency guards for command text, not proof of runtime execution."""

import re
import unittest
from pathlib import Path

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


class WorkflowContractTests(unittest.TestCase):
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
