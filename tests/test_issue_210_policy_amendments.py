"""Contract checks for current D-069A and #212 superseding notices."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text()


class Issue210PolicyAmendmentTests(unittest.TestCase):
    def test_completed_212_notices_explicitly_supersede_historical_text(self) -> None:
        markers = {
            "AGENTS.md": "Current D-069A And #212 Status",
            ".kilo/command/loop.md": "Current #212 Status",
            ".kilo/rules/10-task-system.md": "Current #212 Status",
            ".kilo/rules/35-technical-recovery.md": "Current #212 Status",
            "docs/decisions.md": "Current D-069A And #212 Amendment",
            "docs/llm-operating-policy.md": "Current D-069A Amendment",
        }
        for path, marker in markers.items():
            with self.subTest(path=path):
                content = " ".join(text(path).split())
                self.assertIn(marker.lower(), content.lower())
                current_notice = content.lower().index(marker.lower())
                self.assertIn("supersedes", content.lower()[current_notice:])
                self.assertLess(current_notice, content.lower().index("no #20 reuse", current_notice))

    def test_root_notice_overrides_unmergeable_instruction_mirrors(self) -> None:
        current = text("AGENTS.md").split("## Mission and priority", 1)[0].lower()
        self.assertIn("any command/profile/rule mirror", current)
        self.assertIn("all #1-20 for #212/pr #213 are consumed", current)
        self.assertIn("121ee3fe33b74dc38b2401d22e7aa6df1b124bc9", current)

    def test_d069a_amendment_records_exact_210_scope_and_router_limit(self) -> None:
        decisions = " ".join(text("docs/decisions.md").split())
        amendment = decisions.split("### D-001", 1)[0]
        for requirement in (
            "121ee3fe33b74dc38b2401d22e7aa6df1b124bc9",
            "1dcb4a08d9d57d02ed67bf8e7f7d15275c5b3ecf",
            "cddcac0d7a475c32eb2d8ad942b4f004c1ea1256",
            "maximum11",
            "#10 is ordinary",
            "#11 is owner-manual security only after #10 passes",
            "#12 is prohibited",
            "ses_edcee3d47ffedWKe8vBjTUNCG7",
            "prt_1408235dd001UMJyvip9N29CFM",
            "1792083637749",
            "3cfe86b95cf3faa7728c90137222966b226a687c24046bfad92d3283c362913b",
            "#18082/#18085",
            "old #17258/file-copy hash omitted two constraints",
            "prt_13ca21f3c001W5Szn0yCzs3E89",
            "88587ec09f71a4f4cf74456d35fc93f0cea0560b413fdaa08db8d43f1b845319",
            "#210 comment #18085",
            "RUNTIME_UNOBSERVABLE",
            "Never infer `RUNTIME_VERIFIED`",
            "unauthorized substitution is FAIL",
            "No automatic Daybreak",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement.lower(), amendment.lower())

    def test_manual_assurance_is_router_scoped_and_keeps_other_proof_mandatory(self) -> None:
        for path in (
            "AGENTS.md",
            "docs/llm-operating-policy.md",
            ".kilo/rules/40-llm-operating-policy.md",
        ):
            content = text(path)
            with self.subTest(path=path):
                self.assertIn("RUNTIME_UNOBSERVABLE", content)
                self.assertIn("RUNTIME_VERIFIED", content)
                self.assertIn("requested", content.lower())
                self.assertIn("context/tool", content.lower())
                self.assertIn("mismatch", content.lower())
                self.assertIn("FAIL", content)
                self.assertIn("no automatic daybreak", " ".join(content.lower().split()))


if __name__ == "__main__":
    _ = unittest.main()
