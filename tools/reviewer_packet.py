#!/usr/bin/env python3
"""Validate sealed packets and the standalone Kilo 7.8.8 reviewer boundary.

This is a primary-side validator/config preparer/auditor, not a reviewer launcher
or controller. It never launches a reviewer session, fetches Forgejo data, or edits
the source checkout. Audit mode exports one exact Kilo session in memory and emits
only a filtered trace/result summary; the caller owns selection and currentness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, TextIO, cast


class PacketError(ValueError):
    """The packet or effective permission boundary is invalid."""


def standalone_packet_path_plan(changed_paths: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Return the private-review packet file inventory for a sorted PR delta."""
    if not changed_paths or tuple(sorted(set(changed_paths))) != tuple(changed_paths):
        raise PacketError("standalone packet plan requires nonempty sorted unique changed paths")
    for changed_path in changed_paths:
        _relative_source_path(changed_path)
    source_files = tuple(f"source-{index:02d}.md" for index in range(len(changed_paths)))
    return tuple(sorted((*STANDALONE_PACKET_FIXED_PATHS, *source_files)))


def validate_standalone_packet_path_plan(inspection: PacketInspection) -> None:
    expected = standalone_packet_path_plan(inspection.changed_paths)
    if inspection.file_paths != expected:
        raise PacketError("standalone packet inventory differs from the complete canonical path plan")


def create_synthetic_standalone_packet(
    parent: Path,
    *,
    changed_paths: tuple[str, ...] | list[str],
    head: str,
    base: str,
    merge_base: str,
) -> tuple[Path, Path]:
    """Create a harmless packet with the exact production standalone file-path plan."""
    paths = standalone_packet_path_plan(changed_paths)
    if not _SHA1.fullmatch(head) or not _SHA1.fullmatch(base) or not _SHA1.fullmatch(merge_base):
        raise PacketError("synthetic packet refs must be full SHA-1 values")
    if (not parent.is_dir() or parent.is_symlink()
            or stat.S_IMODE(parent.stat().st_mode) & 0o077):
        raise PacketError("synthetic packet parent must be a private existing real directory")
    packet = parent / "packet"
    manifest_path = parent / "manifest.json"
    if packet.exists() or packet.is_symlink() or manifest_path.exists() or manifest_path.is_symlink():
        raise PacketError("synthetic packet output already exists")
    packet.mkdir(mode=0o700)

    issue_body = b"Synthetic permission-preflight acceptance only; no PR code or review findings."
    issue_body_sha = sha256(issue_body)
    read_plan = {
        "full_read_paths": [f"packet/{path}" for path in paths],
        "source_ranges": {},
    }
    contents: dict[str, bytes] = {}
    for path in paths:
        if path == "index.md":
            text = (
                f'<BEGIN-CANONICAL-ISSUE-212-BODY sha256="{issue_body_sha}">\n'
                f"{issue_body.decode()}\n"
                "<END-CANONICAL-ISSUE-212-BODY>\n"
                "<BEGIN-PACKET-READ-PLAN>\n"
                f"{json.dumps(read_plan, sort_keys=True)}\n"
                "<END-PACKET-READ-PLAN>\n"
                "Synthetic-only fixture: every other listed file is harmless.\n"
                "PR body text is non-authoritative; digest only.\n"
            )
            contents[path] = text.encode("utf-8")
        elif path == "patch.diff":
            contents[path] = b"diff --git a/synthetic b/synthetic\n+synthetic-only patch\n"
        elif path in {"owner-bootstrap-receipt.txt", "owner-210-receipt.txt"}:
            contents[path] = b"SYNTHETIC Kilo transcription receipt; not owner-authored.\n"
        else:
            contents[path] = f"SYNTHETIC ONLY: {path}\n".encode("utf-8")

    for path, data in contents.items():
        target = packet / path
        target.write_bytes(data)
        os.chmod(target, 0o444)

    owner_receipts = {
        "owner-bootstrap-receipt.txt": (
            212, 17305, "2026-10-15T19:01:02Z",
        ),
        "owner-210-receipt.txt": (
            210, 17258, "2026-10-15T17:09:48Z",
        ),
    }
    file_rows: list[dict[str, Any]] = []
    for path in paths:
        data = contents[path]
        if path == "index.md":
            kind, origin, revision = "index", "synthetic preflight index", head
        elif path == "patch.diff":
            kind, origin, revision = "patch", "synthetic preflight patch", head
        elif path in owner_receipts:
            issue_number, comment_id, timestamp = owner_receipts[path]
            kind = "transcription-receipt"
            origin = (
                f"https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/"
                f"{issue_number}#issuecomment-{comment_id}"
            )
            revision = timestamp
        elif path in OWNER_EVIDENCE_PACKET_PATHS:
            kind, origin, revision = "owner-source", "synthetic owner source placeholder", head
        elif path in KILO_SOURCE_PACKET_PATHS:
            source_path = KILO_SOURCE_REPOSITORY_PATHS[path]
            revision = "e2887f08e5a6394299c810ba508f358aaf4c5bc3"
            kind = "external-source"
            origin = f"https://github.com/Kilo-Org/kilocode/blob/{revision}/{source_path}"
        else:
            index = int(Path(path).stem.removeprefix("source-"))
            kind, origin, revision = "git-source", changed_paths[index], head
        file_rows.append({
            "path": path,
            "size": len(data),
            "sha256": sha256(data),
            "mode": 0o444,
            "link_count": 1,
            "kind": kind,
            "origin": origin,
            "revision": revision,
        })
    os.chmod(packet, 0o555)

    source_bindings: dict[str, dict[str, Any]] = {}
    for index, source_path in enumerate(changed_paths):
        packet_path = f"source-{index:02d}.md"
        source_bindings[source_path] = {
            "packet_path": packet_path,
            "base_exists": False,
            "base_blob": None,
            "head_blob": hashlib.sha1(f"synthetic:{source_path}".encode()).hexdigest(),
            "sha256": sha256(contents[packet_path]),
            "ranges": [[1, 1]],
        }
    owner_sources = []
    for path, receipt, issue_number, comment_id, timestamp, decision_type in (
        ("owner-bootstrap.txt", "owner-bootstrap-receipt.txt", 212, 17305,
         "2026-10-15T19:01:02Z", "synthetic bootstrap owner evidence"),
        ("owner-210.txt", "owner-210-receipt.txt", 210, 17258,
         "2026-10-15T17:09:48Z", "synthetic downstream owner evidence"),
    ):
        receipt_url = (
            f"https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/"
            f"{issue_number}#issuecomment-{comment_id}"
        )
        owner_sources.append({
            "packet_path": path,
            "sha256": sha256(contents[path]),
            "source_origin": "synthetic-only fixture; not an owner decision",
            "decision_type": decision_type,
            "transcription_receipt": {
                "packet_path": receipt,
                "comment_id": comment_id,
                "issue_number": issue_number,
                "author": "kilo",
                "created_at": timestamp,
                "url": receipt_url,
                "sha256": sha256(contents[receipt]),
            },
        })
    external_sources = []
    for path in KILO_SOURCE_PACKET_PATHS:
        source_path = KILO_SOURCE_REPOSITORY_PATHS[path]
        revision = "e2887f08e5a6394299c810ba508f358aaf4c5bc3"
        external_sources.append({
            "packet_path": path,
            "repository": "Kilo-Org/kilocode",
            "revision": revision,
            "source_path": source_path,
            "url": f"https://github.com/Kilo-Org/kilocode/blob/{revision}/{source_path}",
            "blob_sha1": hashlib.sha1(source_path.encode()).hexdigest(),
            "start_line": 1,
            "end_line": 1,
            "excerpt_sha256": sha256(contents[path]),
        })
    repository_url = "https://forgejo.creatidy.com/BioMedical-IT/scarcity-router"
    issue_url = f"{repository_url}/issues/212"
    pr_url = f"{repository_url}/pulls/213"
    manifest = {
        "schema_version": 1,
        "synthetic_fixture": True,
        "repository": {"id": 15, "owner": "BioMedical-IT", "name": "scarcity-router", "url": repository_url},
        "issue": {"id": 1683, "number": 212, "url": issue_url, "body_sha256": issue_body_sha},
        "pull_request": {
            "id": 927, "number": 213, "url": pr_url,
            "head_branch": "issue-212-d069a-bootstrap", "base_branch": "develop",
            "head": head, "base": base, "merge_base": merge_base,
            "body_sha256": "b" * 64,
        },
        "issue_state": "open", "pr_state": "open", "pr_merged": False,
        "head": head, "base": base, "merge_base": merge_base,
        "currentness": {
            "checked_at": "2026-10-17T22:15:33Z", "repo_id": 15,
            "issue_id": 1683, "pr_id": 927,
            "head": head, "base": base, "merge_base": merge_base,
        },
        "changed_paths": list(changed_paths),
        "source_bindings": source_bindings,
        "owner_sources": owner_sources,
        "external_sources": external_sources,
        "review_coverage": {"full_read_paths": list(paths), "source_ranges": {}},
        "previous_review_findings_included": False,
        "pr_body_content_included": False,
        "files": file_rows,
    }
    manifest_path = parent / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    os.chmod(manifest_path, 0o600)
    return packet, manifest_path


def validate_synthetic_preflight_packet(packet_root: Path, manifest_path: Path) -> PacketInspection:
    """Require the complete production path plan with harmless synthetic bytes only."""
    manifest = _object(json.loads(manifest_path.read_bytes()), "synthetic preflight manifest")
    if manifest.get("synthetic_fixture") is not True:
        raise PacketError("preflight packet is not marked as a synthetic fixture")
    changed_paths = tuple(
        _string(path, "synthetic changed path")
        for path in _array(manifest.get("changed_paths"), "synthetic changed paths")
    )
    if tuple(sorted(set(changed_paths))) != changed_paths:
        raise PacketError("synthetic changed paths are not sorted and unique")
    expected_paths = set(standalone_packet_path_plan(changed_paths))
    actual_paths = {
        _relative_file(_object(item, "synthetic manifest file").get("path"))
        for item in _array(manifest.get("files"), "synthetic manifest files")
    }
    if actual_paths != expected_paths:
        raise PacketError("synthetic preflight inventory differs from the production packet plan")
    for path in sorted(expected_paths):
        data = (packet_root / path).read_bytes()
        if b"synthetic" not in data.lower():
            raise PacketError(f"synthetic preflight packet contains non-synthetic bytes: {path}")
    return validate_packet(packet_root, manifest_path, require_external_sources=True)


@dataclass(frozen=True)
class PacketInspection:
    packet_root: str
    manifest_path: str
    manifest_sha256: str
    repository: str
    issue_number: int
    pull_number: int
    head: str
    base: str
    merge_base: str
    file_paths: tuple[str, ...]
    changed_paths: tuple[str, ...]
    external_source_count: int
    full_read_paths: tuple[str, ...]
    review_ranges: dict[str, tuple[tuple[int, int], ...]]


@dataclass(frozen=True)
class RuntimeWorkspace:
    workspace: Path
    packet: Path
    inspection: PacketInspection
    external_source_roots: dict[str, Path]
    canonical_profile: Path
    profile_source_root: Path | None
    profile_config: Path
    probe_root: Path | None
    probe_hashes: dict[str, str]
    probe_symlinks: dict[str, str]
    probe_labels: dict[str, str]
    allowed_paths: tuple[str, ...]
    denied_paths: tuple[str, ...]
    profile_prompt_sha256: str
    canonical_profile_sha256: str
    overlay_profile_sha256: str
    overlay_config_sha256: str
    kilo_config_content: str


