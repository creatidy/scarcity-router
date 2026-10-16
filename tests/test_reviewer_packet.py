from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast

from tools.reviewer_packet import (
    PacketError,
    audit_task_trace,
    make_kilo_config_content,
    prepare_task_environment,
    validate_packet,
    verify_effective_agent_pair,
)


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / ".kilo/agents/pr-reviewer.md"
BASE = "1" * 40
HEAD = "2" * 40
MERGE_BASE = BASE


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
        "owner-bootstrap-receipt.txt", "owner-bootstrap.txt", "patch.diff",
    ]
    read_plan = {
        "full_read_paths": [f"packet/{path}" for path in full_read_paths],
        "source_ranges": {"packet/source-00.md": [[1, 3]]},
    }
    contents = {
        "index.md": (
            f'<BEGIN-CANONICAL-ISSUE-212-BODY sha256="{issue_body_sha}">\n'
            f"{issue_body.decode()}\n"
            "<END-CANONICAL-ISSUE-212-BODY>\n"
            "<BEGIN-PACKET-READ-PLAN>\n"
            f"{json.dumps(read_plan, sort_keys=True)}\n"
            "<END-PACKET-READ-PLAN>\n"
            "PR body text is non-authoritative; digest only.\n"
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
    manifest = {
        "schema_version": 1,
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
            "source_ranges": {"source-00.md": [[1, 3]]},
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
        ))
        self.assertEqual(result.review_ranges, {"source-00.md": ((1, 3),)})
        self.assertEqual(result.external_source_count, 1)

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
            _ = manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

            result = validate_packet(packet, manifest_path, source_root=repository)

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

    def test_kilo_config_allows_only_exact_read_paths(self) -> None:
        config = _load_object(make_kilo_config_content(["index.md", "source-00.md"]))
        agent = cast(dict[str, object], config["agent"])
        parent_agent = cast(dict[str, object], agent["code"])
        reviewer_agent = cast(dict[str, object], agent["pr-reviewer"])
        parent = cast(dict[str, str], parent_agent["permission"])
        reviewer = cast(dict[str, object], reviewer_agent["permission"])
        self.assertEqual(parent, {"*": "deny", "task": "allow"})
        self.assertEqual(reviewer["*"], "deny")
        self.assertEqual(reviewer["read"], {
            "*": "deny",
            "packet/index.md": "allow",
            "packet/source-00.md": "allow",
        })
        self.assertEqual(cast(dict[str, str], reviewer["external_directory"])["*"], "deny")

    def test_static_deny_and_runtime_exact_allow_last_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packet, manifest = _make_packet(root)
            profile = PROFILE.read_text(encoding="utf-8")
            result = prepare_task_environment(
                packet, manifest, PROFILE, root, root, preflight=True)
            code_tools = {"task": True, "read": False, "bash": False, "edit": False}
            reviewer_tools = {"read": True, "bash": False, "glob": False, "task": False}
            rules = [
                {"permission": "*", "pattern": "*", "action": "allow"},
                {"permission": "*", "pattern": "*", "action": "deny"},
                {"permission": "read", "pattern": "*", "action": "deny"},
                *[
                    {"permission": "read", "pattern": path, "action": "allow"}
                    for path in result.allowed_paths
                ],
                {"permission": "external_directory", "pattern": "*", "action": "deny"},
            ]
            code: dict[str, object] = {
                "name": "code", "tools": code_tools,
                "permission": [
                    {"permission": "*", "pattern": "*", "action": "allow"},
                    {"permission": "*", "pattern": "*", "action": "deny"},
                    {"permission": "task", "pattern": "*", "action": "allow"},
                ],
            }
            reviewer: dict[str, object] = {
                "name": "pr-reviewer", "mode": "subagent", "tools": reviewer_tools,
                "permission": rules,
                "prompt": profile.split("---", 2)[2].lstrip("\n").rstrip(),
            }
            checked = verify_effective_agent_pair(code, reviewer, result, PROFILE)
            self.assertEqual(checked["parent_effective_tools"], ["task"])
            self.assertEqual(checked["reviewer_effective_tools"], ["read"])

            reopened = dict(reviewer)
            reopened["tools"] = {**reviewer_tools, "bash": True}
            with self.assertRaisesRegex(PacketError, "effective tool set"):
                _ = verify_effective_agent_pair(code, reopened, result, PROFILE)

            broad_read = dict(reviewer)
            broad_read["permission"] = [
                *rules, {"permission": "read", "pattern": "*", "action": "allow"}
            ]
            with self.assertRaisesRegex(PacketError, "not allow|exact rule"):
                _ = verify_effective_agent_pair(code, broad_read, result, PROFILE)

            space_suffix_read = dict(reviewer)
            space_suffix_read["permission"] = [
                *rules, {"permission": "read *", "pattern": "*", "action": "allow"}
            ]
            with self.assertRaisesRegex(PacketError, "exact rule"):
                _ = verify_effective_agent_pair(code, space_suffix_read, result, PROFILE)

    def test_native_preflight_trace_requires_complete_reads_and_denies_before_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_packet, manifest = _make_packet(root)
            runtime = prepare_task_environment(source_packet, manifest, PROFILE, root, root, preflight=True)
            overlay = runtime.profile_config
            self.assertEqual(
                {path.relative_to(overlay).as_posix() for path in overlay.rglob("*")},
                {".gitignore", "kilo.json", "agents", "agents/pr-reviewer.md"},
            )
            config_path = overlay / "kilo.json"
            self.assertEqual(
                json.loads(config_path.read_text(encoding="utf-8")),
                json.loads(runtime.kilo_config_content),
            )
            self.assertEqual(_sha(config_path.read_bytes()), runtime.overlay_config_sha256)
            for path in (config_path, overlay / "agents/pr-reviewer.md"):
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o444)
            canonical_prompt = PROFILE.read_text(encoding="utf-8").split("---", 2)[2].lstrip("\n").rstrip()
            reviewer_overlay = (overlay / "agents/pr-reviewer.md").read_text(encoding="utf-8")
            self.assertEqual(reviewer_overlay.split("---", 2)[2].lstrip("\n").rstrip(), canonical_prompt)
            self.assertIn('"packet/index.md": "allow"', reviewer_overlay)
            self.assertIn('"task":"allow"', runtime.kilo_config_content)
            parent_id = "ses_parent"
            model_binding = {"providerID": "openai", "modelID": "gpt-6-luna"}
            parent: dict[str, object] = {
                "info": {"id": parent_id, "agent": "code", "parentID": None,
                         "directory": str(runtime.workspace), "model": model_binding,
                         "variant": "max"},
                "messages": [{"parts": [{
                    "type": "tool", "tool": "task",
                    "state": {"status": "completed", "input": {
                        "subagent_type": "pr-reviewer", "background": False,
                        "prompt": "Read the packet only and report the preflight result.",
                    }, "metadata": {
                        "approval": {"agent": "code", "rule": {
                            "permission": "task", "action": "allow", "pattern": "*"}}}},
                }]}],
            }
            calls: list[dict[str, object]] = []
            for path in runtime.allowed_paths:
                file_path = runtime.packet / Path(path).name
                lines = _expected_line_map(file_path)
                rendered = "\n".join([*(f"{n}: {line}" for n, line in lines.items()), "",
                                       f"(End of file - total {len(lines)} lines)"])
                calls.append({"type": "tool", "tool": "read", "state": {
                    "status": "completed", "input": {"filePath": path},
                    "output": f"<content>\n{rendered}\n</content>",
                    "metadata": {"approval": {"agent": "pr-reviewer", "rule": {
                        "permission": "read", "action": "allow", "pattern": path}}},
                }})
            for path in runtime.denied_paths:
                normalized = Path(os.path.abspath(Path(path) if Path(path).is_absolute()
                                                   else runtime.workspace / path))
                permission = "read" if normalized.is_relative_to(runtime.workspace) else "external_directory"
                calls.append({"type": "tool", "tool": "read", "state": {
                    "status": "error", "input": {"filePath": path}, "output": None,
                    "error": "permission denied before content",
                    "metadata": {"approval": {"agent": "pr-reviewer", "rule": {
                        "permission": permission, "action": "deny", "pattern": "*"}}},
                }})
            reviewer: dict[str, object] = {
                "info": {"id": "ses_child", "agent": "pr-reviewer", "parentID": parent_id,
                         "directory": str(runtime.workspace), "model": model_binding,
                         "variant": "max"},
                "messages": [{"parts": calls}],
            }
            result = audit_task_trace(
                parent, reviewer, runtime=runtime, expected_model=model_binding, preflight=True
            )
            self.assertEqual(result["status"], "NATIVE_TASK_TRACE_PASS")
            self.assertEqual(result["allowed_file_count"], len(runtime.allowed_paths))
            self.assertEqual(result["denied_before_content_count"], 5)

            leaked = cast(dict[str, object], json.loads(json.dumps(reviewer)))
            messages = cast(list[dict[str, object]], leaked["messages"])
            parts = cast(list[dict[str, object]], messages[0]["parts"])
            state = cast(dict[str, object], parts[-1]["state"])
            state["status"] = "completed"
            state["output"] = "DENIED PROBE CANARY: leaked"
            with self.assertRaisesRegex(PacketError, "denied before content"):
                _ = audit_task_trace(parent, leaked, runtime=runtime, preflight=True)

            for field, value in (
                ("subagent_type", "code"), ("background", True), ("task_id", "ses_old"),
                ("model", "another-model"), ("provider", "another-provider"), ("variant", "minimal"),
            ):
                invalid_parent = cast(dict[str, object], json.loads(json.dumps(parent)))
                invalid_messages = cast(list[dict[str, object]], invalid_parent["messages"])
                invalid_parts = cast(list[dict[str, object]], invalid_messages[0]["parts"])
                invalid_state = cast(dict[str, object], invalid_parts[0]["state"])
                invalid_input = cast(dict[str, object], invalid_state["input"])
                invalid_input[field] = value
                with self.assertRaisesRegex(PacketError, "fresh foreground"):
                    _ = audit_task_trace(
                        invalid_parent, reviewer, runtime=runtime, expected_model=model_binding,
                        preflight=True,
                    )

            wrong_variant = cast(dict[str, object], json.loads(json.dumps(reviewer)))
            wrong_info = cast(dict[str, object], wrong_variant["info"])
            wrong_info["variant"] = "minimal"
            with self.assertRaisesRegex(PacketError, "inherit the exact parent model"):
                _ = audit_task_trace(
                    parent, wrong_variant, runtime=runtime, expected_model=model_binding,
                    preflight=True,
                )


if __name__ == "__main__":
    _ = unittest.main()
