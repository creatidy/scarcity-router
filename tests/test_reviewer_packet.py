from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from typing import cast
from unittest.mock import patch

from tools.reviewer_packet import (
    PacketError,
    PacketInspection,
    RuntimeWorkspace,
    KILO_SOURCE_PACKET_PATHS,
    OWNER_EVIDENCE_PACKET_PATHS,
    STANDALONE_PACKET_FIXED_PATHS,
    audit_standalone_session,
    capture_session_id,
    create_synthetic_standalone_packet,
    main,
    make_per_run_permission_map,
    prepare_standalone_environment,
    standalone_environment,
    summarize_failed_standalone_session,
    standalone_preflight_prompt,
    standalone_review_prompt,
    standalone_run_argv,
    standalone_packet_path_plan,
    validate_packet as _validate_packet,
    validate_synthetic_preflight_packet,
    verify_effective_standalone_agent,
)


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / ".kilo/agents/pr-reviewer-standalone.md"
BASE = "1" * 40
HEAD = "2" * 40
MERGE_BASE = BASE


def validate_packet(
    packet_root: Path,
    manifest_path: Path,
    *,
    source_root: Path | None = None,
    profile_source_root: Path | None = None,
    external_source_roots: dict[str, Path] | None = None,
    require_external_sources: bool = True,
) -> PacketInspection:
    return _validate_packet(
        packet_root,
        manifest_path,
        source_root=source_root,
        profile_source_root=profile_source_root,
        external_source_roots=external_source_roots,
        require_external_sources=require_external_sources,
        allow_synthetic_fixture=True,
    )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest_record(path: str, data: bytes, kind: str, origin: str, revision: str) -> dict[str, object]:
    return {
        "path": path,
        "size": len(data),
        "sha256": _sha(data),
        "mode": 0o444,
        "link_count": 1,
        "kind": kind,
        "origin": origin,
        "revision": revision,
    }


def _load_object(text: str) -> dict[str, object]:
    value = cast(dict[str, object], json.loads(text))
    return value


def _rewrite_packet_file(
    packet: Path, manifest_path: Path, name: str, data: bytes,
) -> None:
    _ = os.chmod(packet, 0o755)
    target = packet / name
    _ = os.chmod(target, 0o644)
    _ = target.write_bytes(data)
    _ = os.chmod(target, 0o444)
    manifest = _load_object(manifest_path.read_text(encoding="utf-8"))
    for row in cast(list[dict[str, object]], manifest["files"]):
        if row["path"] == name:
            row["size"] = len(data)
            row["sha256"] = _sha(data)
    if name in {"owner-bootstrap.txt", "owner-210.txt"}:
        for row in cast(list[dict[str, object]], manifest["owner_sources"]):
            if row["packet_path"] == name:
                row["sha256"] = _sha(data)
    _ = manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    _ = os.chmod(packet, 0o555)


def _expected_line_map(path: Path) -> dict[int, str]:
    return {number: line for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)}


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, text=True,
    )
    return result.stdout.strip()


def _make_packet(parent: Path) -> tuple[Path, Path]:
    packet = parent / "packet"
    packet.mkdir()
    issue_body = b"The complete accepted scope is supplied here."
    issue_body_sha = _sha(issue_body)
    full_read_paths = [
        "external-kilo-source.txt", "index.md", "owner-210-receipt.txt", "owner-210.txt",
        "owner-bootstrap-receipt.txt", "owner-bootstrap.txt", "patch.diff", "source-00.md",
    ]
    read_plan: dict[str, object] = {
        "full_read_paths": [f"packet/{path}" for path in full_read_paths],
        "source_ranges": {},
    }
    contents = {
        "index.md": (
            "PR body text is non-authoritative; digest only.\n"
            "<BEGIN-PACKET-READ-PLAN>\n"
            f"{json.dumps(read_plan, sort_keys=True)}\n"
            "<END-PACKET-READ-PLAN>\n"
            f'<BEGIN-CANONICAL-ISSUE-212-BODY sha256="{issue_body_sha}">\n'
            f"{issue_body.decode()}\n<END-CANONICAL-ISSUE-212-BODY>"
        ).encode(),
        "owner-bootstrap.txt": b"Direct owner source bootstrap fixture.\n",
        "owner-210.txt": b"Direct owner source downstream fixture.\n",
        "owner-bootstrap-receipt.txt": b"Kilo transcription receipt; not owner-authored.\n",
        "owner-210-receipt.txt": b"Kilo transcription receipt; not owner-authored.\n",
        "patch.diff": b"diff --git a/example.py b/example.py\n+packet review fixture\n",
        "source-00.md": b"line one\nline two\nline three\n",
        "external-kilo-source.txt": b"Pinned Kilo 7.8.8 source excerpt.\n",
    }
    rows: list[dict[str, object]] = []
    for name, data in contents.items():
        path = packet / name
        _ = path.write_bytes(data)
        _ = os.chmod(path, 0o444)
        kinds = {
            "owner-bootstrap.txt": "owner-source",
            "owner-210.txt": "owner-source",
            "owner-bootstrap-receipt.txt": "transcription-receipt",
            "owner-210-receipt.txt": "transcription-receipt",
            "patch.diff": "patch",
            "source-00.md": "git-source",
            "external-kilo-source.txt": "external-source",
            "index.md": "index",
        }
        kind = kinds[name]
        receipt_metadata = {
            "owner-bootstrap-receipt.txt": (
                "https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/212#issuecomment-17305",
                "2026-10-15T19:01:02Z",
            ),
            "owner-210-receipt.txt": (
                "https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/210#issuecomment-17258",
                "2026-10-15T17:09:48Z",
            ),
        }
        if kind == "external-source":
            revision = "3" * 40
            origin = f"https://github.com/Kilo-Org/kilocode/blob/{revision}/packages/kilo/src.ts"
        elif kind == "git-source":
            origin, revision = "tests/example.py", HEAD
        elif name in receipt_metadata:
            origin, revision = receipt_metadata[name]
        else:
            origin, revision = name, HEAD
        rows.append(_manifest_record(name, data, kind, origin, revision))
    _ = os.chmod(packet, 0o555)

    source_sha = _sha(contents["source-00.md"])
    manifest: dict[str, object] = {
        "schema_version": 1,
        "synthetic_fixture": True,
        "repository": {
            "id": 15,
            "owner": "BioMedical-IT",
            "name": "scarcity-router",
            "url": "https://forgejo.creatidy.com/BioMedical-IT/scarcity-router",
        },
        "issue": {
            "id": 1683, "number": 212,
            "url": "https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/212",
            "body_sha256": issue_body_sha,
        },
        "pull_request": {
            "id": 927,
            "number": 213,
            "url": "https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/pulls/213",
            "head_branch": "issue-212-d069a-bootstrap",
            "base_branch": "develop",
            "head": HEAD,
            "base": BASE,
            "merge_base": MERGE_BASE,
            "body_sha256": "b" * 64,
        },
        "issue_state": "open",
        "pr_state": "open",
        "pr_merged": False,
        "head": HEAD,
        "base": BASE,
        "merge_base": MERGE_BASE,
        "currentness": {
            "checked_at": "2026-10-16T12:00:00Z",
            "repo_id": 15,
            "issue_id": 1683,
            "pr_id": 927,
            "head": HEAD,
            "base": BASE,
            "merge_base": MERGE_BASE,
        },
        "changed_paths": ["tests/example.py"],
        "source_bindings": {
            "tests/example.py": {
                "packet_path": "source-00.md",
                "base_exists": True,
                "base_blob": "4" * 40,
                "head_blob": "5" * 40,
                "sha256": source_sha,
                "ranges": [[1, 3]],
            },
        },
        "owner_sources": [
            {"packet_path": "owner-bootstrap.txt", "sha256": _sha(contents["owner-bootstrap.txt"]),
             "source_origin": "direct owner response", "decision_type": "bootstrap",
             "transcription_receipt": {
                 "packet_path": "owner-bootstrap-receipt.txt", "comment_id": 17305,
                 "issue_number": 212, "author": "kilo", "created_at": "2026-10-15T19:01:02Z",
                 "url": "https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/212#issuecomment-17305",
                 "sha256": _sha(contents["owner-bootstrap-receipt.txt"]),
             }},
            {"packet_path": "owner-210.txt", "sha256": _sha(contents["owner-210.txt"]),
             "source_origin": "direct owner response", "decision_type": "downstream",
             "transcription_receipt": {
                 "packet_path": "owner-210-receipt.txt", "comment_id": 17258,
                 "issue_number": 210, "author": "kilo", "created_at": "2026-10-15T17:09:48Z",
                 "url": "https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/210#issuecomment-17258",
                 "sha256": _sha(contents["owner-210-receipt.txt"]),
             }},
        ],
        "external_sources": [
            {"packet_path": "external-kilo-source.txt", "url": "https://github.com/Kilo-Org/kilocode/blob/" + "3" * 40 + "/packages/kilo/src.ts",
             "repository": "Kilo-Org/kilocode", "revision": "3" * 40,
             "source_path": "packages/kilo/src.ts", "blob_sha1": "6" * 40,
             "start_line": 1, "end_line": 1,
             "excerpt_sha256": _sha(contents["external-kilo-source.txt"])},
        ],
        "review_coverage": {
            "full_read_paths": full_read_paths,
            "source_ranges": {},
        },
        "previous_review_findings_included": False,
        "pr_body_content_included": False,
        "files": rows,
    }
    manifest_path = parent / "manifest.json"
    _ = manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    _ = os.chmod(manifest_path, 0o600)
    return packet, manifest_path