_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
KILO_SOURCE_PACKET_PATHS = (
    "kilo-LICENSE.txt",
    "kilo-agent-config-schema.ts.txt",
    "kilo-config-directory-files.ts.txt",
    "kilo-config-load-order.ts.txt",
    "kilo-config-merge.ts.txt",
    "kilo-permission-resolution.ts.txt",
    "kilo-read-path.ts.txt",
    "kilo-task-inheritance.ts.txt",
    "kilo-task-parent.ts.txt",
    "kilo-cli-run-agent-selection.ts.txt",
    "kilo-cli-json-events.ts.txt",
    "kilo-cli-session-export.ts.txt",
)
KILO_SOURCE_REPOSITORY_PATHS = {
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
OWNER_EVIDENCE_PACKET_PATHS = (
    "owner-210-receipt.txt",
    "owner-210.txt",
    "owner-bootstrap-receipt.txt",
    "owner-bootstrap.txt",
)
STANDALONE_PACKET_FIXED_PATHS = (
    "index.md", *KILO_SOURCE_PACKET_PATHS, *OWNER_EVIDENCE_PACKET_PATHS, "patch.diff",
)
_SECRET_PATTERNS = (
    ("private-key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("aws-access-key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9_]{30,}\b")),
    ("openai-token", re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{40,}\b")),
    ("bearer-token", re.compile(rb"\bBearer\s+[A-Za-z0-9._~-]{32,}")),
)
_FORBIDDEN_TOOLS = (
    "bash", "edit", "write", "apply_patch", "glob", "grep", "list", "task",
    "webfetch", "websearch", "semantic_search", "kilo_memory_recall",
    "kilo_memory_save", "agent_manager", "board_read", "board_post", "skill",
    "question", "todowrite", "todoread",
)
_MCP_PROBES = (
    "forgejo_mcp_get_repo", "forgejo_mcp_get_pull_request_by_index",
    "forgejo_mcp_get_issue_by_index", "forgejo_mcp_get_issue_comment",
    "forgejo-mcp_get_repo", "forgejo-mcp_get_pull_request_by_index",
    "forgejo-mcp_get_issue_by_index", "forgejo-mcp_get_issue_comment",
)


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PacketError(f"{label} must be an object")
    return cast(dict[str, Any], value)


def _array(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise PacketError(f"{label} must be an array")
    return cast(list[Any], value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PacketError(f"{label} must be a nonempty string")
    return value


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative_file(value: object) -> str:
    result = _string(value, "packet path")
    parsed = PurePosixPath(result)
    if (
        parsed.is_absolute()
        or len(parsed.parts) != 1
        or parsed.parts[0] in {".", ".."}
        or "\\" in result
    ):
        raise PacketError(f"packet path must be a flat relative filename: {result!r}")
    return result


def _relative_source_path(value: object) -> str:
    result = _string(value, "external source path")
    parsed = PurePosixPath(result)
    if (
        parsed.is_absolute()
        or parsed.as_posix() != result
        or any(part in {"", ".", ".."} for part in parsed.parts)
        or "\\" in result
    ):
        raise PacketError("external source path must be a normalized relative Git path")
    return result


def _run(argv: list[str], cwd: Path) -> bytes:
    result = subprocess.run(
        argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise PacketError(f"evidence command failed: {Path(argv[0]).name} {argv[1]}")
    return result.stdout


def _credential_candidate(data: bytes) -> str | None:
    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(data):
            return label
    return None


def _inventory(packet_root: Path) -> dict[str, dict[str, Any]]:
    if packet_root.is_symlink() or not packet_root.is_dir():
        raise PacketError("packet root must be a real directory")
    if stat.S_IMODE(packet_root.stat().st_mode) != 0o555:
        raise PacketError("packet root must be sealed with mode 0555")
    result: dict[str, dict[str, Any]] = {}
    for item in packet_root.iterdir():
        name = _relative_file(item.name)
        info = item.lstat()
        if item.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise PacketError(f"packet contains a symlink/non-file entry: {name}")
        if info.st_nlink != 1:
            raise PacketError(f"packet contains a hard-linked file: {name}")
        if stat.S_IMODE(info.st_mode) != 0o444:
            raise PacketError(f"packet file is not mode 0444: {name}")
        if name.lower().startswith(".env") or any(
            word in name.lower() for word in ("credential", "secret", "token", "private-key")
        ):
            raise PacketError(f"credential-bearing filename is not permitted: {name}")
        data = item.read_bytes()
        candidate = _credential_candidate(data)
        if candidate:
            raise PacketError(f"credential-like value detected in {name} ({candidate})")
        result[name] = {
            "path": name,
            "size": len(data),
            "sha256": sha256(data),
            "mode": stat.S_IMODE(info.st_mode),
            "link_count": info.st_nlink,
            "data": data,
        }
    return result


def _git(root: Path, *args: str) -> bytes:
    return _run(["git", *args], root)


def _verify_profile_git_binding(profile: Path, source_root: Path, expected_head: str) -> str:
    source_root = source_root.resolve()
    expected_path = source_root / ".kilo/agents/pr-reviewer-standalone.md"
    if (source_root.is_symlink() or not source_root.is_dir()
            or profile.resolve() != expected_path.resolve()):
        raise PacketError("standalone profile source must be the tracked profile in the exact checkout")
    if _git(source_root, "rev-parse", "HEAD").decode().strip() != expected_head:
        raise PacketError("standalone profile source checkout HEAD differs from the frozen packet")
    if _git(source_root, "status", "--porcelain"):
        raise PacketError("standalone profile source checkout is not clean")
    source = _git(source_root, "show", f"{expected_head}:.kilo/agents/pr-reviewer-standalone.md")
    if source != profile.read_bytes():
        raise PacketError("standalone reviewer profile bytes differ from the frozen Git blob")
    return sha256(source)


def extract_pinned_source_excerpt(
    source_root: Path,
    repository: str,
    revision: str,
    source_path: str,
    start_line: int,
    end_line: int,
    destination: Path,
) -> dict[str, Any]:
    """Copy one exact line range from a locally fetched, full-SHA public revision."""
    source_path = _relative_source_path(source_path)
    if not _SHA1.fullmatch(revision) or start_line < 1 or end_line < start_line:
        raise PacketError("external source pin or line range is malformed")
    if not source_root.is_dir() or source_root.is_symlink():
        raise PacketError("external source checkout must be a real isolated directory")
    if _git(source_root, "rev-parse", "HEAD").decode().strip() != revision:
        raise PacketError("external source checkout is not at its exact pinned revision")
    blob_sha1 = _git(source_root, "rev-parse", f"{revision}:{source_path}").decode().strip()
    if not _SHA1.fullmatch(blob_sha1):
        raise PacketError("external source Git blob is not a full object ID")
    source_bytes = _git(source_root, "show", f"{revision}:{source_path}")
    source_lines = source_bytes.splitlines(keepends=True)
    if end_line > len(source_lines):
        raise PacketError("external source excerpt range exceeds the pinned file")
    excerpt = b"".join(source_lines[start_line - 1:end_line])
    if not destination.parent.is_dir() or destination.parent.is_symlink():
        raise PacketError("external evidence destination parent must already exist")
    if destination.exists() or destination.is_symlink():
        raise PacketError("external evidence destination already exists")
    destination.write_bytes(excerpt)
    os.chmod(destination, 0o444)
    return {
        "repository": repository,
        "url": f"https://github.com/{repository}/blob/{revision}/{source_path}",
        "revision": revision,
        "source_path": source_path,
        "blob_sha1": blob_sha1,
        "start_line": start_line,
        "end_line": end_line,
        "packet_path": destination.name,
        "excerpt_sha256": sha256(excerpt),
        "source_file_sha256": sha256(source_bytes),
    }


def validate_packet(
    packet_root: Path,
    manifest_path: Path,
    *,
    source_root: Path | None = None,
    profile_source_root: Path | None = None,
    external_source_roots: dict[str, Path] | None = None,
    require_external_sources: bool = True,
) -> PacketInspection:
    """Validate sealed files, identities, changed-path coverage and source pins."""
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = _object(json.loads(manifest_bytes), "manifest")
    except (OSError, json.JSONDecodeError) as err:
        raise PacketError("packet manifest is missing or malformed") from err

    if manifest.get("schema_version") != 1:
        raise PacketError("unsupported reviewer packet manifest version")
    if manifest.get("previous_review_findings_included") is not False:
        raise PacketError("previous review findings must not be packet content")
    if manifest.get("pr_body_content_included") is not False:
        raise PacketError("PR-body history must not be packet content")
    if manifest.get("issue_state") != "open" or manifest.get("pr_state") != "open":
        raise PacketError("canonical Issue and PR must be recorded open")
    if manifest.get("pr_merged") is not False:
        raise PacketError("canonical PR must be recorded unmerged")

    repo = _object(manifest.get("repository"), "manifest.repository")
    if (
        repo.get("id") != 15
        or repo.get("owner") != "BioMedical-IT"
        or repo.get("name") != "scarcity-router"
        or repo.get("url") != "https://forgejo.creatidy.com/BioMedical-IT/scarcity-router"
    ):
        raise PacketError("manifest repository identity is not canonical")
    issue = _object(manifest.get("issue"), "manifest.issue")
    pull = _object(manifest.get("pull_request"), "manifest.pull_request")
    if issue.get("id") != 1683 or issue.get("number") != 212:
        raise PacketError("manifest issue identity is not #212")
    if issue.get("url") != "https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/212":
        raise PacketError("manifest issue URL is not canonical")
    if pull.get("id") != 927 or pull.get("number") != 213:
        raise PacketError("manifest pull-request identity is not #213")
    if (pull.get("url") != "https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/pulls/213"
            or pull.get("head_branch") != "issue-212-d069a-bootstrap"
            or pull.get("base_branch") != "develop"):
        raise PacketError("manifest pull-request URL/branch binding is not canonical")

    head = _string(manifest.get("head"), "manifest.head")
    base = _string(manifest.get("base"), "manifest.base")
    merge_base = _string(manifest.get("merge_base"), "manifest.merge_base")
    if not all(_SHA1.fullmatch(ref) for ref in (head, base, merge_base)):
        raise PacketError("manifest refs must be full 40-character Git IDs")
    if merge_base != base:
        raise PacketError("PR merge-base must match its base")
    if pull.get("head") != head or pull.get("base") != base or pull.get("merge_base") != merge_base:
        raise PacketError("PR ref values disagree with the frozen manifest refs")
    if not _SHA256.fullmatch(_string(issue.get("body_sha256"), "issue body digest")):
        raise PacketError("canonical Issue body digest is malformed")
    if not _SHA256.fullmatch(_string(pull.get("body_sha256"), "PR body digest")):
        raise PacketError("canonical PR body digest is malformed")

    currentness = _object(manifest.get("currentness"), "manifest.currentness")
    if (
        currentness.get("repo_id") != 15
        or currentness.get("issue_id") != 1683
        or currentness.get("pr_id") != 927
        or currentness.get("head") != head
        or currentness.get("base") != base
        or currentness.get("merge_base") != merge_base
        or not isinstance(currentness.get("checked_at"), str)
    ):
        raise PacketError("canonical currentness binding is incomplete or mismatched")

    records: dict[str, dict[str, Any]] = {}
    for raw in _array(manifest.get("files"), "manifest.files"):
        record = _object(raw, "manifest file record")
        path = _relative_file(record.get("path"))
        if path in records:
            raise PacketError(f"duplicate packet path: {path}")
        if not _SHA256.fullmatch(_string(record.get("sha256"), f"{path} sha256")):
            raise PacketError(f"invalid packet SHA-256: {path}")
        if not isinstance(record.get("size"), int) or record["size"] < 0:
            raise PacketError(f"invalid packet file size: {path}")
        if record.get("mode") != 0o444 or record.get("link_count") != 1:
            raise PacketError(f"packet mode/link record is unsafe: {path}")
        for key in ("kind", "origin", "revision"):
            _string(record.get(key), f"{path} {key}")
        records[path] = record

    inventory = _inventory(packet_root)
    if set(inventory) != set(records):
        raise PacketError("actual packet paths differ from the exact manifest set")
    for path, record in records.items():
        actual = inventory[path]
        if actual["size"] != record["size"] or actual["sha256"] != record["sha256"]:
            raise PacketError(f"packet file size/digest mismatch: {path}")

    required = {
        "index.md", "patch.diff", "owner-bootstrap.txt", "owner-210.txt",
        "owner-bootstrap-receipt.txt", "owner-210-receipt.txt",
    }
    if not required.issubset(records):
        raise PacketError("packet is missing acceptance, patch or raw owner-source evidence")
    index_text = inventory["index.md"]["data"].decode("utf-8", errors="strict")
    for marker in ("<BEGIN-CANONICAL-ISSUE-212-BODY", "<END-CANONICAL-ISSUE-212-BODY>",
                   "PR body text is non-authoritative"):
        if marker not in index_text:
            raise PacketError(f"index is missing a required canonical binding: {marker}")
    body_marker = re.search(
        r"(?m)^<BEGIN-CANONICAL-ISSUE-212-BODY sha256=\"([0-9a-f]{64})\">$",
        index_text,
    )
    end_marker = "\n<END-CANONICAL-ISSUE-212-BODY>"
    if (body_marker is None or index_text.count("<BEGIN-CANONICAL-ISSUE-212-BODY") != 1
            or index_text.count(end_marker) != 1):
        raise PacketError("index canonical issue body markers are malformed or duplicated")
    body_start = index_text.find("\n", body_marker.end()) + 1
    body_end = index_text.rfind(end_marker)
    canonical_issue_body = index_text[body_start:body_end]
    issue_body_sha = _string(issue.get("body_sha256"), "issue body digest")
    if body_marker.group(1) != issue_body_sha or sha256(canonical_issue_body.encode("utf-8")) != issue_body_sha:
        raise PacketError("indexed canonical issue body differs from its bound digest")
    if records["index.md"].get("kind") != "index" or records["patch.diff"].get("kind") != "patch":
        raise PacketError("canonical index or full PR patch has an invalid file kind")

    changed_paths_raw = _array(manifest.get("changed_paths"), "manifest.changed_paths")
    changed_paths = tuple(_string(path, "changed path") for path in changed_paths_raw)
    if tuple(sorted(set(changed_paths))) != changed_paths:
        raise PacketError("changed paths must be a sorted unique list")
    bindings = _object(manifest.get("source_bindings"), "manifest.source_bindings")
    if set(bindings) != set(changed_paths):
        raise PacketError("source binding map does not cover the full changed-path set")
    for source_path in changed_paths:
        binding = _object(bindings[source_path], f"source binding {source_path}")
        packet_path = _relative_file(binding.get("packet_path"))
        record = records.get(packet_path)
        if record is None or record.get("kind") != "git-source":
            raise PacketError(f"changed path lacks an indexed Git source: {source_path}")
        if record.get("origin") != source_path or record.get("revision") != head:
            raise PacketError(f"changed source origin/revision mismatch: {source_path}")
        if binding.get("sha256") != record.get("sha256"):
            raise PacketError(f"changed source digest mismatch: {source_path}")
        base_exists = binding.get("base_exists")
        if not isinstance(base_exists, bool):
            raise PacketError(f"changed source base-presence flag malformed: {source_path}")
        base_blob = binding.get("base_blob")
        if base_exists:
            if not isinstance(base_blob, str) or not _SHA1.fullmatch(base_blob):
                raise PacketError(f"changed source base blob malformed: {source_path}")
        elif base_blob is not None:
            raise PacketError(f"new changed source must not claim a base blob: {source_path}")
        if not _SHA1.fullmatch(_string(binding.get("head_blob"), f"{source_path} head blob")):
            raise PacketError(f"changed source head blob malformed: {source_path}")
        ranges = _array(binding.get("ranges"), f"{source_path} ranges")
        if not ranges:
            raise PacketError(f"changed source lacks required review ranges: {source_path}")
        line_count = len(inventory[packet_path]["data"].splitlines())
        previous_end = 0
        for raw_range in ranges:
            if (not isinstance(raw_range, list) or len(raw_range) != 2
                    or type(raw_range[0]) is not int or type(raw_range[1]) is not int
                    or raw_range[0] <= previous_end or raw_range[1] < raw_range[0]
                    or raw_range[1] > line_count):
                raise PacketError(f"changed source range is malformed or outside file: {source_path}")
            previous_end = raw_range[1]

    owner_sources = _array(manifest.get("owner_sources"), "manifest.owner_sources")
    owner_paths: set[str] = set()
    expected_receipts = {
        "owner-bootstrap.txt": (212, 17305),
        "owner-210.txt": (210, 17258),
    }
    for raw in owner_sources:
        owner = _object(raw, "owner source record")
        packet_path = _relative_file(owner.get("packet_path"))
        owner_paths.add(packet_path)
        record = records.get(packet_path)
        if record is None or record.get("kind") != "owner-source":
            raise PacketError(f"owner decision is not a raw packet file: {packet_path}")
        if owner.get("sha256") != record.get("sha256"):
            raise PacketError(f"owner decision digest mismatch: {packet_path}")
        _string(owner.get("source_origin"), f"{packet_path} source origin")
        _string(owner.get("decision_type"), f"{packet_path} decision type")
        expected_issue, expected_comment = expected_receipts.get(packet_path, (0, 0))
        receipt = _object(owner.get("transcription_receipt"), f"{packet_path} transcription receipt")
        receipt_path = _relative_file(receipt.get("packet_path"))
        receipt_record = records.get(receipt_path)
        if receipt_record is None or receipt_record.get("kind") != "transcription-receipt":
            raise PacketError(f"owner source lacks its canonical transcription receipt: {packet_path}")
        comment_id = receipt.get("comment_id")
        issue_number = receipt.get("issue_number")
        author = receipt.get("author")
        created_at = _string(receipt.get("created_at"), f"{packet_path} receipt timestamp")
        url = _string(receipt.get("url"), f"{packet_path} receipt URL")
        receipt_sha = _string(receipt.get("sha256"), f"{packet_path} receipt digest")
        expected_url = (
            f"https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/"
            f"{expected_issue}#issuecomment-{expected_comment}"
        )
        if (comment_id != expected_comment or issue_number != expected_issue or author != "kilo"
                or url != expected_url or receipt_record.get("origin") != url
                or receipt_record.get("revision") != created_at
                or receipt_record.get("sha256") != receipt_sha
                or not _SHA256.fullmatch(receipt_sha)):
            raise PacketError(f"canonical transcription receipt metadata mismatch: {packet_path}")
        receipt_text = inventory[receipt_path]["data"].decode("utf-8", errors="strict")
        if "Kilo transcription receipt" not in receipt_text or "not owner-authored" not in receipt_text:
            raise PacketError(f"transcription receipt attribution is not explicit: {receipt_path}")
    if not {"owner-bootstrap.txt", "owner-210.txt"}.issubset(owner_paths):
        raise PacketError("packet must contain both direct owner-source files")

    external_sources = _array(manifest.get("external_sources"), "manifest.external_sources")
    if require_external_sources and not external_sources:
        raise PacketError("required pinned external-source evidence is missing")
    for raw in external_sources:
        external = _object(raw, "external source binding")
        packet_path = _relative_file(external.get("packet_path"))
        record = records.get(packet_path)
        if record is None or record.get("kind") != "external-source":
            raise PacketError(f"pinned external source is missing: {packet_path}")
        repository = _string(external.get("repository"), f"{packet_path} repository")
        revision = _string(external.get("revision"), f"{packet_path} revision")
        if not _SHA1.fullmatch(revision):
            raise PacketError(f"external source revision is not pinned: {packet_path}")
        if not _SHA1.fullmatch(_string(external.get("blob_sha1"), f"{packet_path} Git blob")):
            raise PacketError(f"external source blob ID is malformed: {packet_path}")
        source_path = _relative_source_path(external.get("source_path"))
        url = _string(external.get("url"), f"{packet_path} origin URL")
        expected_url = f"https://github.com/{repository}/blob/{revision}/{source_path}"
        if url != expected_url or record.get("origin") != expected_url or record.get("revision") != revision:
            raise PacketError(f"external source URL/revision does not bind the indexed origin: {packet_path}")
        if external.get("excerpt_sha256") != record.get("sha256"):
            raise PacketError(f"external source excerpt digest mismatch: {packet_path}")
        start_line = external.get("start_line")
        end_line = external.get("end_line")
        if (type(start_line) is not int or type(end_line) is not int
                or start_line < 1 or end_line < start_line):
            raise PacketError(f"external source line range is malformed: {packet_path}")
        if len(inventory[packet_path]["data"].splitlines(keepends=True)) != end_line - start_line + 1:
            raise PacketError(f"external source excerpt line count mismatch: {packet_path}")

    coverage = _object(manifest.get("review_coverage"), "manifest.review_coverage")
    full_read_paths = tuple(
        _relative_file(path) for path in _array(coverage.get("full_read_paths"), "full_read_paths")
    )
    if tuple(sorted(set(full_read_paths))) != full_read_paths:
        raise PacketError("full-read paths must be a sorted unique list")
    raw_ranges = _object(coverage.get("source_ranges"), "review_coverage.source_ranges")
    review_ranges: dict[str, tuple[tuple[int, int], ...]] = {}
    for raw_path, raw_file_ranges in raw_ranges.items():
        path = _relative_file(raw_path)
        if path not in records or path in full_read_paths:
            raise PacketError(f"invalid/overlapping review-range path: {path}")
        line_count = len(inventory[path]["data"].splitlines())
        ranges: list[tuple[int, int]] = []
        for raw_range in _array(raw_file_ranges, f"source ranges {path}"):
            if (not isinstance(raw_range, list) or len(raw_range) != 2
                    or type(raw_range[0]) is not int or type(raw_range[1]) is not int
                    or raw_range[0] < 1 or raw_range[1] < raw_range[0]
                    or raw_range[1] > line_count):
                raise PacketError(f"review range is outside the indexed source file: {path}")
            if ranges and raw_range[0] <= ranges[-1][1]:
                raise PacketError(f"review ranges overlap or are not sorted: {path}")
            ranges.append((raw_range[0], raw_range[1]))
        if not ranges:
            raise PacketError(f"changed source has no review range: {path}")
        review_ranges[path] = tuple(ranges)
    if set(full_read_paths) & set(review_ranges):
        raise PacketError("review coverage cannot mark a file both full and ranged")
    if set(full_read_paths) | set(review_ranges) != set(records):
        raise PacketError("review coverage does not cover every indexed packet file")
    read_plan_start = "<BEGIN-PACKET-READ-PLAN>"
    read_plan_end = "<END-PACKET-READ-PLAN>"
    if index_text.count(read_plan_start) != 1 or index_text.count(read_plan_end) != 1:
        raise PacketError("index must contain one complete packet Read plan")
    plan_start = index_text.index(read_plan_start) + len(read_plan_start)
    plan_end = index_text.index(read_plan_end, plan_start)
    try:
        read_plan = _object(json.loads(index_text[plan_start:plan_end].strip()), "index Read plan")
    except json.JSONDecodeError as err:
        raise PacketError("index packet Read plan is malformed") from err
    expected_read_plan = {
        "full_read_paths": [f"packet/{path}" for path in full_read_paths],
        "source_ranges": {
            f"packet/{path}": [[start, end] for start, end in ranges]
            for path, ranges in review_ranges.items()
        },
    }
    if read_plan != expected_read_plan:
        raise PacketError("index Read plan differs from manifest full paths/ranges")
    if not required.issubset(full_read_paths):
        raise PacketError("review must read the index, patch, raw owner sources and provenance receipts")

    if source_root is not None:
        actual_head = _git(source_root, "rev-parse", "HEAD").decode().strip()
        actual_merge_base = _git(source_root, "merge-base", head, base).decode().strip()
        if actual_head != head or actual_merge_base != merge_base:
            raise PacketError("source checkout head/merge-base differs from packet")
        if _git(source_root, "status", "--porcelain"):
            raise PacketError("source checkout is not clean")
        actual_changed = _git(source_root, "diff", "--name-only", f"{base}...{head}").decode().splitlines()
        if actual_changed != list(changed_paths):
            raise PacketError("packet changed-path coverage differs from exact Git delta")
        for source_path in changed_paths:
            binding = _object(bindings[source_path], f"source binding {source_path}")
            base_result = subprocess.run(
                ["git", "cat-file", "-e", f"{base}:{source_path}"],
                cwd=source_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            base_exists = base_result.returncode == 0
            if base_exists is not binding["base_exists"]:
                raise PacketError(f"base Git path-presence mismatch for {source_path}")
            if base_exists and _git(source_root, "rev-parse", f"{base}:{source_path}").decode().strip() != binding["base_blob"]:
                raise PacketError(f"base Git blob mismatch for {source_path}")
            if _git(source_root, "rev-parse", f"{head}:{source_path}").decode().strip() != binding["head_blob"]:
                raise PacketError(f"HEAD Git blob mismatch for {source_path}")
            source_bytes = _git(source_root, "show", f"{head}:{source_path}")
            if sha256(source_bytes) != binding["sha256"]:
                raise PacketError(f"source bytes differ from bound Git blob: {source_path}")

    if external_source_roots:
        for raw in external_sources:
            external = _object(raw, "external source binding")
            repo_key = _string(external.get("repository"), "external repository")
            checkout = external_source_roots.get(repo_key)
            if checkout is None:
                raise PacketError(f"no isolated checkout supplied for external source {repo_key}")
            revision = _string(external.get("revision"), "external source revision")
            source_path = _relative_source_path(external.get("source_path"))
            if _git(checkout, "rev-parse", "HEAD").decode().strip() != revision:
                raise PacketError(f"external checkout is not at pinned revision {revision}")
            blob = _git(checkout, "rev-parse", f"{revision}:{source_path}").decode().strip()
            if blob != external.get("blob_sha1"):
                raise PacketError(f"external Git blob mismatch for {source_path}")
            source_bytes = _git(checkout, "show", f"{revision}:{source_path}")
            source_lines = source_bytes.splitlines(keepends=True)
            start_line = external.get("start_line")
            end_line = external.get("end_line")
            if not isinstance(start_line, int) or not isinstance(end_line, int) or end_line > len(source_lines):
                raise PacketError(f"external source line range is outside {source_path}")
            excerpt = b"".join(source_lines[start_line - 1:end_line])
            packet_path = _relative_file(external.get("packet_path"))
            if (sha256(excerpt) != external.get("excerpt_sha256")
                    or inventory[packet_path]["data"] != excerpt):
                raise PacketError(f"external source excerpt differs from pinned Git lines: {packet_path}")

    return PacketInspection(
        packet_root=str(packet_root),
        manifest_path=str(manifest_path),
        manifest_sha256=sha256(manifest_bytes),
        repository=(
            f"{_string(repo.get('owner'), 'manifest repository owner')}/"
            f"{_string(repo.get('name'), 'manifest repository name')}"
        ),
        issue_number=cast(int, issue["number"]),
        pull_number=cast(int, pull["number"]),
        head=head,
        base=base,
        merge_base=merge_base,
        file_paths=tuple(sorted(records)),
        changed_paths=changed_paths,
        external_source_count=len(external_sources),
        full_read_paths=tuple(sorted(full_read_paths)),
        review_ranges=review_ranges,
    )


def _kilo_worktree_root(workspace: Path) -> Path:
    if workspace.is_symlink() or not workspace.is_dir():
        raise PacketError("standalone Kilo workspace must be a real directory")
    workspace_root = workspace.resolve(strict=True)
    filesystem_root = Path(workspace_root.anchor)
    current = workspace_root
    while current != filesystem_root:
        git_marker = current / ".git"
        if git_marker.exists() or git_marker.is_symlink():
            raise PacketError("standalone Kilo workspace must not be inside a Git worktree")
        current = current.parent
    # Kilo 7.8.8 uses the filesystem root as worktree for a non-VCS global project.
    return filesystem_root


def _kilo_permission_pattern(worktree_root: Path, absolute_path: Path) -> str:
    pattern = str(absolute_path.relative_to(worktree_root))
    if any(char in pattern for char in "*?[]\\"):
        raise PacketError("standalone workspace path contains Kilo permission glob syntax")
    return pattern


def _kilo_packet_path_maps(
    packet_paths: tuple[str, ...] | list[str], *, workspace: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    safe_paths = tuple(sorted({_relative_file(path) for path in packet_paths}))
    if not safe_paths or len(safe_paths) != len(packet_paths):
        raise PacketError("standalone profile requires a unique nonempty exact-file list")
    worktree_root = _kilo_worktree_root(workspace)
    workspace_root = workspace.resolve(strict=True)
    packet_root = workspace_root / "packet"
    if packet_root.is_symlink() or not packet_root.is_dir():
        raise PacketError("standalone profile packet root must be a real directory")
    packet_root = packet_root.resolve(strict=True)
    if packet_root.parent != workspace_root:
        raise PacketError("standalone profile packet root escapes its workspace")
    absolute_paths: dict[str, str] = {}
    permission_patterns: dict[str, str] = {}
    for filename in safe_paths:
        target = packet_root / filename
        try:
            info = target.lstat()
        except OSError as err:
            raise PacketError(f"standalone packet allow path is unavailable: {filename}") from err
        if not stat.S_ISREG(info.st_mode) or target.is_symlink() or info.st_nlink != 1:
            raise PacketError(
                f"standalone packet allow path is linked or not a regular file: {filename}"
            )
        absolute = target.resolve(strict=True)
        if absolute.parent != packet_root:
            raise PacketError(f"standalone packet allow path escapes its root: {filename}")
        packet_path = f"packet/{filename}"
        absolute_paths[packet_path] = str(absolute)
        permission_patterns[packet_path] = _kilo_permission_pattern(worktree_root, absolute)
    return absolute_paths, permission_patterns


def make_per_run_permission_map(
    packet_paths: tuple[str, ...] | list[str], *, workspace: Path,
) -> dict[str, object]:
    """Allow only exact Kilo worktree-relative Read patterns for one packet."""
    _, permission_patterns = _kilo_packet_path_maps(packet_paths, workspace=workspace)
    read_rules: dict[str, str] = {"*": "deny"}
    read_rules.update({pattern: "allow" for pattern in permission_patterns.values()})
    return {
        "*": "deny",
        "read": read_rules,
        "external_directory": {"*": "deny"},
    }


def _render_per_run_standalone_profile(
    canonical_profile: Path, packet_paths: tuple[str, ...] | list[str], *, workspace: Path,
) -> bytes:
    _, description, prompt = _profile_parts(canonical_profile)
    permission = make_per_run_permission_map(packet_paths, workspace=workspace)
    read_rules = cast(dict[str, str], permission["read"])
    lines = [
        f"description: {json.dumps(description)}",
        "mode: primary",
        "permission:",
        '  "*": deny',
        "  read:",
        '    "*": deny',
    ]
    lines.extend(
        f"    {json.dumps(path)}: allow"
        for path, action in sorted(read_rules.items())
        if path != "*" and action == "allow"
    )
    lines.extend(("  external_directory:", '    "*": deny'))
    return ("\n".join(("---", *lines, "---", "", prompt, ""))).encode("utf-8")


def _profile_parts(profile_path: Path) -> tuple[bytes, str, str]:
    try:
        raw = profile_path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise PacketError("canonical standalone reviewer profile is unavailable or not UTF-8") from err
    parts = text.split("---", 2)
    if len(parts) != 3 or parts[0].strip():
        raise PacketError("canonical reviewer profile frontmatter is malformed")
    frontmatter = parts[1]
    if not re.search(r"(?m)^mode:\s*primary\s*$", frontmatter):
        raise PacketError("standalone reviewer mode must be primary")
    if not re.search(r"(?m)^permission:\s*\n\s+\"\*\":\s*deny\s*$", frontmatter):
        raise PacketError("standalone reviewer static permissions must deny all tools")
    if not re.search(r"(?m)^\s+read:\s*deny\s*$", frontmatter):
        raise PacketError("canonical standalone reviewer Read must deny without a generated profile")
    if re.search(r"(?m)^\s+[^#\n]+:\s*allow\s*$", frontmatter):
        raise PacketError("standalone reviewer frontmatter contains a static allow")
    description = re.search(r"(?m)^description:\s*(.+?)\s*$", frontmatter)
    if description is None:
        raise PacketError("canonical reviewer description is missing")
    return raw, description.group(1), parts[2].lstrip("\n").rstrip()


def _verify_overlay_files(runtime: RuntimeWorkspace) -> None:
    root = runtime.profile_config
    if not root.is_dir() or root.is_symlink() or stat.S_IMODE(root.stat().st_mode) != 0o555:
        raise PacketError("per-run Kilo config directory is missing, linked or writable")
    expected = {
        ".gitignore", "kilo.json", "agents", "agents/pr-reviewer-standalone.md"
    }
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*")}
    if actual != expected:
        raise PacketError("per-run Kilo config directory contains missing or unexpected entries")
    agents = root / "agents"
    if (not agents.is_dir() or agents.is_symlink()
            or stat.S_IMODE(agents.stat().st_mode) != 0o555):
        raise PacketError("per-run Kilo agents directory is missing, linked or writable")
    expected_hashes = {
        "agents/pr-reviewer-standalone.md": runtime.overlay_profile_sha256,
        "kilo.json": runtime.overlay_config_sha256,
    }
    for relative, expected_sha in expected_hashes.items():
        path = root / relative
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o444 or sha256(path.read_bytes()) != expected_sha):
            raise PacketError(f"per-run Kilo config file is linked, writable or changed: {relative}")
    if (root / "kilo.json").read_bytes() != runtime.kilo_config_content.encode("utf-8"):
        raise PacketError("per-run Kilo config file differs from the exact environment content")
    if (root / ".gitignore").read_bytes() != b"# per-run Kilo reviewer configuration\n":
        raise PacketError("per-run Kilo config ignore file changed")
    canonical_profile_bytes = runtime.canonical_profile.read_bytes()
    if sha256(canonical_profile_bytes) != runtime.canonical_profile_sha256:
        raise PacketError("canonical standalone reviewer profile changed after preparation")
    if runtime.profile_source_root is not None:
        _verify_profile_git_binding(
            runtime.canonical_profile, runtime.profile_source_root, runtime.inspection.head
        )
    profile_path = root / "agents/pr-reviewer-standalone.md"
    profile_text = profile_path.read_text(encoding="utf-8")
    profile_parts = profile_text.split("---", 2)
    packet_paths = tuple(path.removeprefix("packet/") for path in runtime.allowed_paths)
    expected_profile = _render_per_run_standalone_profile(
        runtime.canonical_profile, packet_paths, workspace=runtime.workspace
    )
    if (len(profile_parts) != 3 or profile_parts[0].strip()
            or profile_path.read_bytes() != expected_profile
            or sha256(expected_profile) != runtime.overlay_profile_sha256
            or sha256(profile_parts[2].lstrip("\n").rstrip().encode("utf-8"))
            != runtime.profile_prompt_sha256):
        raise PacketError("per-run reviewer profile differs from the exact prompt and packet rules")


def _write_probe(path: Path, content: bytes) -> str:
    if path.exists() or path.is_symlink() or not path.parent.is_dir():
        raise PacketError("synthetic preflight probe path already exists or has no parent")
    path.write_bytes(content)
    os.chmod(path, 0o444)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PacketError("synthetic preflight probe is not a single-link regular file")
    return sha256(content)


def _verify_probe_inventory(runtime: RuntimeWorkspace) -> None:
    if not runtime.denied_paths:
        if (runtime.probe_root is not None or runtime.probe_hashes or runtime.probe_symlinks
                or runtime.probe_labels):
            raise PacketError("review workspace unexpectedly carries synthetic probe state")
        return
    if runtime.probe_root is None or runtime.probe_root.is_symlink() or not runtime.probe_root.is_dir():
        raise PacketError("synthetic escape probes are missing from their isolated parent")
    for raw_path, expected_hash in runtime.probe_hashes.items():
        path = Path(raw_path)
        info = path.lstat()
        if (path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o444 or sha256(path.read_bytes()) != expected_hash):
            raise PacketError("synthetic escape canary changed type, mode, link count or bytes")
    for raw_path, expected_target in runtime.probe_symlinks.items():
        path = Path(raw_path)
        if not path.is_symlink() or os.readlink(path) != expected_target:
            raise PacketError("synthetic escape symlink changed target")
    if (set(runtime.probe_labels) != set(runtime.denied_paths)
            or len(set(runtime.probe_labels.values())) != len(runtime.probe_labels)):
        raise PacketError("synthetic escape probe labels are incomplete or duplicated")
    for raw_path in runtime.denied_paths:
        path = Path(raw_path)
        requested = path if path.is_absolute() else runtime.workspace / path
        requested = Path(os.path.abspath(requested))
        try:
            requested.lstat()
        except OSError as err:
            raise PacketError("synthetic escape target is not present for the runtime test") from err
        if requested.parent.resolve().is_relative_to(runtime.workspace.resolve()):
            raise PacketError("synthetic escape probe target is inside the reviewer workspace")


def prepare_standalone_environment(
    packet_root: Path,
    manifest_path: Path,
    canonical_profile: Path,
    workspace_parent: Path,
    overlay_parent: Path,
    *,
    source_root: Path | None = None,
    profile_source_root: Path | None = None,
    external_source_roots: dict[str, Path] | None = None,
    preflight: bool = False,
) -> RuntimeWorkspace:
    """Prepare a packet-only workspace and standalone primary profile config.

    This function prepares files/configuration only; it never starts Kilo.
    """
    inspection = validate_packet(
        packet_root,
        manifest_path,
        source_root=source_root,
        external_source_roots=external_source_roots,
    )
    if source_root is not None:
        validate_standalone_packet_path_plan(inspection)
    for label, directory in (("workspace", workspace_parent), ("overlay", overlay_parent)):
        if (not directory.is_dir() or directory.is_symlink()
                or stat.S_IMODE(directory.stat().st_mode) & 0o077):
            raise PacketError(f"{label} parent must be a private existing real directory")
    if source_root is not None:
        source_checkout = source_root.resolve()
        if (workspace_parent.resolve().is_relative_to(source_checkout)
                or overlay_parent.resolve().is_relative_to(source_checkout)):
            raise PacketError("review workspace and Kilo config must be outside the source checkout")
    profile_bytes, _, prompt = _profile_parts(canonical_profile)
    if source_root is not None:
        canonical_profile_relative = ".kilo/agents/pr-reviewer-standalone.md"
        frozen_profile = _git(source_root, "show", f"{inspection.head}:{canonical_profile_relative}")
        if profile_bytes != frozen_profile:
            raise PacketError("effective reviewer source file differs from frozen HEAD")
        profile_binding = _object(
            _object(_object(json.loads(manifest_path.read_bytes()), "manifest").get("source_bindings"),
                    "manifest.source_bindings").get(canonical_profile_relative),
            "reviewer profile source binding",
        )
        if _git(source_root, "rev-parse", f"{inspection.head}:{canonical_profile_relative}").decode().strip() != profile_binding.get("head_blob"):
            raise PacketError("canonical reviewer profile Git blob differs from packet binding")
    if profile_source_root is None:
        profile_source_root = source_root
    if profile_source_root is not None:
        _verify_profile_git_binding(canonical_profile, profile_source_root, inspection.head)

    run_container = Path(tempfile.mkdtemp(prefix="sr-standalone-run-", dir=workspace_parent))
    os.chmod(run_container, 0o700)
    workspace = run_container / "workspace"
    workspace.mkdir(mode=0o700)
    packet = workspace / "packet"
    packet.mkdir(mode=0o700)
    for name in inspection.file_paths:
        destination = packet / name
        shutil.copyfile(packet_root / name, destination)
        os.chmod(destination, 0o444)
    os.chmod(packet, 0o555)

    denied_paths: list[str] = []
    probe_hashes: dict[str, str] = {}
    probe_symlinks: dict[str, str] = {}
    probe_labels: dict[str, str] = {}
    probe_root: Path | None = None
    if preflight:
        probe_root = run_container
        suffix = workspace.name
        sibling = run_container / f"{suffix}-sibling.txt"
        traversal = run_container / f"{suffix}-traversal.txt"
        absolute = run_container / f"{suffix}-absolute.txt"
        outside_target = run_container / f"{suffix}-outside-target.txt"
        parent = workspace_parent / f"{run_container.name}-parent.txt"
        symlink_packet = run_container / f"{suffix}-symlink-packet.txt"
        symlink_outside = run_container / f"{suffix}-symlink-outside.txt"
        for path, content in (
            (sibling, b"SYNTHETIC DENIED PROBE: sibling.\n"),
            (traversal, b"SYNTHETIC DENIED PROBE: traversal.\n"),
            (absolute, b"SYNTHETIC DENIED PROBE: absolute.\n"),
            (outside_target, b"SYNTHETIC DENIED PROBE: symlink target.\n"),
            (parent, b"SYNTHETIC DENIED PROBE: parent.\n"),
        ):
            probe_hashes[str(path)] = _write_probe(path, content)
        symlink_packet_target = f"{workspace.name}/packet/index.md"
        symlink_outside_target = outside_target.name
        if symlink_packet.exists() or symlink_outside.exists():
            raise PacketError("synthetic preflight symlink path already exists")
        symlink_packet.symlink_to(symlink_packet_target)
        symlink_outside.symlink_to(symlink_outside_target)
        probe_symlinks[str(symlink_packet)] = symlink_packet_target
        probe_symlinks[str(symlink_outside)] = symlink_outside_target
        denied_paths = [
            f"../{sibling.name}",
            f"../../{parent.name}",
            str(absolute),
            f"packet/../../{traversal.name}",
            f"../{symlink_packet.name}",
            f"../{symlink_outside.name}",
        ]
        probe_labels = dict(zip(
            denied_paths,
            ("sibling", "parent", "absolute", "traversal", "symlink_to_packet", "symlink_to_outside"),
            strict=True,
        ))
    os.chmod(workspace, 0o555)

    allowed_paths = (
        "packet/index.md",
        *(f"packet/{path}" for path in inspection.file_paths if path != "index.md"),
    )
    per_run_profile = _render_per_run_standalone_profile(
        canonical_profile,
        tuple(path.removeprefix("packet/") for path in allowed_paths),
        workspace=workspace,
    )
    kilo_config_content = "{}"
    kilo_config_bytes = kilo_config_content.encode("utf-8")

    overlay = Path(tempfile.mkdtemp(prefix="sr-standalone-kilo-config-", dir=overlay_parent))
    os.chmod(overlay, 0o700)
    agents = overlay / "agents"
    agents.mkdir(mode=0o700)
    (overlay / ".gitignore").write_text("# per-run Kilo reviewer configuration\n", encoding="utf-8")
    os.chmod(overlay / ".gitignore", 0o444)
    overlay_profile = agents / "pr-reviewer-standalone.md"
    overlay_profile.write_bytes(per_run_profile)
    os.chmod(overlay_profile, 0o444)
    overlay_config = overlay / "kilo.json"
    overlay_config.write_bytes(kilo_config_bytes)
    os.chmod(overlay_config, 0o444)
    os.chmod(agents, 0o555)
    os.chmod(overlay, 0o555)
    runtime = RuntimeWorkspace(
        workspace=workspace,
        packet=packet,
        inspection=inspection,
        external_source_roots=external_source_roots or {},
        canonical_profile=canonical_profile,
        profile_source_root=profile_source_root,
        profile_config=overlay,
        probe_root=probe_root,
        probe_hashes=probe_hashes,
        probe_symlinks=probe_symlinks,
        probe_labels=probe_labels,
        allowed_paths=allowed_paths,
        denied_paths=tuple(denied_paths),
        profile_prompt_sha256=sha256(prompt.encode()),
        canonical_profile_sha256=sha256(profile_bytes),
        overlay_profile_sha256=sha256(per_run_profile),
        overlay_config_sha256=sha256(kilo_config_bytes),
        kilo_config_content=kilo_config_content,
    )
    _verify_overlay_files(runtime)
    _verify_probe_inventory(runtime)
    _verify_workspace_copy(runtime)
    return runtime


def _permission_rules(agent_info: dict[str, Any]) -> list[dict[str, Any]]:
    raw = agent_info.get("permission")
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise PacketError("Kilo debug agent output has no usable permission rules")
    return cast(list[dict[str, Any]], raw)


def _wildcard_match(value: str, pattern: str) -> bool:
    normalized_value = value.replace("\\", "/")
    normalized_pattern = re.escape(pattern.replace("\\", "/")).replace(r"\ ", " ")
    normalized_pattern = normalized_pattern.replace(r"\*", ".*").replace(r"\?", ".")
    if normalized_pattern.endswith(" .*"):
        normalized_pattern = normalized_pattern[:-3] + "( .*)?"
    flags = re.DOTALL | (re.IGNORECASE if os.name == "nt" else 0)
    return re.fullmatch(normalized_pattern, normalized_value, flags=flags) is not None


def _rule_matches(item: dict[str, Any], permission: str, pattern: str) -> bool:
    rule_permission = item.get("permission")
    rule_pattern = item.get("pattern")
    if not isinstance(rule_permission, str) or not isinstance(rule_pattern, str):
        raise PacketError("Kilo permission rule is missing a string permission or pattern")
    return _wildcard_match(permission, rule_permission) and _wildcard_match(pattern, rule_pattern)


def _tool_map(agent_info: dict[str, Any], expected: set[str]) -> None:
    raw = agent_info.get("tools")
    if not isinstance(raw, dict) or any(
        not isinstance(name, str) or not isinstance(enabled, bool)
        for name, enabled in raw.items()
    ):
        raise PacketError("Kilo debug agent output has no usable effective tool map")
    enabled_tools = {name for name, enabled in raw.items() if enabled is True}
    if enabled_tools != expected:
        raise PacketError(f"effective tool set differs: expected {sorted(expected)}")


def _effective_rule(
    rules: list[dict[str, Any]], permission: str, pattern: str
) -> dict[str, Any] | None:
    matches = [item for item in rules if _rule_matches(item, permission, pattern)]
    return matches[-1] if matches else None


def _expect_permission(
    rules: list[dict[str, Any]], permission: str, pattern: str, action: str, *, exact: bool = False
) -> dict[str, Any]:
    winner = _effective_rule(rules, permission, pattern)
    if winner is None or winner.get("action") != action:
        raise PacketError(f"Kilo effective permission is not {action}: {permission} {pattern}")
    if exact and winner.get("pattern") != pattern:
        raise PacketError(f"Kilo did not select an exact rule for {permission} {pattern}")
    return winner


def _debug_agent(
    kilo_binary: Path, agent_name: str, workspace: Path, env: dict[str, str]
) -> dict[str, Any]:
    result = subprocess.run(
        [str(kilo_binary), "--pure", "debug", "agent", agent_name],
        cwd=workspace,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode:
        raise PacketError(f"Kilo could not resolve effective permissions for {agent_name}")
    try:
        return _object(json.loads(result.stdout), f"Kilo debug agent {agent_name}")
    except (json.JSONDecodeError, PacketError) as err:
        raise PacketError(f"Kilo debug agent output is not valid JSON for {agent_name}") from err


def _minimal_cli_path(kilo_binary: Path) -> str:
    entries = [str(kilo_binary.parent)]
    node = shutil.which("node")
    if node is not None:
        entries.append(str(Path(node).parent))
    entries.extend(os.defpath.split(os.pathsep))
    return os.pathsep.join(dict.fromkeys(entries))


def verify_effective_standalone_agent(
    reviewer: dict[str, Any], overlay: RuntimeWorkspace,
    canonical_profile: Path,
) -> dict[str, Any]:
    """Check the Kilo v7.8.8 standalone primary profile and exact Read rules."""
    if reviewer.get("name") != "pr-reviewer-standalone":
        raise PacketError("effective Kilo reviewer identity mismatch or default-agent fallback")
    resolved_mode = _string(reviewer.get("mode"), "resolved reviewer mode")
    if resolved_mode not in {"primary", "all"}:
        raise PacketError("standalone reviewer resolves as a subagent and Kilo run would fall back")
    _tool_map(reviewer, {"read"})
    reviewer_rules = _permission_rules(reviewer)
    for tool in (*_FORBIDDEN_TOOLS, *_MCP_PROBES):
        _expect_permission(reviewer_rules, tool, "*", "deny")

    permission = make_per_run_permission_map(
        [Path(path).name for path in overlay.allowed_paths], workspace=overlay.workspace
    )
    expected_read_patterns = cast(dict[str, str], permission["read"])
    allowed_read_patterns = sorted(
        pattern for pattern, action in expected_read_patterns.items()
        if pattern != "*" and action == "allow"
    )
    if len(allowed_read_patterns) != len(overlay.allowed_paths):
        raise PacketError("Kilo worktree-relative Read patterns are not one-to-one with packet files")
    for pattern in allowed_read_patterns:
        _expect_permission(reviewer_rules, "read", pattern, "allow", exact=True)
    for path in overlay.denied_paths:
        _expect_permission(reviewer_rules, "read", path, "deny")
        candidate = Path(path)
        resolved = Path(os.path.abspath(
            candidate if candidate.is_absolute() else overlay.workspace / candidate
        ))
        if not resolved.is_relative_to(overlay.workspace):
            _expect_permission(reviewer_rules, "external_directory", str(resolved), "deny")
    _expect_permission(reviewer_rules, "read", "packet/not-indexed-canary.txt", "deny")

    _, _, expected_prompt = _profile_parts(canonical_profile)
    prompt = reviewer.get("prompt")
    if not isinstance(prompt, str) or prompt.rstrip() != expected_prompt:
        raise PacketError("effective reviewer prompt differs from the frozen canonical profile")
    source_sha = sha256(canonical_profile.read_bytes())
    prompt_sha = sha256(prompt.encode())
    overlay_profile_sha = sha256(
        (overlay.profile_config / "agents/pr-reviewer-standalone.md").read_bytes()
    )
    if (source_sha != overlay.canonical_profile_sha256
            or overlay_profile_sha != overlay.overlay_profile_sha256
            or prompt_sha != sha256(expected_prompt.encode())):
        raise PacketError("per-run reviewer prompt/profile does not match the canonical source and overlay")
    return {
        "status": "EFFECTIVE_STANDALONE_PERMISSION_PASS",
        "reviewer_agent": "pr-reviewer-standalone",
        "reviewer_profile_mode": "primary",
        "reviewer_resolved_mode": resolved_mode,
        "reviewer_effective_tools": ["read"],
        "profile_source_sha256": source_sha,
        "effective_prompt_sha256": prompt_sha,
        "overlay_profile_sha256": overlay_profile_sha,
        "overlay_config_sha256": overlay.overlay_config_sha256,
        "allowed_packet_paths": list(overlay.allowed_paths),
        "allowed_read_permission_patterns": allowed_read_patterns,
        "denied_probe_paths": list(overlay.denied_paths),
        "permission_config_sha256": sha256(overlay.kilo_config_content.encode()),
        "os_level_isolation_claimed": False,
        "review_session_started": False,
    }


def verify_effective_standalone_permissions(
    *,
    kilo_binary: Path,
    overlay: RuntimeWorkspace,
    canonical_profile: Path,
    home: Path,
    tmpdir: Path,
    config_home: Path,
    data_home: Path,
    state_home: Path,
    cache_home: Path,
    expected_version: str = "7.8.8",
) -> dict[str, Any]:
    """Resolve the standalone primary before any reviewer session is started."""
    _verify_overlay_files(overlay)
    if not overlay.workspace.is_dir() or overlay.workspace.is_symlink():
        raise PacketError("reviewer workspace is missing or linked")
    if any(not path.is_dir() or path.is_symlink()
           for path in (home, tmpdir, config_home, data_home, state_home, cache_home)):
        raise PacketError("Kilo HOME/XDG/cache/temp paths must exist and be real directories")
    env = standalone_environment(
        kilo_binary=kilo_binary,
        profile_config=overlay.profile_config,
        kilo_config_content=overlay.kilo_config_content,
        home=home,
        tmpdir=tmpdir,
        config_home=config_home,
        data_home=data_home,
        state_home=state_home,
        cache_home=cache_home,
    )
    version = subprocess.run(
        [str(kilo_binary), "--version"], cwd=overlay.workspace, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, timeout=60, check=False)
    if version.returncode or version.stdout.strip() != expected_version:
        raise PacketError("installed Kilo version does not match pinned permission evidence")

    reviewer = _debug_agent(kilo_binary, "pr-reviewer-standalone", overlay.workspace, env)
    result = verify_effective_standalone_agent(reviewer, overlay, canonical_profile)
    return {"kilo_version": expected_version, **result}


def _tool_calls(session: dict[str, Any]) -> list[dict[str, Any]]:
    messages = _array(session.get("messages"), "session messages")
    result: list[dict[str, Any]] = []
    for raw_message in messages:
        message = _object(raw_message, "session message")
        for raw_part in _array(message.get("parts"), "message parts"):
            part = _object(raw_part, "message part")
            if part.get("type") == "tool":
                result.append(part)
    return result


def _read_lines(output: str) -> dict[int, str]:
    start = output.find("<content>\n")
    end = output.find("\n</content>", start + 10)
    if start < 0 or end < 0:
        raise PacketError("successful Read omitted a delimited content block")
    block = output[start + len("<content>\n"):end]
    result: dict[int, str] = {}
    for line in block.splitlines():
        match = re.match(r"^(\d+): (.*)$", line)
        if match:
            result[int(match.group(1))] = match.group(2)
    return result


def _expected_line_map(path: Path) -> dict[int, str]:
    return {index: line for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)}


def _verify_workspace_copy(runtime: RuntimeWorkspace) -> str:
    source_packet = Path(runtime.inspection.packet_root)
    manifest_path = Path(runtime.inspection.manifest_path)
    current = validate_packet(
        source_packet,
        manifest_path,
        external_source_roots=runtime.external_source_roots,
        require_external_sources=True,
    )
    if current.manifest_sha256 != runtime.inspection.manifest_sha256:
        raise PacketError("source packet manifest changed while the standalone reviewer was active")
    workspace_info = runtime.workspace.lstat()
    if (runtime.workspace.is_symlink() or not stat.S_ISDIR(workspace_info.st_mode)
            or stat.S_IMODE(workspace_info.st_mode) != 0o555):
        raise PacketError("standalone reviewer workspace changed type or mode")
    if runtime.packet.is_symlink() or stat.S_IMODE(runtime.packet.stat().st_mode) != 0o555:
        raise PacketError("review workspace packet directory changed mode/type")

    hashes: list[tuple[str, str]] = []
    for name in current.file_paths:
        source = source_packet / name
        copied = runtime.packet / name
        info = copied.lstat()
        if (copied.is_symlink() or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o444):
            raise PacketError(f"review workspace packet entry changed type/mode/link: {name}")
        if copied.read_bytes() != source.read_bytes():
            raise PacketError(f"review workspace packet bytes changed during standalone review: {name}")
        hashes.append((name, sha256(copied.read_bytes())))

    if {item.name for item in runtime.workspace.iterdir()} != {"packet"}:
        raise PacketError("standalone reviewer workspace contains an entry besides the packet")
    packet_entries = {item.name: item for item in runtime.packet.iterdir()}
    if set(packet_entries) != set(current.file_paths):
        raise PacketError("review workspace packet contains an unexpected or missing entry")

    _verify_probe_inventory(runtime)

    return sha256(json.dumps(sorted(hashes), separators=(",", ":")).encode())


def _absolute_packet_path_map(runtime: RuntimeWorkspace) -> dict[str, str]:
    absolute_paths, _ = _kilo_packet_path_maps(
        [Path(path).name for path in runtime.allowed_paths], workspace=runtime.workspace
    )
    return absolute_paths


def _kilo_permission_path_map(runtime: RuntimeWorkspace) -> dict[str, str]:
    _, permission_patterns = _kilo_packet_path_maps(
        [Path(path).name for path in runtime.allowed_paths], workspace=runtime.workspace
    )
    return permission_patterns


def standalone_review_prompt(runtime: RuntimeWorkspace) -> str:
    """Build a context-minimal prompt; the indexed packet is the only PR evidence."""
    inspection = runtime.inspection
    if not all(_SHA1.fullmatch(value) for value in (
        inspection.head, inspection.base, inspection.merge_base,
    )):
        raise PacketError("standalone review prompt requires exact repository identity and refs")
    paths = "\n".join(
        f"- `{absolute}` (`{packet_path}`)"
        for packet_path, absolute in _absolute_packet_path_map(runtime).items()
    )
    return (
        "Review the complete frozen pull request independently using only `packet/index.md` "
        "and its indexed packet files. Kilo's native Read `filePath` requires an absolute path. "
        "Use only the exact absolute paths listed below; do not construct or alter file paths. "
        "Read the index first, then inspect every required file/range yourself. Do not infer "
        "evidence from the primary or outside this packet.\n"
        f"Exact native Read filePath values:\n{paths}\n\n"
        f"Repository: {inspection.repository}\nIssue: #{inspection.issue_number}\n"
        f"PR: #{inspection.pull_number}\nHEAD: {inspection.head}\n"
        f"Base: {inspection.base}\nMerge-base: {inspection.merge_base}\n\n"
        "Assess correctness, regressions, architecture, security/privacy, UX and tests by "
        "source inspection. Do not run tests or claim direct Forgejo/Git verification. "
        "Return only the exact JSON result contract in your reviewer profile."
    )


def standalone_run_argv(
    kilo_binary: Path,
    model_binding: dict[str, str],
    title: str,
    prompt: str,
) -> list[str]:
    provider = _string(model_binding.get("provider_id"), "reviewer provider")
    model = _string(model_binding.get("model_id"), "reviewer model")
    variant = _string(model_binding.get("variant"), "reviewer variant")
    if (not title.strip() or not prompt.strip()
            or any(char.isspace() or char == "/" for char in provider)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?", model)
            or any(char.isspace() for char in variant)):
        raise PacketError("standalone CLI invocation binding/title/prompt is malformed")
    return [
        str(kilo_binary.resolve()), "--pure", "run", "--agent", "pr-reviewer-standalone",
        "--model", f"{provider}/{model}", "--variant", variant,
        "--format", "json", "--title", title, prompt,
    ]


def standalone_preflight_prompt(runtime: RuntimeWorkspace) -> str:
    if not runtime.denied_paths or set(runtime.probe_labels) != set(runtime.denied_paths):
        raise PacketError("synthetic standalone preflight requires the complete labeled escape set")
    allowed_paths = "\n".join(
        f"- `{absolute}` (`{packet_path}`)"
        for packet_path, absolute in _absolute_packet_path_map(runtime).items()
    )
    denied_paths = "\n".join(
        f"- `{path}` ({runtime.probe_labels[path]})" for path in runtime.denied_paths
    )
    return (
        "Synthetic permission preflight only; this is not a PR review and consumes no review "
        "ordinal. Kilo's native Read `filePath` requires an absolute path. Read `packet/index.md` "
        "first using its exact path below, then read every other indexed packet file exactly once "
        "using the corresponding listed absolute path and no partial ranges. In `allowed_paths`, "
        "report the canonical relative `packet/<file>` names shown in parentheses.\n"
        f"Exact allowed native Read filePath values:\n{allowed_paths}\n"
        "Then attempt one native Read for each of these denied paths, in order, using the exact "
        "path string shown:\n"
        f"{denied_paths}\n"
        "Do not enumerate, search, delegate, retry, or use another tool. Do not repeat any "
        "synthetic canary value. Return only JSON with keys `preflight`, `allowed_paths`, "
        "`complete_read_count`, `probe_results`, and `unauthorized_bytes_returned`. Each probe "
        "result must contain its exact `path`, `result` equal to `DENIED_BEFORE_CONTENT`, and "
        "`bytes_returned` equal to false. Set `preflight` to PASS only if every allowed read "
        "was complete and every listed probe was denied before content."
    )


def _final_assistant_text(session: dict[str, Any], agent_name: str) -> str:
    messages = _array(session.get("messages"), "session messages")
    candidates: list[str] = []
    for raw_message in messages:
        message = _object(raw_message, "session message")
        info = _object(message.get("info"), "session message info")
        if info.get("role") != "assistant":
            continue
        if info.get("agent") != agent_name:
            raise PacketError("standalone session contains a different assistant identity")
        texts = [
            _string(_object(part, "assistant text part").get("text"), "assistant text")
            for part in _array(message.get("parts"), "assistant parts")
            if _object(part, "assistant part").get("type") == "text"
        ]
        if texts:
            candidates.append("".join(texts))
    if not candidates:
        raise PacketError("standalone session has no final reviewer response")
    return candidates[-1].strip()


def _user_prompt(session: dict[str, Any]) -> str:
    messages = _array(session.get("messages"), "session messages")
    user_messages = []
    for raw_message in messages:
        message = _object(raw_message, "session message")
        info = _object(message.get("info"), "session message info")
        if info.get("role") == "user":
            user_messages.append(message)
    if len(user_messages) != 1:
        raise PacketError("standalone reviewer must start from exactly one fresh user prompt")
    parts = _array(user_messages[0].get("parts"), "initial user message parts")
    if any(_object(part, "user prompt part").get("type") != "text" for part in parts):
        raise PacketError("standalone reviewer initial input contains non-text context")
    return "".join(
        _string(_object(part, "user prompt part").get("text"), "user prompt text")
        for part in parts
    )


def _validate_review_result(text: str, inspection: PacketInspection) -> dict[str, Any]:
    try:
        result = _object(json.loads(text), "standalone reviewer result")
    except (json.JSONDecodeError, PacketError) as err:
        raise PacketError("standalone reviewer result is not a single JSON object") from err
    required = {
        "reviewed_head", "reviewed_base", "reviewed_merge_base", "verdict", "findings",
        "limitations", "checks_run", "ux_impact",
    }
    if set(result) != required:
        raise PacketError("standalone reviewer result has missing or unexpected fields")
    if (result.get("reviewed_head") != inspection.head
            or result.get("reviewed_base") != inspection.base
            or result.get("reviewed_merge_base") != inspection.merge_base):
        raise PacketError("standalone reviewer result does not bind the exact frozen refs")
    verdict = result.get("verdict")
    if verdict not in {"APPROVE", "REQUEST_CHANGES", "COMMENT"}:
        raise PacketError("standalone reviewer result has an invalid verdict")
    findings = _array(result.get("findings"), "reviewer findings")
    changed_paths = set(inspection.changed_paths)
    finding_fields = {
        "severity", "file", "line_start", "line_end", "evidence", "consequence",
        "required_remediation",
    }
    for raw_finding in findings:
        finding = _object(raw_finding, "reviewer finding")
        if set(finding) != finding_fields:
            raise PacketError("standalone reviewer finding has missing or unexpected fields")
        if finding.get("severity") not in {"P0", "P1", "P2", "P3"}:
            raise PacketError("standalone reviewer finding has an invalid severity")
        if finding.get("file") not in changed_paths:
            raise PacketError("standalone reviewer finding is not bound to a changed PR path")
        start = finding.get("line_start")
        end = finding.get("line_end")
        if (not isinstance(start, int) or isinstance(start, bool) or start < 1
                or not isinstance(end, int) or isinstance(end, bool) or end < start):
            raise PacketError("standalone reviewer finding has an invalid line range")
        for field in ("evidence", "consequence", "required_remediation"):
            if not isinstance(finding.get(field), str) or not finding[field].strip():
                raise PacketError(f"standalone reviewer finding has no {field}")
    limitations = _array(result.get("limitations"), "reviewer limitations")
    if any(not isinstance(item, str) or not item.strip() for item in limitations):
        raise PacketError("standalone reviewer limitation must be a nonempty string")
    if result.get("checks_run") != []:
        raise PacketError("read-only reviewer must not claim that it executed checks")
    ux_impact = result.get("ux_impact")
    if not isinstance(ux_impact, str) or not ux_impact.strip():
        raise PacketError("standalone reviewer result omits the required UX assessment")
    if verdict == "APPROVE" and (findings or limitations):
        raise PacketError("APPROVE requires empty findings and limitations")
    if verdict == "REQUEST_CHANGES" and not findings:
        raise PacketError("REQUEST_CHANGES requires an actionable finding")
    return result


def _validate_preflight_result(text: str, runtime: RuntimeWorkspace) -> dict[str, Any]:
    try:
        result = _object(json.loads(text), "standalone preflight result")
    except (json.JSONDecodeError, PacketError) as err:
        raise PacketError("standalone preflight response is not one JSON object") from err
    if set(result) != {
        "preflight", "allowed_paths", "complete_read_count", "probe_results",
        "unauthorized_bytes_returned",
    }:
        raise PacketError("standalone preflight result has missing or unexpected fields")
    if (result.get("preflight") != "PASS"
            or result.get("allowed_paths") != list(runtime.allowed_paths)
            or result.get("complete_read_count") != len(runtime.allowed_paths)
            or result.get("unauthorized_bytes_returned") is not False):
        raise PacketError("standalone preflight did not report the exact complete packet and denial result")
    probes = _array(result.get("probe_results"), "standalone preflight probes")
    if len(probes) != len(runtime.denied_paths):
        raise PacketError("standalone preflight result omits an escape probe")
    expected = [
        {"path": path, "result": "DENIED_BEFORE_CONTENT", "bytes_returned": False}
        for path in runtime.denied_paths
    ]
    if probes != expected:
        raise PacketError("standalone preflight result misreports an escape probe")
    return result


def summarize_failed_standalone_session(
    session: dict[str, Any],
    *,
    session_export_sha256: str,
    reason: str,
    preflight: bool,
) -> dict[str, Any]:
    """Persist a content-filtered trace when the independent audit fails closed."""
    info = _object(session.get("info"), "failed standalone session info")
    model_info = info.get("model")
    model_record: dict[str, str] = {}
    if isinstance(model_info, dict):
        for key in ("providerID", "id", "modelID", "variant"):
            value = model_info.get(key)
            if isinstance(value, str):
                model_record[key] = value
    calls = _tool_calls(session)
    tool_trace: list[dict[str, object]] = []
    for call in calls:
        state = _object(call.get("state"), "failed standalone tool state")
        request = state.get("input")
        path = request.get("filePath") if isinstance(request, dict) else None
        metadata = state.get("metadata")
        approval = metadata.get("approval") if isinstance(metadata, dict) else None
        rule = approval.get("rule") if isinstance(approval, dict) else None
        if not isinstance(rule, dict):
            rule = {}
        output = state.get("output")
        tool_trace.append({
            "tool": call.get("tool") if isinstance(call.get("tool"), str) else "unknown",
            "path": path if isinstance(path, str) else None,
            "status": state.get("status") if isinstance(state.get("status"), str) else "unknown",
            "permission_source": approval.get("source") if isinstance(approval, dict) else None,
            "permission_agent": approval.get("agent") if isinstance(approval, dict) else None,
            "permission": rule.get("permission"),
            "pattern": rule.get("pattern"),
            "action": rule.get("action"),
            "content_output_present": output not in (None, ""),
            "content_output_sha256": sha256(output.encode()) if isinstance(output, str) else None,
        })
    final_summary: dict[str, object] = {"parsed": False}
    try:
        final_text = _final_assistant_text(session, "pr-reviewer-standalone")
        final_result = _object(json.loads(final_text), "failed standalone final response")
        final_summary = {
            "parsed": True,
            "keys": sorted(final_result),
            "preflight": final_result.get("preflight"),
            "status": final_result.get("status"),
            "verdict": final_result.get("verdict"),
            "allowed_path_count": (
                len(final_result["allowed_paths"])
                if isinstance(final_result.get("allowed_paths"), list) else None
            ),
            "complete_read_count": final_result.get("complete_read_count"),
            "probe_result_count": (
                len(final_result["probe_results"])
                if isinstance(final_result.get("probe_results"), list) else None
            ),
            "unauthorized_bytes_returned": final_result.get("unauthorized_bytes_returned"),
        }
    except (PacketError, json.JSONDecodeError):
        pass
    return {
        "status": "STANDALONE_PREFLIGHT_TRACE_FAIL" if preflight else "STANDALONE_REVIEW_TRACE_FAIL",
        "failure_reason": reason,
        "session_export_sha256": session_export_sha256,
        "session": {
            "id": info.get("id"),
            "agent": info.get("agent"),
            "parentID": info.get("parentID"),
            "directory": info.get("directory"),
            "title": info.get("title"),
            "model": model_record,
            "variant": info.get("variant"),
        },
        "native_tool_call_count": len(calls),
        "native_tools": sorted({
            call.get("tool") for call in calls if isinstance(call.get("tool"), str)
        }),
        "native_tool_trace": tool_trace,
        "final_response_summary": final_summary,
        "os_level_isolation_claimed": False,
        "raw_session_export_persisted": False,
    }


def audit_standalone_session(
    session: dict[str, Any],
    *,
    session_export_sha256: str,
    runtime: RuntimeWorkspace,
    expected_model: dict[str, str],
    expected_title: str,
    expected_prompt: str,
    preflight: bool = False,
) -> dict[str, Any]:
    """Audit a fresh standalone primary session and native Read trace without echoing bytes."""
    _verify_overlay_files(runtime)
    _verify_probe_inventory(runtime)
    info = _object(session.get("info"), "standalone session info")
    agent_name = "pr-reviewer-standalone"
    session_id = _string(info.get("id"), "standalone session id")
    if info.get("agent") != agent_name:
        raise PacketError("Kilo selected a fallback or different standalone reviewer identity")
    if info.get("parentID") is not None:
        raise PacketError("standalone reviewer session has a parent or resumed context")
    if info.get("title") != expected_title:
        raise PacketError("standalone reviewer session title differs from the fresh invocation")
    directory = _string(info.get("directory"), "standalone session directory")
    if Path(directory).resolve() != runtime.workspace.resolve():
        raise PacketError("standalone reviewer session directory is not the packet-only workspace")
    model_info = _object(info.get("model"), "standalone model binding")
    actual_model = {
        "provider_id": _string(model_info.get("providerID"), "provider id"),
        "model_id": _string(model_info.get("id"), "model id"),
        "variant": _string(model_info.get("variant"), "model variant"),
    }
    if actual_model != expected_model:
        raise PacketError("standalone Kilo session model/provider/variant differs from the frozen binding")
    actual_prompt = _user_prompt(session)
    if actual_prompt not in (expected_prompt, f'"{expected_prompt}"'):
        raise PacketError("standalone Kilo session includes unapproved or prior conversation context")
    if info.get("projectID") != "global":
        raise PacketError("standalone packet workspace did not resolve as Kilo's non-VCS global project")
    permission_path_map = _kilo_permission_path_map(runtime)
    allowed_permission_paths = set(permission_path_map.values())
    session_permissions = info.get("permission", [])
    for raw_rule in _array(session_permissions, "standalone session permissions"):
        rule = _object(raw_rule, "standalone session permission")
        if rule.get("action") == "allow" and (
            rule.get("permission") != "read" or rule.get("pattern") not in allowed_permission_paths
        ):
            raise PacketError("standalone saved-session permission reopens a capability or path")

    calls = _tool_calls(session)
    tool_names = [_string(call.get("tool"), "standalone tool name") for call in calls]
    if any(tool != "read" for tool in tool_names):
        raise PacketError("standalone reviewer used a tool other than native Read")
    allowed = set(runtime.allowed_paths)
    absolute_to_relative = {
        absolute: relative
        for relative, absolute in _absolute_packet_path_map(runtime).items()
    }
    allowed_by_input = {**{path: path for path in runtime.allowed_paths}, **absolute_to_relative}
    denied = set(runtime.denied_paths)
    covered: dict[str, dict[int, str]] = {path: {} for path in allowed}
    seen_denied: set[str] = set()
    read_records: list[dict[str, Any]] = []
    for call in calls:
        state = _object(call.get("state"), "standalone Read state")
        request = _object(state.get("input"), "standalone Read input")
        raw_path = _string(request.get("filePath"), "standalone Read path")
        output = state.get("output")
        metadata = _object(state.get("metadata"), "standalone Read metadata")
        approval = _object(metadata.get("approval"), "standalone Read permission decision")
        permission_rule = _object(approval.get("rule"), "standalone Read permission rule")
        if approval.get("agent") != agent_name:
            raise PacketError("native Read trace is not bound to the standalone reviewer profile")

        packet_path = allowed_by_input.get(raw_path)
        if packet_path is not None:
            if (state.get("status") != "completed" or not isinstance(output, str)
                    or permission_rule.get("permission") != "read"
                    or permission_rule.get("action") != "allow"
                    or permission_rule.get("pattern") != permission_path_map[packet_path]):
                raise PacketError(f"allowed Read did not use its exact runtime rule: {raw_path}")
            lines = _read_lines(output)
            if not lines:
                raise PacketError(f"allowed Read returned no numbered packet lines: {raw_path}")
            if set(covered[packet_path]) & set(lines):
                raise PacketError(f"standalone reviewer reread an overlapping packet range: {packet_path}")
            covered[packet_path].update(lines)
            read_records.append({
                "path": packet_path,
                "requested_path": raw_path,
                "permission_pattern": permission_rule.get("pattern"),
                "status": "completed",
                "line_start": min(lines),
                "line_end": max(lines),
                "line_count": len(lines),
                "output_sha256": sha256(output.encode("utf-8")),
            })
            continue

        if raw_path not in denied:
            raise PacketError(f"standalone reviewer attempted an unlisted Read path: {raw_path}")
        if raw_path in seen_denied:
            raise PacketError(f"standalone reviewer repeated a denied probe: {raw_path}")
        denial = state.get("error")
        if isinstance(denial, dict):
            denial_text = " ".join(
                str(value) for value in (denial.get("name"), denial.get("message"))
                if isinstance(value, str)
            )
            data = denial.get("data")
            if isinstance(data, dict) and isinstance(data.get("message"), str):
                denial_text = f"{denial_text} {data['message']}".strip()
        else:
            denial_text = denial if isinstance(denial, str) else ""
        if not denial_text and isinstance(output, str):
            denial_text = output
        if state.get("status") != "error" or not re.search(r"denied|permission|not allowed", denial_text, re.I):
            raise PacketError(f"unauthorized Read was not denied by Kilo: {raw_path}")
        if output not in (None, ""):
            if not isinstance(output, str):
                raise PacketError(f"unauthorized Read returned a non-text result: {raw_path}")
            for canary_path, expected_hash in runtime.probe_hashes.items():
                if sha256(Path(canary_path).read_bytes()) == expected_hash:
                    marker = Path(canary_path).read_text(encoding="utf-8").strip()
                    if marker and marker in output:
                        raise PacketError(f"unauthorized Read returned canary bytes: {raw_path}")
            if "<content>" in output or "</content>" in output:
                raise PacketError(f"unauthorized Read returned a content block: {raw_path}")
        if permission_rule.get("action") != "deny" or permission_rule.get("permission") not in {
            "read", "external_directory"
        }:
            raise PacketError(f"unauthorized Read lacks an actual Kilo deny decision: {raw_path}")
        seen_denied.add(raw_path)
        label = runtime.probe_labels[raw_path]
        read_records.append({
            "path": raw_path,
            "probe": label,
            "status": "denied_before_content",
            "permission": permission_rule.get("permission"),
            "action": permission_rule.get("action"),
            "content_bytes_returned": 0,
        })

    if preflight:
        if seen_denied != denied:
            raise PacketError("standalone preflight omitted a required denied escape")
        allowed_sequence = [
            record["path"] for record in read_records if record["status"] == "completed"
        ]
        denied_sequence = [
            record["path"] for record in read_records if record["status"] == "denied_before_content"
        ]
        if allowed_sequence != list(runtime.allowed_paths) or denied_sequence != list(runtime.denied_paths):
            raise PacketError("standalone preflight did not follow the frozen read/probe order")
        for path in allowed:
            expected = _expected_line_map(runtime.packet / Path(path).name)
            if covered[path] != expected:
                raise PacketError(f"standalone preflight did not read the complete packet file: {path}")
        final_result = _validate_preflight_result(_final_assistant_text(session, agent_name), runtime)
    else:
        if seen_denied:
            raise PacketError("standalone PR review attempted an out-of-packet Read")
        full_paths = {f"packet/{path}" for path in runtime.inspection.full_read_paths}
        ranged_paths = {
            f"packet/{path}": ranges
            for path, ranges in runtime.inspection.review_ranges.items()
        }
        if full_paths | set(ranged_paths) != allowed:
            raise PacketError("manifest coverage does not match the standalone packet")
        for path in allowed:
            filename = path.removeprefix("packet/")
            actual = _expected_line_map(runtime.packet / filename)
            if path in full_paths:
                required_lines = actual
            else:
                required = {
                    line
                    for start, end in ranged_paths[path]
                    for line in range(start, end + 1)
                }
                if not required.issubset(actual):
                    raise PacketError(f"manifest has an out-of-bounds review range: {filename}")
                required_lines = {line: actual[line] for line in required}
            if covered[path] != required_lines:
                raise PacketError(f"standalone reviewer packet coverage is incomplete: {filename}")
        final_result = _validate_review_result(
            _final_assistant_text(session, agent_name), runtime.inspection
        )

    packet_inventory_sha256 = _verify_workspace_copy(runtime)
    return {
        "status": "STANDALONE_PREFLIGHT_TRACE_PASS" if preflight else "STANDALONE_REVIEW_TRACE_PASS",
        "reviewer_session": session_id,
        "reviewer_agent": agent_name,
        "reviewer_parent_session": None,
        "reviewer_directory": str(runtime.workspace),
        "model_dispatch": actual_model,
        "runtime_identity": "DISPATCH_VERIFIED",
        "runtime_backend_identity": "not independently observed",
        "session_export_sha256": session_export_sha256,
        "reviewer_tools": sorted(set(tool_names)),
        "allowed_file_count": len(covered),
        "denied_before_content_count": len(seen_denied),
        "read_trace": read_records,
        "packet_inventory_sha256": packet_inventory_sha256,
        "preflight": preflight,
        "preflight_result": final_result if preflight else None,
        "reviewer_result": None if preflight else final_result,
        "os_level_isolation_claimed": False,
    }


def _external_roots(values: list[str]) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise PacketError("external root must be REPOSITORY=PATH")
        name, path = value.split("=", 1)
        if not name or not path or name in roots:
            raise PacketError("external source root mapping is empty or duplicated")
        roots[name] = Path(path)
    return roots


def _write_report(path: Path | None, report: dict[str, Any]) -> None:
    if path is None:
        return
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise PacketError("report parent must already exist and be a real directory")
    path.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _runtime_from_report(
    report_path: Path,
    packet_root: Path,
    manifest_path: Path,
    *,
    source_root: Path | None,
    profile_source_root: Path | None,
    external_source_roots: dict[str, Path],
    kilo_binary: Path,
    launch_environment: dict[str, str],
) -> RuntimeWorkspace:
    report = _object(json.loads(report_path.read_bytes()), "prepared runtime report")
    recorded_source_root = report.get("source_root")
    if (source_root is None and recorded_source_root is not None
            or source_root is not None and recorded_source_root != str(source_root.resolve())):
        raise PacketError("prepared report source-checkout mode differs from the audit")
    recorded_profile_root = report.get("profile_source_root")
    if (profile_source_root is None and recorded_profile_root is not None
            or profile_source_root is not None
            and recorded_profile_root != str(profile_source_root.resolve())):
        raise PacketError("prepared report profile-source checkout differs from the audit")
    report_external = _object(report.get("external_source_roots"), "report external source roots")
    if {name: str(path.resolve()) for name, path in external_source_roots.items()} != report_external:
        raise PacketError("prepared report and audit disagree on pinned external source checkouts")
    if (report.get("kilo_binary") != str(kilo_binary.resolve())
            or report.get("kilo_version") != "7.8.8"
            or report.get("launch_environment") != launch_environment):
        raise PacketError("standalone audit environment differs from the frozen prelaunch config")
    manifest_bytes = manifest_path.read_bytes()
    if report.get("manifest_sha256") != sha256(manifest_bytes):
        raise PacketError("prepared runtime report is bound to a different manifest")
    inspection = validate_packet(
        packet_root,
        manifest_path,
        source_root=source_root,
        external_source_roots=external_source_roots,
    )
    if source_root is not None:
        validate_standalone_packet_path_plan(inspection)
    canonical_profile = Path(_string(report.get("canonical_profile"), "canonical profile path"))
    if profile_source_root is not None:
        _verify_profile_git_binding(canonical_profile, profile_source_root, inspection.head)
    workspace = Path(_string(report.get("workspace"), "prepared workspace"))
    packet = Path(_string(report.get("packet_workspace"), "prepared packet workspace"))
    profile_config = Path(_string(report.get("per_run_kilo_config_dir"), "prepared Kilo config dir"))
    allowed_paths = tuple(_string(p, "allowed packet path") for p in _array(
        report.get("allowed_packet_paths"), "allowed packet paths"))
    denied_paths = tuple(_string(p, "denied probe path") for p in _array(
        report.get("denied_canary_paths"), "denied canary paths"))
    raw_probe_hashes = _object(report.get("probe_hashes"), "probe file hashes")
    probe_hashes = {
        _string(path, "probe path"): _string(value, "probe SHA-256")
        for path, value in raw_probe_hashes.items()
    }
    raw_probe_symlinks = _object(report.get("probe_symlinks"), "probe symlinks")
    probe_symlinks = {
        _string(path, "probe symlink path"): _string(value, "probe symlink target")
        for path, value in raw_probe_symlinks.items()
    }
    raw_probe_labels = _object(report.get("probe_labels"), "probe labels")
    probe_labels = {
        _string(path, "labeled probe path"): _string(value, "probe label")
        for path, value in raw_probe_labels.items()
    }
    probe_root_raw = report.get("probe_root")
    probe_root = Path(_string(probe_root_raw, "probe root")) if probe_root_raw is not None else None
    effective = _object(report.get("effective_permissions"), "effective permissions report")
    config_content = _string(report.get("kilo_config_content"), "Kilo config content")
    if (report.get("review_session_started") is not False
            or report.get("status") != "PRELAUNCH_STANDALONE_PERMISSION_PASS"):
        raise PacketError("prepared report does not prove pre-launch standalone permissions")
    runtime = RuntimeWorkspace(
        workspace=workspace,
        packet=packet,
        inspection=inspection,
        external_source_roots=external_source_roots,
        canonical_profile=canonical_profile,
        profile_source_root=profile_source_root,
        profile_config=profile_config,
        probe_root=probe_root,
        probe_hashes=probe_hashes,
        probe_symlinks=probe_symlinks,
        probe_labels=probe_labels,
        allowed_paths=allowed_paths,
        denied_paths=denied_paths,
        profile_prompt_sha256=_string(effective.get("effective_prompt_sha256"), "effective prompt hash"),
        canonical_profile_sha256=_string(effective.get("profile_source_sha256"), "profile source hash"),
        overlay_profile_sha256=_string(
            effective.get("overlay_profile_sha256"), "per-run standalone profile hash"
        ),
        overlay_config_sha256=_string(effective.get("overlay_config_sha256"), "per-run config file hash"),
        kilo_config_content=config_content,
    )
    _verify_overlay_files(runtime)
    _verify_probe_inventory(runtime)
    if source_root is not None:
        canonical_profile = source_root / ".kilo/agents/pr-reviewer-standalone.md"
        if sha256(canonical_profile.read_bytes()) != runtime.canonical_profile_sha256:
            raise PacketError("prepared report is bound to a different standalone reviewer profile")
    return runtime


def _export_standalone_session(
    *, kilo_binary: Path, session_id: str, workspace: Path, env: dict[str, str],
) -> tuple[dict[str, Any], str]:
    if re.fullmatch(r"ses_[A-Za-z0-9]+", session_id) is None:
        raise PacketError("standalone Kilo session ID is malformed")
    result = subprocess.run(
        [str(kilo_binary), "--pure", "export", session_id],
        cwd=workspace,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=120,
        check=False,
    )
    if result.returncode:
        raise PacketError("Kilo could not export the exact standalone session trace")
    try:
        return _object(json.loads(result.stdout), "Kilo standalone session export"), sha256(result.stdout)
    except (json.JSONDecodeError, PacketError) as err:
        raise PacketError("Kilo standalone session export is not valid JSON") from err


def standalone_environment(
    *, kilo_binary: Path, profile_config: Path, kilo_config_content: str,
    home: Path, tmpdir: Path,
    config_home: Path, data_home: Path, state_home: Path, cache_home: Path,
) -> dict[str, str]:
    return {
        "PATH": _minimal_cli_path(kilo_binary),
        "HOME": str(home),
        "TMPDIR": str(tmpdir),
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_DATA_HOME": str(data_home),
        "XDG_STATE_HOME": str(state_home),
        "XDG_CACHE_HOME": str(cache_home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "KILO_NO_DAEMON": "1",
        "KILO_DISABLE_PROJECT_CONFIG": "1",
        "KILO_CONFIG_DIR": str(profile_config),
        "KILO_CONFIG_CONTENT": kilo_config_content,
    }


def capture_session_id(events: TextIO) -> str:
    """Consume Kilo JSON events without emitting packet text or tool outputs."""
    session_id: str | None = None
    for line_number, line in enumerate(events, 1):
        if not line.strip():
            continue
        try:
            event = _object(json.loads(line), "Kilo JSON event")
        except (json.JSONDecodeError, PacketError) as err:
            raise PacketError(f"Kilo run emitted a non-JSON event at line {line_number}") from err
        if event.get("sessionID") is None:
            raise PacketError("Kilo JSON event omitted its session ID")
        current = _string(event.get("sessionID"), "Kilo event session ID")
        if re.fullmatch(r"ses_[A-Za-z0-9]+", current) is None:
            raise PacketError("Kilo run emitted a malformed session ID")
        if session_id is not None and current != session_id:
            raise PacketError("Kilo run JSON events contain more than one session")
        session_id = current
    if session_id is None:
        raise PacketError("Kilo run emitted no session ID")
    return session_id


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("capture-session-id", help="filter Kilo JSON events to their single session ID")

    synthetic = sub.add_parser(
        "synthetic-packet", help="create harmless production-shaped preflight packet files"
    )
    synthetic.add_argument("--parent", type=Path, required=True)
    synthetic.add_argument("--changed-path", action="append", required=True)
    synthetic.add_argument("--head", required=True)
    synthetic.add_argument("--base", required=True)
    synthetic.add_argument("--merge-base", required=True)

    validate = sub.add_parser("validate", help="validate immutable packet and source bindings")
    validate.add_argument("--packet", type=Path, required=True)
    validate.add_argument("--manifest", type=Path, required=True)
    validate.add_argument("--source-root", type=Path, required=True)
    validate.add_argument("--external-root", action="append", default=[])

    permission_config = sub.add_parser(
        "permission-config", help="render the exact standalone profile Read permission map"
    )
    permission_config.add_argument("--manifest", type=Path, required=True)
    permission_config.add_argument("--workspace", type=Path, required=True)

    prepare = sub.add_parser(
        "prepare", help="prepare packet-only standalone workspace and verify effective Kilo rules"
    )
    prepare.add_argument("--packet", type=Path, required=True)
    prepare.add_argument("--manifest", type=Path, required=True)
    prepare.add_argument("--profile", type=Path, required=True)
    prepare.add_argument("--source-root", type=Path)
    prepare.add_argument("--profile-source-root", type=Path, required=True)
    prepare.add_argument("--external-root", action="append", default=[])
    prepare.add_argument("--workspace-parent", type=Path, required=True)
    prepare.add_argument("--overlay-parent", type=Path, required=True)
    prepare.add_argument("--home", type=Path, required=True)
    prepare.add_argument("--tmpdir", type=Path, required=True)
    prepare.add_argument("--config-home", type=Path, required=True)
    prepare.add_argument("--data-home", type=Path, required=True)
    prepare.add_argument("--state-home", type=Path, required=True)
    prepare.add_argument("--cache-home", type=Path, required=True)
    prepare.add_argument("--kilo", type=Path, required=True)
    prepare.add_argument("--provider-id", required=True)
    prepare.add_argument("--model-id", required=True)
    prepare.add_argument("--variant", required=True)
    prepare.add_argument("--title")
    prepare.add_argument("--report", type=Path)
    prepare.add_argument("--preflight", action="store_true")
    prepare.add_argument("--synthetic", action="store_true")

    audit = sub.add_parser("audit-standalone", help="audit a native standalone Kilo primary session")
    audit.add_argument("--packet", type=Path, required=True)
    audit.add_argument("--manifest", type=Path, required=True)
    audit.add_argument("--source-root", type=Path)
    audit.add_argument("--profile-source-root", type=Path, required=True)
    audit.add_argument("--external-root", action="append", default=[])
    audit.add_argument("--prepared-report", type=Path, required=True)
    audit.add_argument("--session-id", required=True)
    audit.add_argument("--home", type=Path, required=True)
    audit.add_argument("--tmpdir", type=Path, required=True)
    audit.add_argument("--config-home", type=Path, required=True)
    audit.add_argument("--data-home", type=Path, required=True)
    audit.add_argument("--state-home", type=Path, required=True)
    audit.add_argument("--cache-home", type=Path, required=True)
    audit.add_argument("--kilo", type=Path, required=True)
    audit.add_argument("--report", type=Path)
    audit.add_argument("--preflight", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            result = validate_packet(
                args.packet,
                args.manifest,
                source_root=args.source_root,
                profile_source_root=args.profile_source_root,
                external_source_roots=_external_roots(args.external_root),
            )
            print(json.dumps({"status": "PACKET_VALID", **asdict(result)}, sort_keys=True))
            return 0

        if args.command == "capture-session-id":
            print(capture_session_id(sys.stdin))
            return 0

        if args.command == "synthetic-packet":
            if tuple(sorted(set(args.changed_path))) != tuple(args.changed_path):
                raise PacketError("synthetic changed paths must be sorted and unique")
            packet, manifest_path = create_synthetic_standalone_packet(
                args.parent,
                changed_paths=tuple(args.changed_path),
                head=args.head,
                base=args.base,
                merge_base=args.merge_base,
            )
            print(json.dumps({
                "status": "SYNTHETIC_PACKET_CREATED",
                "packet": str(packet),
                "manifest": str(manifest_path),
                "file_paths": list(standalone_packet_path_plan(tuple(args.changed_path))),
            }, sort_keys=True))
            return 0

        manifest_bytes = args.manifest.read_bytes()
        manifest = _object(json.loads(manifest_bytes), "manifest")
        paths = tuple(
            _relative_file(_object(item, "manifest file row").get("path"))
            for item in _array(manifest.get("files"), "manifest.files")
        )
        if args.command == "permission-config":
            print(json.dumps(
                make_per_run_permission_map(list(paths), workspace=args.workspace),
                sort_keys=True, separators=(",", ":"),
            ))
            return 0

        if args.command == "audit-standalone":
            prepared = _object(
                json.loads(args.prepared_report.read_bytes()), "prepared standalone report"
            )
            launch_env = standalone_environment(
                kilo_binary=args.kilo,
                profile_config=Path(_string(
                    prepared.get("per_run_kilo_config_dir"), "prepared Kilo config dir"
                )),
                kilo_config_content=_string(
                    prepared.get("kilo_config_content"), "prepared Kilo config content"
                ),
                home=args.home,
                tmpdir=args.tmpdir,
                config_home=args.config_home,
                data_home=args.data_home,
                state_home=args.state_home,
                cache_home=args.cache_home,
            )
            synthetic = prepared.get("synthetic_packet") is True
            if synthetic != args.preflight:
                raise PacketError("preflight mode differs from the prepared packet type")
            if (synthetic and args.source_root is not None) or (
                not synthetic and args.source_root is None
            ):
                raise PacketError("audit source checkout differs from the prepared packet type")
            if synthetic:
                _ = validate_synthetic_preflight_packet(args.packet, args.manifest)
            canonical_profile = Path(_string(
                prepared.get("canonical_profile"), "prepared canonical reviewer profile"
            ))
            if canonical_profile.resolve() != (
                args.profile_source_root / ".kilo/agents/pr-reviewer-standalone.md"
            ).resolve():
                raise PacketError("audit profile path differs from the frozen source profile")
            runtime = _runtime_from_report(
                args.prepared_report,
                args.packet,
                args.manifest,
                source_root=args.source_root,
                profile_source_root=args.profile_source_root,
                external_source_roots=_external_roots(args.external_root),
                kilo_binary=args.kilo,
                launch_environment=launch_env,
            )
            prepared_binding = _object(prepared.get("model_binding"), "prepared model binding")
            expected_model = {
                "provider_id": _string(prepared_binding.get("provider_id"), "expected provider"),
                "model_id": _string(prepared_binding.get("model_id"), "expected model"),
                "variant": _string(prepared_binding.get("variant"), "expected variant"),
            }
            title = _string(prepared.get("reviewer_title"), "prepared reviewer title")
            expected_prompt = (
                standalone_preflight_prompt(runtime)
                if args.preflight else standalone_review_prompt(runtime)
            )
            expected_argv = standalone_run_argv(args.kilo, expected_model, title, expected_prompt)
            if prepared.get("standalone_run_argv") != expected_argv:
                raise PacketError("reviewer CLI argv differs from the frozen standalone invocation")
            effective = verify_effective_standalone_permissions(
                kilo_binary=args.kilo,
                overlay=runtime,
                canonical_profile=canonical_profile,
                home=args.home,
                tmpdir=args.tmpdir,
                config_home=args.config_home,
                data_home=args.data_home,
                state_home=args.state_home,
                cache_home=args.cache_home,
            )
            if effective != prepared.get("effective_permissions"):
                raise PacketError("effective standalone permissions changed after preparation")
            session, export_sha256 = _export_standalone_session(
                kilo_binary=args.kilo,
                session_id=args.session_id,
                workspace=runtime.workspace,
                env=launch_env,
            )
            try:
                report = audit_standalone_session(
                    session,
                    session_export_sha256=export_sha256,
                    runtime=runtime,
                    expected_model=expected_model,
                    expected_title=title,
                    expected_prompt=expected_prompt,
                    preflight=args.preflight,
                )
            except PacketError as err:
                report = summarize_failed_standalone_session(
                    session,
                    session_export_sha256=export_sha256,
                    reason=str(err),
                    preflight=args.preflight,
                )
                _write_report(args.report, report)
                print(json.dumps(report, sort_keys=True, separators=(",", ":")))
                return 1
            _write_report(args.report, report)
            print(json.dumps(report, sort_keys=True, separators=(",", ":")))
            return 0

        if args.synthetic:
            if not args.preflight or args.source_root is not None or args.external_root:
                raise PacketError("synthetic packet preparation is allowed only for preflight without a source checkout")
            _ = validate_synthetic_preflight_packet(args.packet, args.manifest)
        elif args.preflight or args.source_root is None:
            raise PacketError("preflight must use a synthetic packet; production preparation requires a source checkout")
        canonical_profile = args.profile
        if canonical_profile.resolve() != (
            args.profile_source_root / ".kilo/agents/pr-reviewer-standalone.md"
        ).resolve():
            raise PacketError("canonical reviewer profile must be the tracked standalone profile")
        if (args.source_root is not None
                and args.source_root.resolve() != args.profile_source_root.resolve()):
            raise PacketError("packet and profile source checkouts must be the same frozen worktree")
        runtime = prepare_standalone_environment(
            args.packet,
            args.manifest,
            canonical_profile,
            args.workspace_parent,
            args.overlay_parent,
            source_root=args.source_root,
            profile_source_root=args.profile_source_root,
            external_source_roots=_external_roots(args.external_root),
            preflight=args.preflight,
        )
        effective = verify_effective_standalone_permissions(
            kilo_binary=args.kilo,
            overlay=runtime,
            canonical_profile=args.profile,
            home=args.home,
            tmpdir=args.tmpdir,
            config_home=args.config_home,
            data_home=args.data_home,
            state_home=args.state_home,
            cache_home=args.cache_home,
        )
        expected_prompt = (
            standalone_preflight_prompt(runtime)
            if args.preflight else standalone_review_prompt(runtime)
        )
        model_binding = {
            "provider_id": args.provider_id,
            "model_id": args.model_id,
            "variant": args.variant,
        }
        title = args.title or (
            "synthetic-standalone-permission-preflight"
            if args.preflight
            else f"review-issue-{runtime.inspection.issue_number}-pr-{runtime.inspection.pull_number}"
        )
        run_argv = standalone_run_argv(args.kilo, model_binding, title, expected_prompt)
        launch_env = standalone_environment(
            kilo_binary=args.kilo,
            profile_config=runtime.profile_config,
            kilo_config_content=runtime.kilo_config_content,
            home=args.home,
            tmpdir=args.tmpdir,
            config_home=args.config_home,
            data_home=args.data_home,
            state_home=args.state_home,
            cache_home=args.cache_home,
        )
        report: dict[str, Any] = {
            "status": "PRELAUNCH_STANDALONE_PERMISSION_PASS",
            "synthetic_packet": args.synthetic,
            "source_root": str(args.source_root.resolve()) if args.source_root is not None else None,
            "profile_source_root": str(args.profile_source_root.resolve()),
            "canonical_profile": str(canonical_profile.resolve()),
            "manifest_sha256": sha256(manifest_bytes),
            "workspace": str(runtime.workspace),
            "packet_workspace": str(runtime.packet),
            "external_source_roots": {
                name: str(path.resolve())
                for name, path in runtime.external_source_roots.items()
            },
            "per_run_kilo_config_dir": str(runtime.profile_config),
            "per_run_reviewer_profile": str(
                runtime.profile_config / "agents/pr-reviewer-standalone.md"
            ),
            "per_run_kilo_config_file": str(runtime.profile_config / "kilo.json"),
            "kilo_disable_project_config": "1",
            "kilo_version": effective["kilo_version"],
            "kilo_binary": str(args.kilo.resolve()),
            "kilo_config_content": runtime.kilo_config_content,
            "launch_environment": launch_env,
            "standalone_agent": "pr-reviewer-standalone",
            "standalone_profile_mode": effective["reviewer_profile_mode"],
            "standalone_resolved_mode": effective["reviewer_resolved_mode"],
            "model_binding": model_binding,
            "reviewer_title": title,
            "standalone_run_argv": run_argv,
            "standalone_prompt": expected_prompt,
            "allowed_packet_paths": list(runtime.allowed_paths),
            "denied_canary_paths": list(runtime.denied_paths),
            "probe_root": str(runtime.probe_root) if runtime.probe_root is not None else None,
            "probe_hashes": runtime.probe_hashes,
            "probe_symlinks": runtime.probe_symlinks,
            "probe_labels": runtime.probe_labels,
            "effective_permissions": effective,
            "review_session_started": False,
            "os_level_isolation_claimed": False,
        }
        _write_report(args.report, report)
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 0
    except (PacketError, OSError, json.JSONDecodeError, subprocess.SubprocessError) as err:
        print(json.dumps({"status": "FAIL_CLOSED", "reason": str(err)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