def _make_standalone_preflight_packet(
    parent: Path,
    *,
    changed_paths: tuple[str, ...],
    head: str,
    base: str,
    merge_base: str,
) -> tuple[Path, Path]:
    """Make a harmless packet with the complete production path inventory."""
    kilo_sources = {
        "kilo-LICENSE.txt": "LICENSE",
        "kilo-agent-config-schema.ts.txt": "packages/core/src/v1/config/agent.ts",
        "kilo-config-directory-files.ts.txt": "packages/opencode/src/kilocode/config/config.ts",
        "kilo-config-load-order.ts.txt": "packages/opencode/src/config/config.ts",
        "kilo-config-merge.ts.txt": "packages/opencode/src/config/config.ts",
        "kilo-permission-resolution.ts.txt": "packages/opencode/src/permission/index.ts",
        "kilo-read-path.ts.txt": "packages/opencode/src/tool/read.ts",
        "kilo-task-inheritance.ts.txt": "packages/opencode/src/kilocode/tool/task.ts",
        "kilo-task-parent.ts.txt": "packages/opencode/src/tool/task.ts",
        "kilo-cli-run-agent-selection.ts.txt": "packages/opencode/src/cli/cmd/run.ts",
        "kilo-cli-json-events.ts.txt": "packages/opencode/src/cli/cmd/run.ts",
        "kilo-cli-session-export.ts.txt": "packages/opencode/src/cli/cmd/export.ts",
    }
    owner_names = OWNER_EVIDENCE_PACKET_PATHS
    if tuple(sorted(set(changed_paths))) != changed_paths or not changed_paths:
        raise ValueError("synthetic preflight changed paths must be sorted and unique")
    if len(head) != 40 or len(base) != 40 or len(merge_base) != 40:
        raise ValueError("synthetic preflight refs must be SHA-1 sized")

    packet = parent / "packet"
    packet.mkdir()
    body = b"Synthetic acceptance only; no source or prior review findings."
    body_sha = _sha(body)
    source_names = tuple(f"source-{index:02d}.md" for index in range(len(changed_paths)))
    if set(kilo_sources) != set(KILO_SOURCE_PACKET_PATHS):
        raise AssertionError("synthetic Kilo file plan differs from production review plan")
    paths = standalone_packet_path_plan(changed_paths)
    read_plan: dict[str, object] = {
        "full_read_paths": [f"packet/{path}" for path in paths],
        "source_ranges": {},
    }
    contents: dict[str, bytes] = {
        name: f"SYNTHETIC packet content only: {name}\n".encode()
        for name in paths if name != "index.md"
    }
    contents["owner-bootstrap-receipt.txt"] = b"Kilo transcription receipt; not owner-authored.\n"
    contents["owner-210-receipt.txt"] = b"Kilo transcription receipt; not owner-authored.\n"
    contents["patch.diff"] = b"diff --git a/synthetic b/synthetic\n+synthetic-only patch\n"
    for index_name, source_path in kilo_sources.items():
        contents[index_name] = f"SYNTHETIC pinned Kilo evidence: {source_path}\n".encode()
    for index, changed_path in enumerate(changed_paths):
        contents[f"source-{index:02d}.md"] = f"SYNTHETIC changed file: {changed_path}\n".encode()
    index_body = (
        "All remaining files are harmless synthetic permission probes.\n"
        "PR body text is non-authoritative; digest only.\n"
        "<BEGIN-PACKET-READ-PLAN>\n"
        f"{json.dumps(read_plan, sort_keys=True)}\n"
        "<END-PACKET-READ-PLAN>\n"
        f'<BEGIN-CANONICAL-ISSUE-212-BODY sha256="{body_sha}">\n'
        f"{body.decode()}\n<END-CANONICAL-ISSUE-212-BODY>"
    )
    contents["index.md"] = index_body.encode()

    rows: list[dict[str, object]] = []
    receipt_rows = {
        "owner-bootstrap-receipt.txt": (
            "https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/212#issuecomment-17305",
            "2026-10-15T19:01:02Z",
        ),
        "owner-210-receipt.txt": (
            "https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/210#issuecomment-17258",
            "2026-10-15T17:09:48Z",
        ),
    }
    for name, data in contents.items():
        path = packet / name
        _ = path.write_bytes(data)
        _ = os.chmod(path, 0o444)
        if name == "index.md":
            kind, origin, revision = "index", name, head
        elif name == "patch.diff":
            kind, origin, revision = "patch", "synthetic PR patch", head
        elif name in receipt_rows:
            kind = "transcription-receipt"
            origin, revision = receipt_rows[name]
        elif name in owner_names:
            kind, origin, revision = "owner-source", "synthetic owner decision", "2026-10-15T19:00:00Z"
        elif name in kilo_sources:
            kind = "external-source"
            revision = "e2887f08e5a6394299c810ba508f358aaf4c5bc3"
            origin = f"https://github.com/Kilo-Org/kilocode/blob/{revision}/{kilo_sources[name]}"
        else:
            kind, origin, revision = "git-source", changed_paths[source_names.index(name)], head
        rows.append(_manifest_record(name, data, kind, origin, revision))
    _ = os.chmod(packet, 0o555)

    owner_records: list[dict[str, object]] = []
    for source, receipt, issue_number, comment_id, timestamp in (
        ("owner-bootstrap.txt", "owner-bootstrap-receipt.txt", 212, 17305, "2026-10-15T19:01:02Z"),
        ("owner-210.txt", "owner-210-receipt.txt", 210, 17258, "2026-10-15T17:09:48Z"),
    ):
        receipt_url = (
            f"https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/"
            f"{issue_number}#issuecomment-{comment_id}"
        )
        owner_records.append({
            "packet_path": source,
            "sha256": _sha(contents[source]),
            "source_origin": "synthetic direct owner source placeholder",
            "decision_type": "synthetic-only permission preflight context",
            "transcription_receipt": {
                "packet_path": receipt,
                "comment_id": comment_id,
                "issue_number": issue_number,
                "author": "kilo",
                "created_at": timestamp,
                "url": receipt_url,
                "sha256": _sha(contents[receipt]),
            },
        })
    external_records: list[dict[str, object]] = []
    for packet_path, source_path in kilo_sources.items():
        revision = "e2887f08e5a6394299c810ba508f358aaf4c5bc3"
        external_records.append({
            "packet_path": packet_path,
            "repository": "Kilo-Org/kilocode",
            "revision": revision,
            "source_path": source_path,
            "url": f"https://github.com/Kilo-Org/kilocode/blob/{revision}/{source_path}",
            "blob_sha1": hashlib.sha1(source_path.encode()).hexdigest(),
            "start_line": 1,
            "end_line": 1,
            "excerpt_sha256": _sha(contents[packet_path]),
        })
    source_bindings = {
        path: {
            "packet_path": f"source-{index:02d}.md",
            "base_exists": False,
            "base_blob": None,
            "head_blob": hashlib.sha1(f"head:{path}".encode()).hexdigest(),
            "sha256": _sha(contents[f"source-{index:02d}.md"]),
            "ranges": [[1, 1]],
        }
        for index, path in enumerate(changed_paths)
    }
    manifest: dict[str, object] = {
        "schema_version": 1,
        "synthetic_fixture": True,
        "repository": {
            "id": 15,
            "owner": "BioMedical-IT",
            "name": "scarcity-router",
            "url": "https://forgejo.creatidy.com/BioMedical-IT/scarcity-router",
        },
        "issue": {
            "id": 1683,
            "number": 212,
            "url": "https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/212",
            "body_sha256": body_sha,
        },
        "pull_request": {
            "id": 927,
            "number": 213,
            "url": "https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/pulls/213",
            "head_branch": "issue-212-d069a-bootstrap",
            "base_branch": "develop",
            "head": head,
            "base": base,
            "merge_base": merge_base,
            "body_sha256": "b" * 64,
        },
        "issue_state": "open",
        "pr_state": "open",
        "pr_merged": False,
        "head": head,
        "base": base,
        "merge_base": merge_base,
        "currentness": {
            "checked_at": "2026-10-17T22:15:33Z",
            "repo_id": 15,
            "issue_id": 1683,
            "pr_id": 927,
            "head": head,
            "base": base,
            "merge_base": merge_base,
        },
        "changed_paths": list(changed_paths),
        "source_bindings": source_bindings,
        "owner_sources": owner_records,
        "external_sources": external_records,
        "review_coverage": {
            "full_read_paths": list(paths),
            "source_ranges": {},
        },
        "previous_review_findings_included": False,
        "pr_body_content_included": False,
        "files": rows,
    }
    manifest_path = parent / "manifest.json"
    _ = manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    _ = os.chmod(manifest_path, 0o600)
    return packet, manifest_path


class ReviewerPacketTests(unittest.TestCase):
    def test_validate_complete_immutable_packet_and_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            packet, manifest = _make_packet(Path(temporary))
            result = validate_packet(packet, manifest, require_external_sources=True)
        self.assertEqual(result.head, HEAD)
        self.assertEqual(result.changed_paths, ("tests/example.py",))
        self.assertEqual(result.full_read_paths, (
            "external-kilo-source.txt", "index.md", "owner-210-receipt.txt", "owner-210.txt",
            "owner-bootstrap-receipt.txt", "owner-bootstrap.txt", "patch.diff",
            "source-00.md",
        ))
        self.assertEqual(result.review_ranges, {})
        self.assertEqual(result.external_source_count, 1)

    def test_validate_cli_accepts_optional_profile_source_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch(
                "tools.reviewer_packet.validate_packet",
                side_effect=RuntimeError("validator invoked"),
            ) as validate:
                with self.assertRaisesRegex(RuntimeError, "validator invoked"):
                    _ = main([
                        "validate", "--packet", str(root / "packet"),
                        "--manifest", str(root / "manifest.json"),
                        "--source-root", str(root),
                        "--profile-source-root", str(root),
                    ])
            validate.assert_called_once()
            self.assertEqual(validate.call_args.kwargs["profile_source_root"], root)

    def test_new_changed_file_binds_absent_base_blob(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            evidence = root / "evidence"
            _ = repository.mkdir()
            _ = evidence.mkdir()
            _ = subprocess.run(
                ["git", "init", "--initial-branch=main"], cwd=repository,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=True, text=True,
            )
            _ = (repository / "README.txt").write_text("base\n", encoding="utf-8")
            _ = _git(repository, "add", "README.txt")
            _ = subprocess.run(
                ["git", "-c", "user.name=Packet Test", "-c", "user.email=packet@example.invalid",
                 "commit", "--quiet", "-m", "base"],
                cwd=repository, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=True, text=True,
            )
            base = _git(repository, "rev-parse", "HEAD")
            _ = (repository / "tests").mkdir()
            source_bytes = b"line one\nline two\nline three\n"
            _ = (repository / "tests/example.py").write_bytes(source_bytes)
            _ = _git(repository, "add", "tests/example.py")
            _ = subprocess.run(
                ["git", "-c", "user.name=Packet Test", "-c", "user.email=packet@example.invalid",
                 "commit", "--quiet", "-m", "add source"],
                cwd=repository, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=True, text=True,
            )
            head = _git(repository, "rev-parse", "HEAD")
            packet, manifest_path = _make_packet(evidence)
            manifest = _load_object(manifest_path.read_text(encoding="utf-8"))
            manifest["base"] = base
            manifest["head"] = head
            manifest["merge_base"] = base
            pull_request = cast(dict[str, object], manifest["pull_request"])
            pull_request["base"] = base
            pull_request["head"] = head
            pull_request["merge_base"] = base
            currentness = cast(dict[str, object], manifest["currentness"])
            currentness["base"] = base
            currentness["head"] = head
            currentness["merge_base"] = base
            binding = cast(dict[str, object], cast(dict[str, object], manifest["source_bindings"])["tests/example.py"])
            binding["base_exists"] = False
            binding["base_blob"] = None
            binding["head_blob"] = _git(repository, "rev-parse", f"{head}:tests/example.py")
            binding["sha256"] = _sha(source_bytes)
            source_record = next(
                row for row in cast(list[dict[str, object]], manifest["files"])
                if row["path"] == "source-00.md"
            )
            source_record["revision"] = head
            patch_record = next(
                row for row in cast(list[dict[str, object]], manifest["files"])
                if row["path"] == "patch.diff"
            )
            patch_record["revision"] = head
            _ = manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

            canonical_patch = subprocess.run(
                [
                    "git", "diff", "--binary", "--full-index", "--no-ext-diff", "--unified=80",
                    f"{base}...{head}", "--", "tests/example.py",
                ],
                cwd=repository, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=True,
            ).stdout
            _rewrite_packet_file(packet, manifest_path, "patch.diff", canonical_patch)

            result = validate_packet(packet, manifest_path, source_root=repository)
            _rewrite_packet_file(packet, manifest_path, "patch.diff", canonical_patch + b"\n# forged")
            with self.assertRaisesRegex(PacketError, "canonical frozen Git diff"):
                _ = _validate_packet(
                    packet, manifest_path, source_root=repository, allow_synthetic_fixture=True
                )

        self.assertEqual(result.head, head)
        self.assertEqual(result.base, base)

    def test_canonical_issue_body_bytes_must_match_the_bound_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packet, manifest_path = _make_packet(root)
            _ = os.chmod(packet, 0o755)
            index_path = packet / "index.md"
            _ = os.chmod(index_path, 0o644)
            changed = index_path.read_bytes().replace(b"complete accepted", b"changed accepted")
            _ = index_path.write_bytes(changed)
            _ = os.chmod(index_path, 0o444)
            _ = os.chmod(packet, 0o555)
            manifest = _load_object(manifest_path.read_text(encoding="utf-8"))
            for row in cast(list[dict[str, object]], manifest["files"]):
                if row["path"] == "index.md":
                    row["size"] = len(changed)
                    row["sha256"] = _sha(changed)
            _ = manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(PacketError, "canonical issue body differs"):
                _ = validate_packet(packet, manifest_path)

    def test_index_rejects_duplicate_closer_and_unbound_trailing_content(self) -> None:
        end_marker = b"<END-CANONICAL-ISSUE-212-BODY>"
        for suffix in (end_marker, b"\nPrior review findings must not be present."):
            with self.subTest(suffix=suffix[:8]), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                packet, manifest_path = _make_packet(root)
                index_path = packet / "index.md"
                mutated = index_path.read_bytes() + suffix
                _rewrite_packet_file(packet, manifest_path, "index.md", mutated)
                with self.assertRaisesRegex(
                    PacketError, "canonical issue body markers are malformed or duplicated|unbound trailing content"
                ):
                    _ = validate_packet(packet, manifest_path)

    def test_review_coverage_requires_full_reads_of_changed_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            packet, manifest_path = _make_packet(Path(temporary))
            manifest = _load_object(manifest_path.read_text(encoding="utf-8"))
            coverage = cast(dict[str, object], manifest["review_coverage"])
            source_ranges = cast(dict[str, object], coverage["source_ranges"])
            source_ranges["source-00.md"] = [[1, 3]]
            full_read_paths = cast(list[str], coverage["full_read_paths"])
            full_read_paths.remove("source-00.md")
            _ = manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

            index_path = packet / "index.md"
            index = index_path.read_text(encoding="utf-8")
            start = index.index("<BEGIN-PACKET-READ-PLAN>\n") + len("<BEGIN-PACKET-READ-PLAN>\n")
            end = index.index("\n<END-PACKET-READ-PLAN>", start)
            read_plan = _load_object(index[start:end])
            read_ranges = cast(dict[str, object], read_plan["source_ranges"])
            read_ranges["packet/source-00.md"] = [[1, 3]]
            packet_full_paths = cast(list[str], read_plan["full_read_paths"])
            packet_full_paths.remove("packet/source-00.md")
            mutated = (
                index[:start]
                + json.dumps(read_plan, sort_keys=True)
                + index[end:]
            ).encode()
            _rewrite_packet_file(packet, manifest_path, "index.md", mutated)

            with self.assertRaisesRegex(PacketError, "all changed source files must be covered by full reads"):
                _ = validate_packet(packet, manifest_path)

    def test_index_read_plan_must_match_manifest_full_paths_and_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packet, manifest_path = _make_packet(root)
            _ = os.chmod(packet, 0o755)
            index_path = packet / "index.md"
            _ = os.chmod(index_path, 0o644)
            changed = index_path.read_bytes().replace(
                b"packet/owner-bootstrap.txt", b"packet/not-indexed.txt", 1
            )
            _ = index_path.write_bytes(changed)
            _ = os.chmod(index_path, 0o444)
            _ = os.chmod(packet, 0o555)
            manifest = _load_object(manifest_path.read_text(encoding="utf-8"))
            for row in cast(list[dict[str, object]], manifest["files"]):
                if row["path"] == "index.md":
                    row["size"] = len(changed)
                    row["sha256"] = _sha(changed)
            _ = manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(PacketError, "index Read plan differs"):
                _ = validate_packet(packet, manifest_path)

    def test_owner_receipt_and_external_origin_tampering_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packet, manifest_path = _make_packet(root)
            manifest = _load_object(manifest_path.read_text(encoding="utf-8"))
            owners = cast(list[dict[str, object]], manifest["owner_sources"])
            receipt = cast(dict[str, object], owners[0]["transcription_receipt"])
            receipt["author"] = "adrian.tkacz"
            _ = manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(PacketError, "receipt metadata mismatch"):
                _ = validate_packet(packet, manifest_path)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packet, manifest_path = _make_packet(root)
            manifest = _load_object(manifest_path.read_text(encoding="utf-8"))
            external_sources = cast(list[dict[str, object]], manifest["external_sources"])
            external_sources[0]["url"] = "https://example.invalid/forged-source"
            _ = manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(PacketError, "URL/revision"):
                _ = validate_packet(packet, manifest_path)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packet, manifest_path = _make_packet(root)
            manifest = _load_object(manifest_path.read_text(encoding="utf-8"))
            external_sources = cast(list[dict[str, object]], manifest["external_sources"])
            external_sources[0]["source_path"] = "../outside.ts"
            _ = manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(PacketError, "normalized relative Git path"):
                _ = validate_packet(packet, manifest_path)

    def test_production_validation_requires_complete_pinned_external_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            packet, manifest_path = _make_packet(Path(temporary))
            manifest = _load_object(manifest_path.read_text(encoding="utf-8"))
            manifest["synthetic_fixture"] = False
            _ = manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            with self.assertRaisesRegex(PacketError, "requires pinned external-source checkouts"):
                _ = _validate_packet(packet, manifest_path, source_root=ROOT)

        with tempfile.TemporaryDirectory() as temporary:
            packet, manifest_path = _make_packet(Path(temporary))
            manifest = _load_object(manifest_path.read_text(encoding="utf-8"))
            manifest["synthetic_fixture"] = False
            _ = manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            roots = {"Kilo-Org/kilocode": Path("/tmp/kilo/kilocode-v7.8.8")}
            with self.assertRaisesRegex(PacketError, "complete pinned Kilo source set"):
                _ = _validate_packet(packet, manifest_path, source_root=ROOT, external_source_roots=roots)

        with tempfile.TemporaryDirectory() as temporary:
            changed = (".kilo/agents/pr-reviewer-standalone.md",)
            packet, manifest_path = _make_standalone_preflight_packet(
                Path(temporary), changed_paths=changed, head=HEAD, base=BASE, merge_base=MERGE_BASE
            )
            manifest = _load_object(manifest_path.read_text(encoding="utf-8"))
            manifest["synthetic_fixture"] = False
            external = cast(list[dict[str, object]], manifest["external_sources"])
            external[0]["source_path"] = "packages/not-the-pinned-source.ts"
            _ = manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            roots = {"Kilo-Org/kilocode": Path("/tmp/kilo/kilocode-v7.8.8")}
            with self.assertRaisesRegex(PacketError, "complete pinned Kilo mapping"):
                _ = _validate_packet(packet, manifest_path, source_root=ROOT, external_source_roots=roots)

    def test_missing_malformed_and_extra_packet_files_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packet, manifest = _make_packet(root)
            with self.assertRaises(PacketError):
                _ = validate_packet(packet / "missing", manifest)
            _ = manifest.write_text("{not-json", encoding="utf-8")
            with self.assertRaises(PacketError):
                _ = validate_packet(packet, manifest)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packet, manifest = _make_packet(root)
            _ = os.chmod(packet, 0o755)
            _ = (packet / "unexpected.txt").write_text("not indexed", encoding="utf-8")
            _ = os.chmod(packet / "unexpected.txt", 0o444)
            _ = os.chmod(packet, 0o555)
            with self.assertRaises(PacketError):
                _ = validate_packet(packet, manifest)

    def test_incomplete_changed_file_coverage_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            packet, manifest_path = _make_packet(Path(temporary))
            manifest = _load_object(manifest_path.read_text(encoding="utf-8"))
            manifest["source_bindings"] = {}
            _ = manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(PacketError, "source binding map"):
                _ = validate_packet(packet, manifest_path)

    def test_symlink_hardlink_and_secret_candidates_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packet, manifest = _make_packet(root)
            _ = os.chmod(packet, 0o755)
            _ = (packet / "sibling-link.txt").symlink_to(packet / "index.md")
            _ = os.chmod(packet, 0o555)
            with self.assertRaises(PacketError):
                _ = validate_packet(packet, manifest)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packet, manifest = _make_packet(root)
            _ = os.chmod(packet, 0o755)
            target = packet / "owner-bootstrap.txt"
            _ = os.link(target, packet / "hardlink.txt")
            _ = os.chmod(packet, 0o555)
            with self.assertRaises(PacketError):
                _ = validate_packet(packet, manifest)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packet, manifest_path = _make_packet(root)
            _ = os.chmod(packet, 0o755)
            secret = packet / "owner-bootstrap.txt"
            _ = os.chmod(secret, 0o644)
            synthetic_key = b"AKIA" + b"1234567890ABCDEF"
            _ = secret.write_bytes(synthetic_key)
            _ = os.chmod(secret, 0o444)
            manifest = _load_object(manifest_path.read_text(encoding="utf-8"))
            for row in cast(list[dict[str, object]], manifest["files"]):
                if row["path"] == "owner-bootstrap.txt":
                    row["size"] = secret.stat().st_size
                    row["sha256"] = _sha(secret.read_bytes())
            cast(list[dict[str, object]], manifest["owner_sources"])[0]["sha256"] = _sha(secret.read_bytes())
            _ = manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            _ = os.chmod(packet, 0o555)
            with self.assertRaisesRegex(PacketError, "credential-like"):
                _ = validate_packet(packet, manifest_path)

    def test_credential_scanner_rejects_gitlab_slack_and_authorization_tokens(self) -> None:
        candidates = (
            ("gitlab", ("glpat-" + "synthetic000000000000000000000").encode()),
            ("slack", ("xoxb-" + "123456789012-123456789012-123456789012-123456789012").encode()),
            ("authorization", ("Authorization: token " + "synthetic_auth_token_12345678901234567890").encode()),
        )
        for label, candidate in candidates:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                packet, manifest = _make_packet(Path(temporary))
                _rewrite_packet_file(packet, manifest, "owner-bootstrap.txt", candidate + b"\n")
                with self.assertRaisesRegex(PacketError, "credential-like"):
                    _ = validate_packet(packet, manifest)

        with tempfile.TemporaryDirectory() as temporary:
            packet, manifest = _make_packet(Path(temporary))
            _rewrite_packet_file(
                packet, manifest, "owner-bootstrap.txt", b"Authorization: token [redacted]\n"
            )
            _ = validate_packet(packet, manifest)

    def test_per_run_profile_allows_only_exact_read_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            packet = workspace / "packet"
            packet.mkdir(parents=True)
            for filename in ("index.md", "source-00.md"):
                _ = (packet / filename).write_text("synthetic\n", encoding="utf-8")
            reviewer = make_per_run_permission_map(
                ["index.md", "source-00.md"], workspace=workspace
            )
            worktree_root = Path(workspace.resolve().anchor)
            index_pattern = str((packet / "index.md").resolve().relative_to(worktree_root))
            source_pattern = str((packet / "source-00.md").resolve().relative_to(worktree_root))
        self.assertEqual(reviewer["*"], "deny")
        self.assertEqual(reviewer["read"], {
            "*": "deny",
            index_pattern: "allow",
            source_pattern: "allow",
        })
        self.assertEqual(cast(dict[str, str], reviewer["external_directory"])["*"], "deny")

    def test_synthetic_preflight_fixture_has_the_complete_standalone_packet_inventory(self) -> None:
        changed = (
            ".kilo/agents/pr-reviewer-standalone.md",
            "tests/test_reviewer_packet.py",
            "tools/reviewer_packet.py",
        )
        expected_paths = standalone_packet_path_plan(changed)
        with tempfile.TemporaryDirectory() as temporary:
            packet, manifest = _make_standalone_preflight_packet(
                Path(temporary), changed_paths=changed, head=HEAD, base=BASE, merge_base=MERGE_BASE
            )
            inspected = validate_packet(packet, manifest, require_external_sources=True)
            self.assertEqual(inspected.file_paths, expected_paths)
            self.assertEqual(inspected.full_read_paths, expected_paths)
            self.assertEqual(len(expected_paths), len(STANDALONE_PACKET_FIXED_PATHS) + len(changed))
            self.assertFalse(_load_object(manifest.read_text(encoding="utf-8"))[
                "previous_review_findings_included"
            ])

    def test_synthetic_packet_builder_uses_the_production_file_plan_without_real_content(self) -> None:
        changed = tuple(sorted((
            ".kilo/agents/pr-reviewer-standalone.md",
            "tests/test_reviewer_packet.py",
            "tools/reviewer_packet.py",
        )))
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            packet, manifest = create_synthetic_standalone_packet(
                parent, changed_paths=changed, head=HEAD, base=BASE, merge_base=MERGE_BASE
            )
            inspected = validate_packet(packet, manifest, require_external_sources=True)
            expected_paths = standalone_packet_path_plan(changed)
            self.assertEqual(inspected.file_paths, expected_paths)
            self.assertEqual(inspected.full_read_paths, inspected.file_paths)
            for path in packet.iterdir():
                text = path.read_text(encoding="utf-8")
                if path.name != "index.md":
                    self.assertIn("synthetic", text.lower())
            self.assertFalse(_load_object(manifest.read_text(encoding="utf-8"))[
                "pr_body_content_included"
            ])
            self.assertEqual(
                validate_synthetic_preflight_packet(packet, manifest).file_paths, expected_paths
            )

    def _prepare_standalone(
        self, root: Path, *, preflight: bool,
    ) -> tuple[Path, Path, RuntimeWorkspace]:
        packet, manifest = _make_packet(root)
        workspace_parent = root / "workspace-parent"
        overlay_parent = root / "overlay-parent"
        workspace_parent.mkdir()
        overlay_parent.mkdir()
        os.chmod(workspace_parent, 0o700)
        os.chmod(overlay_parent, 0o700)
        runtime = prepare_standalone_environment(
            packet, manifest, PROFILE, workspace_parent, overlay_parent,
            preflight=preflight, allow_synthetic_fixture=True,
        )
        return packet, manifest, runtime

    def _debug_standalone(self, runtime: RuntimeWorkspace) -> dict[str, object]:
        profile = PROFILE.read_text(encoding="utf-8")
        prompt = profile.split("---", 2)[2].lstrip("\n").rstrip()
        permission_map = cast(dict[str, str], make_per_run_permission_map(
            [Path(path).name for path in runtime.allowed_paths], workspace=runtime.workspace
        )["read"])
        rules = [
            {"permission": "*", "pattern": "*", "action": "allow"},
            {"permission": "*", "pattern": "*", "action": "deny"},
            {"permission": "read", "pattern": "*", "action": "deny"},
            *[{
                "permission": "read", "pattern": path, "action": action
            } for path, action in permission_map.items()],
            {"permission": "external_directory", "pattern": "*", "action": "deny"},
        ]
        return {
            "name": "pr-reviewer-standalone",
            "mode": "primary",
            "tools": {"read": True, "bash": False, "task": False, "mcp": False},
            "permission": rules,
            "prompt": prompt,
        }

    def _read_call(
        self, runtime: RuntimeWorkspace, path: str, *, denied: bool = False,
        line_numbers: set[int] | None = None, permission_pattern: str | None = None,
    ) -> dict[str, object]:
        if denied:
            return {
                "type": "tool",
                "tool": "read",
                "state": {
                    "status": "error",
                    "input": {"filePath": path},
                    "output": None,
                    "error": "Permission denied before content",
                    "metadata": {"approval": {
                        "agent": "pr-reviewer-standalone",
                        "rule": {"permission": "external_directory", "pattern": "*", "action": "deny"},
                    }},
                },
            }
        full_file_lines = _expected_line_map(runtime.packet / Path(path).name)
        file_lines = full_file_lines
        if line_numbers is not None:
            file_lines = {number: file_lines[number] for number in line_numbers}
        absolute_path = str((runtime.packet / Path(path).name).resolve())
        rendered = f"<path>{absolute_path}</path>\n<type>file</type>\n<content>\n" + "\n".join(
            f"{number}: {line}" for number, line in file_lines.items()
        ) + "\n</content>"
        return {
            "type": "tool",
            "tool": "read",
            "state": {
                "status": "completed",
                "input": {"filePath": path},
                "output": rendered,
                "metadata": {"approval": {
                    "agent": "pr-reviewer-standalone",
                    "rule": {
                        "permission": "read",
                        "pattern": permission_pattern or path,
                        "action": "allow",
                    },
                }, "loaded": [], "display": {
                    "type": "file",
                    "path": str((runtime.packet / Path(path).name).resolve()),
                    "text": "\n".join(file_lines.values()),
                    "lineStart": min(file_lines),
                    "lineEnd": max(file_lines),
                    "totalLines": len(full_file_lines),
                    "truncated": len(file_lines) != len(full_file_lines),
                }},
            },
        }

    def _standalone_session(
        self,
        runtime: RuntimeWorkspace,
        *,
        preflight: bool,
        short_preflight: bool = False,
        result_override: dict[str, object] | None = None,
    ) -> tuple[dict[str, object], dict[str, str], str, str]:
        expected_model = {"provider_id": "openai", "model_id": "gpt-6-luna", "variant": "max"}
        if short_preflight:
            title = "synthetic-standalone-relative-path-preflight"
        elif preflight:
            title = "synthetic-standalone-permission-preflight"
        else:
            title = f"review-issue-{runtime.inspection.issue_number}-pr-{runtime.inspection.pull_number}"
        prompt = (
            standalone_preflight_prompt(runtime, short=short_preflight)
            if preflight else standalone_review_prompt(runtime)
        )
        parts: list[dict[str, object]] = []
        allowed_paths = (
            ("packet/index.md", "packet/source-00.md")
            if preflight and short_preflight else runtime.allowed_paths
        )
        for path in allowed_paths:
            if preflight:
                lines = None
            else:
                filename = path.removeprefix("packet/")
                if filename in runtime.inspection.full_read_paths:
                    lines = None
                else:
                    line_numbers = {
                        number
                        for start, end in runtime.inspection.review_ranges[filename]
                        for number in range(start, end + 1)
                    }
                    lines = line_numbers
            native_path = path
            resolved_path = runtime.workspace / path
            permission_pattern = str(
                resolved_path.resolve().relative_to(Path(runtime.workspace.resolve().anchor))
            )
            parts.append(self._read_call(
                runtime, native_path, line_numbers=lines, permission_pattern=permission_pattern
            ))
        if preflight:
            parts.extend(self._read_call(runtime, path, denied=True) for path in runtime.denied_paths)
            final: dict[str, object] = {
                "preflight": "PASS",
                "allowed_paths": list(allowed_paths),
                "complete_read_count": len(allowed_paths),
                "probe_results": [
                    {"path": path, "result": "DENIED_BEFORE_CONTENT", "bytes_returned": False}
                    for path in runtime.denied_paths
                ],
                "unauthorized_bytes_returned": False,
            }
        else:
            final = {
                "reviewed_head": runtime.inspection.head,
                "reviewed_base": runtime.inspection.base,
                "reviewed_merge_base": runtime.inspection.merge_base,
                "verdict": "APPROVE",
                "findings": [],
                "limitations": [],
                "checks_run": [],
                "ux_impact": "none",
            }
        if result_override is not None:
            final = result_override
        session: dict[str, object] = {
            "info": {
                "id": "ses_standalone_fixture",
                "agent": "pr-reviewer-standalone",
                "parentID": None,
                "projectID": "global",
                "directory": str(runtime.workspace),
                "model": {"providerID": "openai", "id": "gpt-6-luna", "variant": "max"},
                "variant": None,
                "title": title,
                "permission": [],
            },
            "messages": [
                {"info": {"role": "user"}, "parts": [{"type": "text", "text": f'"{prompt}"'}]},
                {"info": {"role": "assistant", "agent": "pr-reviewer-standalone"}, "parts": parts},
                {"info": {"role": "assistant", "agent": "pr-reviewer-standalone", "finish": "stop"},
                 "parts": [{"type": "text", "text": json.dumps(final)}]},
            ],
        }
        return session, expected_model, title, prompt

    def test_standalone_primary_requires_exact_deny_by_default_runtime_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, runtime = self._prepare_standalone(root, preflight=True)
            resolved = self._debug_standalone(runtime)
            checked = verify_effective_standalone_agent(resolved, runtime, PROFILE)
            self.assertEqual(checked["reviewer_agent"], "pr-reviewer-standalone")
            self.assertEqual(checked["reviewer_profile_mode"], "primary")
            self.assertIn(checked["reviewer_resolved_mode"], {"primary", "all"})
            self.assertEqual(checked["reviewer_effective_tools"], ["read"])
            self.assertEqual(checked["allowed_packet_paths"], list(runtime.allowed_paths))
            permission_map = cast(dict[str, str], make_per_run_permission_map(
                [Path(path).name for path in runtime.allowed_paths], workspace=runtime.workspace
            )["read"])
            self.assertEqual(
                checked["allowed_read_permission_patterns"],
                sorted(path for path, action in permission_map.items()
                       if path != "*" and action == "allow"),
            )
            self.assertFalse(checked["os_level_isolation_claimed"])

            fallback = dict(resolved, name="code")
            with self.assertRaisesRegex(PacketError, "fallback"):
                _ = verify_effective_standalone_agent(fallback, runtime, PROFILE)

            subagent = dict(resolved, mode="subagent")
            with self.assertRaisesRegex(PacketError, "subagent and Kilo run would fall back"):
                _ = verify_effective_standalone_agent(subagent, runtime, PROFILE)

            resolved_tools = cast(dict[str, bool], resolved["tools"])
            reopened = dict(resolved)
            reopened["tools"] = {**resolved_tools, "bash": True}
            with self.assertRaisesRegex(PacketError, "effective tool set"):
                _ = verify_effective_standalone_agent(reopened, runtime, PROFILE)

            broad_read = dict(resolved)
            broad_read["permission"] = [
                *cast(list[dict[str, object]], resolved["permission"]),
                {"permission": "read", "pattern": "*", "action": "allow"},
            ]
            with self.assertRaisesRegex(PacketError, "exact rule"):
                _ = verify_effective_standalone_agent(broad_read, runtime, PROFILE)

            legacy = (ROOT / ".kilo/agents/pr-reviewer.md").read_text(encoding="utf-8")
            standalone = PROFILE.read_text(encoding="utf-8")
            self.assertIn("mode: subagent", legacy)
            self.assertIn("mode: primary", standalone)
            self.assertEqual(legacy.split("---", 2)[2], standalone.split("---", 2)[2])

    def test_standalone_run_args_select_fresh_primary_without_resume_or_task(self) -> None:
        argv = standalone_run_argv(
            Path("/opt/kilo"),
            {"provider_id": "openai", "model_id": "gpt-6-luna", "variant": "max"},
            "review-issue-212-pr-213",
            "Review only the exact packet.",
        )
        self.assertEqual(argv[:9], [
            "/opt/kilo", "--pure", "run", "--agent", "pr-reviewer-standalone",
            "--model", "openai/gpt-6-luna", "--variant", "max",
        ])
        self.assertIn("--format", argv)
        self.assertIn("json", argv)
        self.assertNotIn("--continue", argv)
        self.assertNotIn("--session", argv)
        self.assertNotIn("--fork", argv)
        self.assertFalse(any(arg == "task" or arg == "pr-reviewer" for arg in argv))

    def test_kilo_event_filter_returns_only_one_session_id(self) -> None:
        events = StringIO(
            '{"type":"step_start","sessionID":"ses_abc123"}\n' +
            '{"type":"tool_use","sessionID":"ses_abc123","part":{"state":{"output":"private packet bytes"}}}\n'
        )
        self.assertEqual(capture_session_id(events), "ses_abc123")
        with self.assertRaisesRegex(PacketError, "more than one session"):
            _ = capture_session_id(StringIO(
                '{"sessionID":"ses_abc123"}\n{"sessionID":"ses_other"}\n'
            ))
        with self.assertRaisesRegex(PacketError, "non-JSON event"):
            _ = capture_session_id(StringIO("warning before stream\n"))
        with self.assertRaisesRegex(PacketError, "omitted its session ID"):
            _ = capture_session_id(StringIO("{}\n"))

    def test_standalone_review_prompt_has_only_frozen_packet_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, _, runtime = self._prepare_standalone(Path(temporary), preflight=False)
            prompt = standalone_review_prompt(runtime)
            self.assertIn("packet/index.md", prompt)
            self.assertIn("exact relative Read `filePath` values", prompt)
            self.assertIn("resolves them against the session directory", prompt)
            for path in runtime.allowed_paths:
                self.assertIn(f"- `{path}`", prompt)
            self.assertIn(runtime.inspection.head, prompt)
            self.assertIn(runtime.inspection.base, prompt)
            self.assertIn(runtime.inspection.merge_base, prompt)
            self.assertNotIn(str(ROOT), prompt)
            self.assertNotIn(f"{runtime.packet}/", prompt)
            for historical in ("REQUEST_CHANGES", "prior findings", "r6", "r7", "parent conclusion"):
                self.assertNotIn(historical, prompt)

    def test_short_preflight_prompt_uses_two_exact_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, _, runtime = self._prepare_standalone(Path(temporary), preflight=True)
            prompt = standalone_preflight_prompt(runtime, short=True)
            self.assertIn("`packet/index.md`", prompt)
            self.assertIn("`packet/source-00.md`", prompt)
            self.assertNotIn("`packet/source-01.md`", prompt)
            self.assertIn("relative Read `filePath` values", prompt)
            self.assertNotIn(f"{runtime.packet}/", prompt)

    def test_standalone_workspace_contains_only_packet_and_external_canaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, runtime = self._prepare_standalone(root, preflight=True)
            self.assertEqual({entry.name for entry in runtime.workspace.iterdir()}, {"packet"})
            self.assertEqual({entry.name for entry in runtime.packet.iterdir()}, {
                Path(path).name for path in runtime.allowed_paths
            })
            self.assertEqual(os.stat(runtime.workspace).st_mode & 0o777, 0o555)
            self.assertEqual(os.stat(runtime.packet).st_mode & 0o777, 0o555)
            self.assertTrue(runtime.probe_root is not None)
            self.assertFalse(runtime.probe_root == runtime.workspace)
            self.assertEqual(set(runtime.probe_labels.values()), {
                "sibling", "parent", "absolute", "traversal",
                "symlink_to_packet", "symlink_to_outside",
            })
            self.assertEqual(_load_object(runtime.kilo_config_content), {})
            profile_copy = runtime.profile_config / "agents/pr-reviewer-standalone.md"
            profile_text = profile_copy.read_text(encoding="utf-8")
            frontmatter = profile_text.split("---", 2)[1]
            self.assertIn("mode: primary", frontmatter)
            self.assertIn('"*": deny', frontmatter)
            read_rules = cast(dict[str, str], make_per_run_permission_map(
                [Path(path).name for path in runtime.allowed_paths], workspace=runtime.workspace
            )["read"])
            for path, action in read_rules.items():
                if path != "*" and action == "allow":
                    self.assertIn(f'{json.dumps(path)}: allow', frontmatter)
            self.assertNotIn('"packet/index.md": allow', frontmatter)
            self.assertNotRegex(frontmatter, r'(?m)^\s*"\*":\s*allow\s*$')
            self.assertEqual(
                profile_text.split("---", 2)[2],
                PROFILE.read_text(encoding="utf-8").split("---", 2)[2],
            )

    def test_standalone_environment_prevents_daemon_config_reuse(self) -> None:
        env = standalone_environment(
            kilo_binary=Path("/opt/kilo"),
            profile_config=Path("/tmp/kilo-config"),
            kilo_config_content="{}",
            home=Path("/tmp/home"),
            tmpdir=Path("/tmp/tmp"),
            config_home=Path("/tmp/config"),
            data_home=Path("/tmp/data"),
            state_home=Path("/tmp/state"),
            cache_home=Path("/tmp/cache"),
        )
        self.assertEqual(env["KILO_NO_DAEMON"], "1")

    def test_standalone_preflight_trace_requires_complete_reads_and_actual_denials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, _, runtime = self._prepare_standalone(Path(temporary), preflight=True)
            session, expected_model, title, prompt = self._standalone_session(runtime, preflight=True)
            result = audit_standalone_session(
                session,
                session_export_sha256="a" * 64,
                runtime=runtime,
                expected_model=expected_model,
                expected_title=title,
                expected_prompt=prompt,
                preflight=True,
            )
            self.assertEqual(result["status"], "STANDALONE_PREFLIGHT_TRACE_PASS")
            self.assertEqual(result["reviewer_agent"], "pr-reviewer-standalone")
            self.assertEqual(result["reviewer_parent_session"], None)
            self.assertEqual(result["reviewer_tools"], ["read"])
            self.assertEqual(result["allowed_file_count"], len(runtime.allowed_paths))
            self.assertEqual(result["denied_before_content_count"], len(runtime.denied_paths))
            self.assertEqual(result["preflight_result"]["preflight"], "PASS")

    def test_short_preflight_audits_relative_reads_and_every_escape_denial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, _, runtime = self._prepare_standalone(Path(temporary), preflight=True)
            session, expected_model, title, prompt = self._standalone_session(
                runtime, preflight=True, short_preflight=True
            )
            result = audit_standalone_session(
                session,
                session_export_sha256="a" * 64,
                runtime=runtime,
                expected_model=expected_model,
                expected_title=title,
                expected_prompt=prompt,
                preflight=True,
                short_preflight=True,
            )
            self.assertEqual(result["status"], "STANDALONE_PREFLIGHT_TRACE_PASS")
            self.assertTrue(result["short_preflight"])
            self.assertEqual(result["allowed_file_count"], 2)
            self.assertEqual(result["denied_before_content_count"], len(runtime.denied_paths))
            read_trace = cast(list[dict[str, object]], result["read_trace"])
            self.assertEqual(
                [call["requested_path"] for call in read_trace[:2]],
                ["packet/index.md", "packet/source-00.md"],
            )
            self.assertEqual(
                [call["status"] for call in read_trace[2:]],
                ["denied_before_content"] * len(runtime.denied_paths),
            )

    def test_preflight_accepts_identical_authorized_repeated_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, _, runtime = self._prepare_standalone(Path(temporary), preflight=True)
            session, expected_model, title, prompt = self._standalone_session(
                runtime, preflight=True, short_preflight=True
            )
            messages = cast(list[dict[str, object]], session["messages"])
            calls = cast(list[dict[str, object]], messages[1]["parts"])
            calls.insert(1, cast(dict[str, object], json.loads(json.dumps(calls[0]))))
            result = audit_standalone_session(
                session,
                session_export_sha256="a" * 64,
                runtime=runtime,
                expected_model=expected_model,
                expected_title=title,
                expected_prompt=prompt,
                preflight=True,
                short_preflight=True,
            )
            read_trace = cast(list[dict[str, object]], result["read_trace"])
            self.assertEqual(result["status"], "STANDALONE_PREFLIGHT_TRACE_PASS")
            self.assertEqual(read_trace[1]["path"], "packet/index.md")
            self.assertEqual(
                read_trace[1]["overlap_line_count"],
                len(_expected_line_map(runtime.packet / "index.md")),
            )
            self.assertEqual(
                result["repeated_read_line_count"],
                len(_expected_line_map(runtime.packet / "index.md")),
            )

    def test_preflight_rejects_repeated_read_with_changed_packet_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, _, runtime = self._prepare_standalone(Path(temporary), preflight=True)
            session, expected_model, title, prompt = self._standalone_session(
                runtime, preflight=True, short_preflight=True
            )
            messages = cast(list[dict[str, object]], session["messages"])
            calls = cast(list[dict[str, object]], messages[1]["parts"])
            duplicate = cast(dict[str, object], json.loads(json.dumps(calls[0])))
            state = cast(dict[str, object], duplicate["state"])
            index_text = cast(str, state["output"])
            first_line = _expected_line_map(runtime.packet / "index.md")[1]
            state["output"] = index_text.replace(f"1: {first_line}", "1: forged packet line", 1)
            metadata = cast(dict[str, object], state["metadata"])
            display = cast(dict[str, object], metadata["display"])
            display["text"] = cast(str, display["text"]).replace(first_line, "forged packet line", 1)
            calls.insert(1, duplicate)
            with self.assertRaisesRegex(PacketError, "Read bytes differ from immutable packet"):
                _ = audit_standalone_session(
                    session,
                    session_export_sha256="a" * 64,
                    runtime=runtime,
                    expected_model=expected_model,
                    expected_title=title,
                    expected_prompt=prompt,
                    preflight=True,
                    short_preflight=True,
                )

    def test_standalone_audit_rejects_absolute_paths_and_preflight_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, _, runtime = self._prepare_standalone(Path(temporary), preflight=False)
            session, expected_model, title, prompt = self._standalone_session(runtime, preflight=False)
            messages = cast(list[dict[str, object]], session["messages"])
            parts = cast(list[dict[str, object]], messages[1]["parts"])
            state = cast(dict[str, object], parts[0]["state"])
            request = cast(dict[str, object], state["input"])
            request["filePath"] = str(runtime.packet / "index.md")
            with self.assertRaisesRegex(PacketError, "unlisted Read path"):
                _ = audit_standalone_session(
                    session,
                    session_export_sha256="a" * 64,
                    runtime=runtime,
                    expected_model=expected_model,
                    expected_title=title,
                    expected_prompt=prompt,
                )

            preflight_root = Path(temporary) / "preflight"
            preflight_root.mkdir()
            _, _, runtime = self._prepare_standalone(preflight_root, preflight=True)
            session, expected_model, title, prompt = self._standalone_session(
                runtime, preflight=True
            )
            extra_prompt = cast(dict[str, object], json.loads(json.dumps(session)))
            extra_messages = cast(list[dict[str, object]], extra_prompt["messages"])
            user_parts = cast(list[dict[str, object]], extra_messages[0]["parts"])
            user_parts[0]["text"] = f'"{prompt} extra"'
            with self.assertRaisesRegex(PacketError, "unapproved or prior conversation context"):
                _ = audit_standalone_session(
                    extra_prompt,
                    session_export_sha256="a" * 64,
                    runtime=runtime,
                    expected_model=expected_model,
                    expected_title=title,
                    expected_prompt=prompt,
                    preflight=True,
                )

            leaked = cast(dict[str, object], json.loads(json.dumps(session)))
            messages = cast(list[dict[str, object]], leaked["messages"])
            calls = cast(list[dict[str, object]], messages[1]["parts"])
            denied = calls[len(runtime.allowed_paths)]
            denied_state = cast(dict[str, object], denied["state"])
            canary = next(
                Path(path).read_text(encoding="utf-8")
                for path in runtime.probe_hashes
                if "sibling" in Path(path).name
            )
            denied_state["output"] = f"<content>\n{canary}</content>"
            with self.assertRaisesRegex(PacketError, "canary bytes"):
                _ = audit_standalone_session(
                    leaked,
                    session_export_sha256="a" * 64,
                    runtime=runtime,
                    expected_model=expected_model,
                    expected_title=title,
                    expected_prompt=prompt,
                    preflight=True,
                )

            incomplete = cast(dict[str, object], json.loads(json.dumps(session)))
            incomplete_messages = cast(list[dict[str, object]], incomplete["messages"])
            incomplete_calls = cast(list[dict[str, object]], incomplete_messages[1]["parts"])
            del incomplete_calls[1]
            with self.assertRaisesRegex(PacketError, "read/probe order|complete packet"):
                _ = audit_standalone_session(
                    incomplete,
                    session_export_sha256="a" * 64,
                    runtime=runtime,
                    expected_model=expected_model,
                    expected_title=title,
                    expected_prompt=prompt,
                    preflight=True,
                )

            wrong_agent = cast(dict[str, object], json.loads(json.dumps(session)))
            cast(dict[str, object], wrong_agent["info"])["agent"] = "code"
            with self.assertRaisesRegex(PacketError, "different standalone reviewer"):
                _ = audit_standalone_session(
                    wrong_agent,
                    session_export_sha256="a" * 64,
                    runtime=runtime,
                    expected_model=expected_model,
                    expected_title=title,
                    expected_prompt=prompt,
                    preflight=True,
                )

            wrong_model = cast(dict[str, object], json.loads(json.dumps(session)))
            wrong_model_info = cast(dict[str, object], wrong_model["info"])
            cast(dict[str, object], wrong_model_info["model"])["id"] = "other-model"
            with self.assertRaisesRegex(PacketError, "model/provider/variant"):
                _ = audit_standalone_session(
                    wrong_model,
                    session_export_sha256="a" * 64,
                    runtime=runtime,
                    expected_model=expected_model,
                    expected_title=title,
                    expected_prompt=prompt,
                    preflight=True,
                )

            resumed = cast(dict[str, object], json.loads(json.dumps(session)))
            cast(dict[str, object], resumed["info"])["parentID"] = "ses_prior"
            with self.assertRaisesRegex(PacketError, "parent or resumed context"):
                _ = audit_standalone_session(
                    resumed,
                    session_export_sha256="a" * 64,
                    runtime=runtime,
                    expected_model=expected_model,
                    expected_title=title,
                    expected_prompt=prompt,
                    preflight=True,
                )

            extra_tool = cast(dict[str, object], json.loads(json.dumps(session)))
            extra_messages = cast(list[dict[str, object]], extra_tool["messages"])
            extra_parts = cast(list[dict[str, object]], extra_messages[1]["parts"])
            extra_parts.append({"type": "tool", "tool": "task", "state": {"status": "error"}})
            with self.assertRaisesRegex(PacketError, "other than native Read"):
                _ = audit_standalone_session(
                    extra_tool,
                    session_export_sha256="a" * 64,
                    runtime=runtime,
                    expected_model=expected_model,
                    expected_title=title,
                    expected_prompt=prompt,
                    preflight=True,
                )

            prior_context = cast(dict[str, object], json.loads(json.dumps(session)))
            prior_messages = cast(list[dict[str, object]], prior_context["messages"])
            prior_messages.append(prior_messages[0])
            with self.assertRaisesRegex(PacketError, "exactly one fresh user prompt"):
                _ = audit_standalone_session(
                    prior_context,
                    session_export_sha256="a" * 64,
                    runtime=runtime,
                    expected_model=expected_model,
                    expected_title=title,
                    expected_prompt=prompt,
                    preflight=True,
                )

    def test_failed_session_summary_filters_packet_content_and_counts_preflight_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, _, runtime = self._prepare_standalone(Path(temporary), preflight=True)
            session, _, _, _ = self._standalone_session(runtime, preflight=True)
            report = summarize_failed_standalone_session(
                session,
                session_export_sha256="c" * 64,
                reason="synthetic audit failure",
                preflight=True,
            )

        summary = cast(dict[str, object], report["final_response_summary"])
        self.assertEqual(report["status"], "STANDALONE_PREFLIGHT_TRACE_FAIL")
        self.assertEqual(summary["allowed_path_count"], len(runtime.allowed_paths))
        self.assertEqual(summary["complete_read_count"], len(runtime.allowed_paths))
        self.assertEqual(summary["probe_result_count"], len(runtime.denied_paths))
        self.assertFalse(summary["unauthorized_bytes_returned"])
        self.assertFalse(report["raw_session_export_persisted"])
        self.assertNotIn("SYNTHETIC ONLY:", json.dumps(report))

    def test_standalone_review_result_requires_exact_refs_complete_coverage_and_ux(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, _, runtime = self._prepare_standalone(Path(temporary), preflight=False)
            session, expected_model, title, prompt = self._standalone_session(runtime, preflight=False)
            result = audit_standalone_session(
                session,
                session_export_sha256="b" * 64,
                runtime=runtime,
                expected_model=expected_model,
                expected_title=title,
                expected_prompt=prompt,
            )
            self.assertEqual(result["status"], "STANDALONE_REVIEW_TRACE_PASS")
            self.assertEqual(result["reviewer_result"]["ux_impact"], "none")

            malformed = cast(dict[str, object], json.loads(json.dumps(session)))
            messages = cast(list[dict[str, object]], malformed["messages"])
            final_text = cast(list[dict[str, object]], messages[-1]["parts"])[0]
            bad_result = cast(dict[str, object], json.loads(cast(str, final_text["text"])))
            del bad_result["ux_impact"]
            final_text["text"] = json.dumps(bad_result)
            with self.assertRaisesRegex(PacketError, "missing or unexpected fields"):
                _ = audit_standalone_session(
                    malformed,
                    session_export_sha256="b" * 64,
                    runtime=runtime,
                    expected_model=expected_model,
                    expected_title=title,
                    expected_prompt=prompt,
                )

            wrong_refs = cast(dict[str, object], json.loads(json.dumps(session)))
            wrong_messages = cast(list[dict[str, object]], wrong_refs["messages"])
            wrong_final = cast(list[dict[str, object]], wrong_messages[-1]["parts"])[0]
            wrong_result = cast(dict[str, object], json.loads(cast(str, wrong_final["text"])))
            wrong_result["reviewed_merge_base"] = "0" * 40
            wrong_final["text"] = json.dumps(wrong_result)
            with self.assertRaisesRegex(PacketError, "exact frozen refs"):
                _ = audit_standalone_session(
                    wrong_refs,
                    session_export_sha256="b" * 64,
                    runtime=runtime,
                    expected_model=expected_model,
                    expected_title=title,
                    expected_prompt=prompt,
                )

            incomplete = cast(dict[str, object], json.loads(json.dumps(session)))
            incomplete_messages = cast(list[dict[str, object]], incomplete["messages"])
            incomplete_calls = cast(list[dict[str, object]], incomplete_messages[1]["parts"])
            del incomplete_calls[0]
            with self.assertRaisesRegex(PacketError, "coverage is incomplete"):
                _ = audit_standalone_session(
                    incomplete,
                    session_export_sha256="b" * 64,
                    runtime=runtime,
                    expected_model=expected_model,
                    expected_title=title,
                    expected_prompt=prompt,
                )


if __name__ == "__main__":
    _ = unittest.main()
