#!/usr/bin/env python3
"""Validate sealed review packets and Kilo 7.8.8 effective permissions.

This is a primary-side validator/config preparer, not a reviewer launcher or
controller. It never starts a Kilo Task, fetches Forgejo data, or edits the source
checkout. The caller owns those operations and must inspect its outputs.
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
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast


class PacketError(ValueError):
    """The packet or effective permission boundary is invalid."""


@dataclass(frozen=True)
class PacketInspection:
    packet_root: str
    manifest_path: str
    manifest_sha256: str
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
    profile_config: Path
    absolute_canary: Path | None
    allowed_paths: tuple[str, ...]
    denied_paths: tuple[str, ...]
    profile_prompt_sha256: str
    profile_file_sha256: str
    kilo_config_content: str


_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
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
        head=head,
        base=base,
        merge_base=merge_base,
        file_paths=tuple(sorted(records)),
        changed_paths=changed_paths,
        external_source_count=len(external_sources),
        full_read_paths=tuple(sorted(full_read_paths)),
        review_ranges=review_ranges,
    )


def make_kilo_config_content(packet_paths: tuple[str, ...] | list[str]) -> str:
    """Return exact parent/reviewer KILO_CONFIG_CONTENT for packet-relative paths."""
    safe_paths = tuple(sorted({_relative_file(path) for path in packet_paths}))
    if not safe_paths or len(safe_paths) != len(packet_paths):
        raise PacketError("Kilo overlay requires a unique nonempty exact-file list")
    read_rules: dict[str, str] = {"*": "deny"}
    read_rules.update({f"packet/{path}": "allow" for path in safe_paths})
    config = {
        "agent": {
            "code": {"permission": {"*": "deny", "task": "allow"}},
            "pr-reviewer": {
                "permission": {
                    "*": "deny",
                    "read": read_rules,
                    "external_directory": {"*": "deny"},
                },
            },
        },
    }
    return json.dumps(config, separators=(",", ":"), sort_keys=True)


def _profile_parts(profile_path: Path) -> tuple[bytes, str, str]:
    try:
        raw = profile_path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise PacketError("canonical pr-reviewer profile is unavailable or not UTF-8") from err
    parts = text.split("---", 2)
    if len(parts) != 3 or parts[0].strip():
        raise PacketError("canonical reviewer profile frontmatter is malformed")
    frontmatter = parts[1]
    if not re.search(r"(?m)^mode:\s*subagent\s*$", frontmatter):
        raise PacketError("canonical reviewer mode must remain subagent")
    if not re.search(r"(?m)^permission:\s*\n\s+\"\*\":\s*deny\s*$", frontmatter):
        raise PacketError("canonical reviewer static permissions must deny all tools")
    if not re.search(r"(?m)^\s+read:\s*deny\s*$", frontmatter):
        raise PacketError("canonical reviewer static Read must deny without a per-run overlay")
    if re.search(r"(?m)^\s+[^#\n]+:\s*allow\s*$", frontmatter):
        raise PacketError("canonical reviewer frontmatter contains a static allow")
    description = re.search(r"(?m)^description:\s*(.+?)\s*$", frontmatter)
    if description is None:
        raise PacketError("canonical reviewer description is missing")
    return raw, description.group(1), parts[2].lstrip("\n").rstrip()


def prepare_task_environment(
    packet_root: Path,
    manifest_path: Path,
    canonical_profile: Path,
    workspace_parent: Path,
    overlay_parent: Path,
    *,
    source_root: Path | None = None,
    external_source_roots: dict[str, Path] | None = None,
    preflight: bool = False,
) -> RuntimeWorkspace:
    """Prepare a packet-only Kilo workspace and per-run exact Read profile.

    This function prepares files/configuration only; it never starts Kilo or a Task.
    """
    inspection = validate_packet(
        packet_root,
        manifest_path,
        source_root=source_root,
        external_source_roots=external_source_roots,
    )
    if not workspace_parent.is_dir() or workspace_parent.is_symlink():
        raise PacketError("workspace parent must already exist and be a real directory")
    if not overlay_parent.is_dir() or overlay_parent.is_symlink():
        raise PacketError("overlay parent must already exist and be a real directory")
    if source_root is not None and workspace_parent.resolve().is_relative_to(source_root.resolve()):
        raise PacketError("review workspace cannot be inside the source checkout")
    profile_bytes, _, prompt = _profile_parts(canonical_profile)
    if source_root is not None:
        canonical_profile_relative = ".kilo/agents/pr-reviewer.md"
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

    workspace = Path(tempfile.mkdtemp(prefix="sr-review-workspace-", dir=workspace_parent))
    packet = workspace / "packet"
    packet.mkdir(mode=0o700)
    for name in inspection.file_paths:
        destination = packet / name
        shutil.copyfile(packet_root / name, destination)
        os.chmod(destination, 0o444)
    os.chmod(packet, 0o555)

    denied_paths: list[str] = []
    absolute_canary: Path | None = None
    if preflight:
        outside = workspace / "probe-outside"
        outside.mkdir(mode=0o700)
        for name, content in (
            ("sibling-canary.txt", b"DENIED PROBE CANARY: sibling.\n"),
            ("traversal-canary.txt", b"DENIED PROBE CANARY: traversal.\n"),
            ("symlink-target.txt", b"DENIED PROBE CANARY: external symlink.\n"),
        ):
            path = outside / name
            path.write_bytes(content)
            os.chmod(path, 0o444)
        (outside / "symlink-into-packet.txt").symlink_to("../packet/index.md")
        os.chmod(packet, 0o755)
        (packet / "symlink-outside.txt").symlink_to("../probe-outside/symlink-target.txt")
        os.chmod(packet, 0o555)
        absolute_fd, absolute_name = tempfile.mkstemp(prefix="sr-review-absolute-canary-", dir=workspace_parent)
        os.close(absolute_fd)
        absolute_canary = Path(absolute_name)
        absolute_canary.write_bytes(b"DENIED PROBE CANARY: absolute external file.\n")
        os.chmod(absolute_canary, 0o444)
        os.chmod(outside, 0o555)
        denied_paths = [
            "probe-outside/sibling-canary.txt",
            str(absolute_canary),
            "packet/../probe-outside/traversal-canary.txt",
            "probe-outside/symlink-into-packet.txt",
            "packet/symlink-outside.txt",
        ]
    os.chmod(workspace, 0o555)

    overlay = Path(tempfile.mkdtemp(prefix="sr-review-kilo-overlay-", dir=overlay_parent))
    agents = overlay / "agents"
    agents.mkdir(mode=0o700)
    (overlay / ".gitignore").write_text("# per-run Kilo reviewer configuration\n", encoding="utf-8")
    os.chmod(overlay / ".gitignore", 0o444)
    overlay_profile = agents / "pr-reviewer.md"
    overlay_profile.write_bytes(profile_bytes)
    os.chmod(overlay_profile, 0o444)
    os.chmod(agents, 0o555)
    os.chmod(overlay, 0o555)
    if overlay_profile.read_bytes() != canonical_profile.read_bytes():
        raise PacketError("per-run reviewer profile is not byte-identical to the canonical source")

    return RuntimeWorkspace(
        workspace=workspace,
        packet=packet,
        inspection=inspection,
        profile_config=overlay,
        absolute_canary=absolute_canary,
        allowed_paths=tuple(f"packet/{path}" for path in inspection.file_paths),
        denied_paths=tuple(denied_paths),
        profile_prompt_sha256=sha256(prompt.encode()),
        profile_file_sha256=sha256(profile_bytes),
        kilo_config_content=make_kilo_config_content(list(inspection.file_paths)),
    )


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


def verify_effective_agent_pair(
    code: dict[str, Any], reviewer: dict[str, Any], overlay: RuntimeWorkspace,
    canonical_profile: Path,
) -> dict[str, Any]:
    """Check the Kilo v7.8.8 resolved rules using its last-matching-rule semantics."""
    if code.get("name") != "code" or reviewer.get("name") != "pr-reviewer":
        raise PacketError("effective Kilo agent identity mismatch")
    if reviewer.get("mode") != "subagent":
        raise PacketError("effective pr-reviewer is not a native subagent")
    _tool_map(code, {"task"})
    _tool_map(reviewer, {"read"})
    code_rules = _permission_rules(code)
    reviewer_rules = _permission_rules(reviewer)
    _expect_permission(code_rules, "task", "*", "allow")
    _expect_permission(reviewer_rules, "task", "*", "deny")
    for tool in (*_FORBIDDEN_TOOLS, *_MCP_PROBES):
        if tool != "task":
            _expect_permission(code_rules, tool, "*", "deny")
            _expect_permission(reviewer_rules, tool, "*", "deny")

    for path in overlay.allowed_paths:
        _expect_permission(reviewer_rules, "read", path, "allow", exact=True)
    for path in overlay.denied_paths:
        _expect_permission(reviewer_rules, "read", path, "deny")
    if overlay.absolute_canary is not None:
        _expect_permission(reviewer_rules, "read", str(overlay.absolute_canary), "deny")
        _expect_permission(reviewer_rules, "external_directory", str(overlay.absolute_canary), "deny")

    _, _, expected_prompt = _profile_parts(canonical_profile)
    prompt = reviewer.get("prompt")
    if not isinstance(prompt, str) or prompt.rstrip() != expected_prompt:
        raise PacketError("effective reviewer prompt differs from the frozen canonical profile")
    source_sha = sha256(canonical_profile.read_bytes())
    prompt_sha = sha256(prompt.encode())
    overlay_profile_sha = sha256((overlay.profile_config / "agents/pr-reviewer.md").read_bytes())
    if overlay_profile_sha != source_sha or prompt_sha != sha256(expected_prompt.encode()):
        raise PacketError("per-run profile/prompt does not match the canonical source")
    return {
        "status": "EFFECTIVE_KILO_PERMISSION_PASS",
        "parent_agent": "code",
        "parent_effective_tools": ["task"],
        "reviewer_agent": "pr-reviewer",
        "reviewer_effective_tools": ["read"],
        "profile_source_sha256": source_sha,
        "effective_prompt_sha256": prompt_sha,
        "overlay_profile_sha256": overlay_profile_sha,
        "allowed_packet_paths": list(overlay.allowed_paths),
        "denied_probe_paths": list(overlay.denied_paths),
        "permission_config_sha256": sha256(overlay.kilo_config_content.encode()),
        "os_level_isolation_claimed": False,
    }


def verify_effective_permissions(
    *,
    kilo_binary: Path,
    overlay: RuntimeWorkspace,
    canonical_profile: Path,
    home: Path,
    tmpdir: Path,
    expected_version: str = "7.8.8",
) -> dict[str, Any]:
    """Resolve the real Kilo agents before any independent Task exists."""
    if not overlay.workspace.is_dir() or overlay.workspace.is_symlink():
        raise PacketError("reviewer workspace is missing or linked")
    if not home.is_dir() or not tmpdir.is_dir():
        raise PacketError("Kilo debug HOME and TMPDIR must already exist")
    env = {
        "PATH": "/home/adrian/.local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(tmpdir),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "KILO_DISABLE_PROJECT_CONFIG": "1",
        "KILO_CONFIG_DIR": str(overlay.profile_config),
        "KILO_CONFIG_CONTENT": overlay.kilo_config_content,
    }
    version = subprocess.run(
        [str(kilo_binary), "--version"], cwd=overlay.workspace, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, timeout=60, check=False)
    if version.returncode or version.stdout.strip() != expected_version:
        raise PacketError("installed Kilo version does not match pinned permission evidence")

    code = _debug_agent(kilo_binary, "code", overlay.workspace, env)
    reviewer = _debug_agent(kilo_binary, "pr-reviewer", overlay.workspace, env)
    result = verify_effective_agent_pair(code, reviewer, overlay, canonical_profile)
    return {"kilo_version": expected_version, "task_created": False, **result}


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
    current = validate_packet(source_packet, manifest_path, require_external_sources=True)
    if current.manifest_sha256 != runtime.inspection.manifest_sha256:
        raise PacketError("source packet manifest changed while the Task was active")
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
            raise PacketError(f"review workspace packet bytes changed during Task: {name}")
        hashes.append((name, sha256(copied.read_bytes())))

    expected_packet_names = set(current.file_paths)
    expected_workspace_names = {"packet"}
    if runtime.denied_paths:
        expected_workspace_names.add("probe-outside")
        expected_packet_names.add("symlink-outside.txt")
    if {item.name for item in runtime.workspace.iterdir()} != expected_workspace_names:
        raise PacketError("packet-only workspace contains an unexpected entry")
    packet_entries = {item.name: item for item in runtime.packet.iterdir()}
    if set(packet_entries) != expected_packet_names:
        raise PacketError("review workspace packet contains an unexpected or missing entry")

    if runtime.denied_paths:
        symlink_out = packet_entries["symlink-outside.txt"]
        if not symlink_out.is_symlink() or os.readlink(symlink_out) != "../probe-outside/symlink-target.txt":
            raise PacketError("preflight out-of-packet symlink changed")
        outside = runtime.workspace / "probe-outside"
        expected_outside = {
            "sibling-canary.txt", "traversal-canary.txt", "symlink-target.txt",
            "symlink-into-packet.txt",
        }
        if {item.name for item in outside.iterdir()} != expected_outside:
            raise PacketError("preflight sibling canary inventory changed")
        into_packet = outside / "symlink-into-packet.txt"
        if not into_packet.is_symlink() or os.readlink(into_packet) != "../packet/index.md":
            raise PacketError("preflight into-packet symlink changed")
        for name, expected in (
            ("sibling-canary.txt", b"DENIED PROBE CANARY: sibling.\n"),
            ("traversal-canary.txt", b"DENIED PROBE CANARY: traversal.\n"),
            ("symlink-target.txt", b"DENIED PROBE CANARY: external symlink.\n"),
        ):
            canary = outside / name
            if canary.is_symlink() or canary.read_bytes() != expected:
                raise PacketError(f"preflight canary changed: {name}")
        if (runtime.absolute_canary is None
                or runtime.absolute_canary.read_bytes() != b"DENIED PROBE CANARY: absolute external file.\n"):
            raise PacketError("preflight absolute canary changed")

    return sha256(json.dumps(sorted(hashes), separators=(",", ":")).encode())


def audit_task_trace(
    parent_session: dict[str, Any],
    reviewer_session: dict[str, Any],
    *,
    runtime: RuntimeWorkspace,
    expected_model: dict[str, str] | None = None,
    preflight: bool = False,
) -> dict[str, Any]:
    """Audit native parent/child tool traces and full packet reads without echoing bytes."""
    parent_info = _object(parent_session.get("info"), "parent session info")
    child_info = _object(reviewer_session.get("info"), "reviewer session info")
    parent_id = _string(parent_info.get("id"), "parent session id")
    if (parent_info.get("agent") != "code" or parent_info.get("parentID") is not None
            or parent_info.get("directory") != str(runtime.workspace)):
        raise PacketError("Kilo parent identity or packet-only directory mismatch")
    if (child_info.get("agent") != "pr-reviewer" or child_info.get("parentID") != parent_id
            or child_info.get("directory") != str(runtime.workspace)):
        raise PacketError("Kilo reviewer identity/parent/packet-workspace mismatch")
    if (parent_info.get("model") != child_info.get("model")
            or parent_info.get("variant") != child_info.get("variant")):
        raise PacketError("reviewer did not inherit the exact parent model and variant")
    if expected_model is not None and child_info.get("model") != expected_model:
        raise PacketError("reviewer model/provider/variant differs from the reserved binding")

    parent_calls = _tool_calls(parent_session)
    child_calls = _tool_calls(reviewer_session)
    if (len(parent_calls) != 1 or parent_calls[0].get("tool") != "task"
            or any(call.get("tool") != "read" for call in child_calls)):
        raise PacketError("parent must use only one Task and reviewer only native Read")
    parent_state = _object(parent_calls[0].get("state"), "parent Task state")
    task_input = _object(parent_state.get("input"), "parent Task input")
    if (task_input.get("subagent_type") != "pr-reviewer"
            or task_input.get("background") is True
            or ("task_id" in task_input and task_input.get("task_id") is not None)
            or any(task_input.get(key) not in (None, "") for key in ("model", "provider", "variant"))):
        raise PacketError("parent Task must be fresh foreground pr-reviewer with no model override")
    approval = _object(_object(parent_state.get("metadata"), "parent Task metadata").get("approval"),
                       "parent Task approval")
    rule = _object(approval.get("rule"), "parent Task permission rule")
    if (parent_state.get("status") != "completed" or approval.get("agent") != "code"
            or rule.get("permission") != "task" or rule.get("action") != "allow"):
        raise PacketError("parent native Task was not explicitly authorized")

    allowed = set(runtime.allowed_paths)
    denied = set(runtime.denied_paths)
    denied_normalized: set[str] = set()
    for path in denied:
        candidate = Path(path)
        absolute = candidate if candidate.is_absolute() else runtime.workspace / candidate
        denied_normalized.add(Path(os.path.abspath(absolute)).relative_to(runtime.workspace).as_posix()
                              if Path(os.path.abspath(absolute)).is_relative_to(runtime.workspace)
                              else str(Path(os.path.abspath(absolute))))

    covered: dict[str, dict[int, str]] = {path: {} for path in allowed}
    seen_denied: set[str] = set()
    content_hashes: list[str] = []
    for call in child_calls:
        state = _object(call.get("state"), "reviewer Read state")
        request = _object(state.get("input"), "reviewer Read input")
        raw_path = _string(request.get("filePath"), "reviewer Read path")
        requested = Path(raw_path)
        absolute = Path(os.path.abspath(requested if requested.is_absolute() else runtime.workspace / requested))
        relative = absolute.relative_to(runtime.workspace).as_posix() if absolute.is_relative_to(runtime.workspace) else str(absolute)
        output = state.get("output")
        metadata = _object(state.get("metadata"), "reviewer Read metadata")
        permission = _object(metadata.get("approval"), "reviewer Read approval")
        permission_rule = _object(permission.get("rule"), "reviewer Read permission rule")
        if relative in allowed:
            if (state.get("status") != "completed" or not isinstance(output, str)
                    or permission.get("agent") != "pr-reviewer"
                    or permission_rule.get("permission") != "read"
                    or permission_rule.get("action") != "allow"
                    or permission_rule.get("pattern") != relative):
                raise PacketError(f"authorized Read did not use its exact packet rule: {relative}")
            lines = _read_lines(output)
            if not lines:
                raise PacketError(f"authorized Read returned no numbered packet lines: {relative}")
            if set(covered[relative]) & set(lines):
                raise PacketError(f"reviewer reread an overlapping packet range: {relative}")
            covered[relative].update(lines)
            content_hashes.append(sha256(output.encode()))
            continue
        if relative not in denied_normalized:
            raise PacketError(f"reviewer attempted an unlisted Read path: {raw_path}")
        expected_permission = "read" if absolute.is_relative_to(runtime.workspace) else "external_directory"
        if (state.get("status") != "error" or output is not None
                or permission.get("agent") != "pr-reviewer"
                or permission_rule.get("permission") != expected_permission
                or permission_rule.get("action") != "deny"):
            raise PacketError(f"unauthorized Read was not denied before content: {raw_path}")
        error = state.get("error")
        if isinstance(error, str) and "DENIED PROBE CANARY:" in error:
            raise PacketError("denied Read error leaked synthetic canary content")
        seen_denied.add(relative)

    if preflight:
        if seen_denied != denied_normalized:
            raise PacketError("native Task preflight omitted a required denied escape")
        for path in allowed:
            expected = _expected_line_map(runtime.packet / Path(path).name)
            if covered[path] != expected:
                raise PacketError(f"preflight did not read every complete packet file: {path}")
    else:
        if seen_denied:
            raise PacketError("review Task attempted an out-of-packet Read")
        full_paths = {f"packet/{path}" for path in runtime.inspection.full_read_paths}
        ranged_paths = {f"packet/{path}": ranges for path, ranges in runtime.inspection.review_ranges.items()}
        if full_paths | set(ranged_paths) != allowed:
            raise PacketError("manifest coverage does not match the reviewer workspace packet")
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
                required_lines = {line: actual[line] for line in required}
            if covered[path] != required_lines:
                raise PacketError(f"reviewer packet coverage is incomplete or unindexed: {filename}")

    packet_inventory_sha256 = _verify_workspace_copy(runtime)

    return {
        "status": "NATIVE_TASK_TRACE_PASS",
        "parent_session": parent_id,
        "reviewer_session": child_info.get("id"),
        "parent_tools": [call.get("tool") for call in parent_calls],
        "reviewer_tools": sorted({call.get("tool") for call in child_calls}),
        "allowed_file_count": len(covered),
        "denied_before_content_count": len(seen_denied),
        "read_output_sha256": content_hashes,
        "packet_inventory_sha256": packet_inventory_sha256,
        "preflight": preflight,
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
    external_source_roots: dict[str, Path],
) -> RuntimeWorkspace:
    report = _object(json.loads(report_path.read_bytes()), "prepared runtime report")
    manifest_bytes = manifest_path.read_bytes()
    if report.get("manifest_sha256") != sha256(manifest_bytes):
        raise PacketError("prepared runtime report is bound to a different manifest")
    inspection = validate_packet(
        packet_root,
        manifest_path,
        source_root=source_root,
        external_source_roots=external_source_roots,
    )
    workspace = Path(_string(report.get("workspace"), "prepared workspace"))
    packet = Path(_string(report.get("packet_workspace"), "prepared packet workspace"))
    profile_config = Path(_string(report.get("per_run_kilo_config_dir"), "prepared Kilo config dir"))
    allowed_paths = tuple(_string(p, "allowed packet path") for p in _array(
        report.get("allowed_packet_paths"), "allowed packet paths"))
    denied_paths = tuple(_string(p, "denied probe path") for p in _array(
        report.get("denied_canary_paths"), "denied canary paths"))
    absolute = next((Path(p) for p in denied_paths if Path(p).is_absolute()), None)
    effective = _object(report.get("effective_permissions"), "effective permissions report")
    config_content = _string(report.get("kilo_config_content"), "Kilo config content")
    if report.get("task_created") is not False or report.get("status") != "PRETASK_EFFECTIVE_PERMISSION_PASS":
        raise PacketError("prepared report does not prove pre-Task effective permissions")
    return RuntimeWorkspace(
        workspace=workspace,
        packet=packet,
        inspection=inspection,
        profile_config=profile_config,
        absolute_canary=absolute,
        allowed_paths=allowed_paths,
        denied_paths=denied_paths,
        profile_prompt_sha256=_string(effective.get("effective_prompt_sha256"), "effective prompt hash"),
        profile_file_sha256=_string(effective.get("profile_source_sha256"), "profile source hash"),
        kilo_config_content=config_content,
    )


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate immutable packet and source bindings")
    validate.add_argument("--packet", type=Path, required=True)
    validate.add_argument("--manifest", type=Path, required=True)
    validate.add_argument("--source-root", type=Path, required=True)
    validate.add_argument("--external-root", action="append", default=[])

    permission_config = sub.add_parser("permission-config", help="render exact Kilo permissions")
    permission_config.add_argument("--manifest", type=Path, required=True)

    prepare = sub.add_parser("prepare", help="prepare packet workspace and verify effective Kilo rules")
    prepare.add_argument("--packet", type=Path, required=True)
    prepare.add_argument("--manifest", type=Path, required=True)
    prepare.add_argument("--profile", type=Path, required=True)
    prepare.add_argument("--source-root", type=Path, required=True)
    prepare.add_argument("--external-root", action="append", default=[])
    prepare.add_argument("--workspace-parent", type=Path, required=True)
    prepare.add_argument("--overlay-parent", type=Path, required=True)
    prepare.add_argument("--home", type=Path, required=True)
    prepare.add_argument("--tmpdir", type=Path, required=True)
    prepare.add_argument("--kilo", type=Path, required=True)
    prepare.add_argument("--report", type=Path)
    prepare.add_argument("--preflight", action="store_true")

    audit = sub.add_parser("audit-task", help="audit native Kilo parent/reviewer access trace")
    audit.add_argument("--packet", type=Path, required=True)
    audit.add_argument("--manifest", type=Path, required=True)
    audit.add_argument("--source-root", type=Path, required=True)
    audit.add_argument("--external-root", action="append", default=[])
    audit.add_argument("--prepared-report", type=Path, required=True)
    audit.add_argument("--parent-export", type=Path, required=True)
    audit.add_argument("--reviewer-export", type=Path, required=True)
    audit.add_argument("--expected-model", type=Path)
    audit.add_argument("--report", type=Path)
    audit.add_argument("--preflight", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            result = validate_packet(
                args.packet,
                args.manifest,
                source_root=args.source_root,
                external_source_roots=_external_roots(args.external_root),
            )
            print(json.dumps({"status": "PACKET_VALID", **asdict(result)}, sort_keys=True))
            return 0

        manifest_bytes = args.manifest.read_bytes()
        manifest = _object(json.loads(manifest_bytes), "manifest")
        paths = tuple(
            _relative_file(_object(item, "manifest file row").get("path"))
            for item in _array(manifest.get("files"), "manifest.files")
        )
        if args.command == "permission-config":
            print(make_kilo_config_content(list(paths)))
            return 0

        if args.command == "audit-task":
            runtime = _runtime_from_report(
                args.prepared_report,
                args.packet,
                args.manifest,
                source_root=args.source_root,
                external_source_roots=_external_roots(args.external_root),
            )
            parent_export = _object(json.loads(args.parent_export.read_bytes()), "parent Kilo export")
            reviewer_export = _object(json.loads(args.reviewer_export.read_bytes()), "reviewer Kilo export")
            expected_model = None
            if args.expected_model is not None:
                expected_model = _object(json.loads(args.expected_model.read_bytes()), "expected model binding")
            report = audit_task_trace(
                parent_export,
                reviewer_export,
                runtime=runtime,
                expected_model=expected_model,
                preflight=args.preflight,
            )
            _write_report(args.report, report)
            print(json.dumps(report, sort_keys=True, separators=(",", ":")))
            return 0

        runtime = prepare_task_environment(
            args.packet,
            args.manifest,
            args.profile,
            args.workspace_parent,
            args.overlay_parent,
            source_root=args.source_root,
            external_source_roots=_external_roots(args.external_root),
            preflight=args.preflight,
        )
        effective = verify_effective_permissions(
            kilo_binary=args.kilo,
            overlay=runtime,
            canonical_profile=args.profile,
            home=args.home,
            tmpdir=args.tmpdir,
        )
        report: dict[str, Any] = {
            "status": "PRETASK_EFFECTIVE_PERMISSION_PASS",
            "manifest_sha256": sha256(manifest_bytes),
            "workspace": str(runtime.workspace),
            "packet_workspace": str(runtime.packet),
            "per_run_kilo_config_dir": str(runtime.profile_config),
            "kilo_disable_project_config": "1",
            "kilo_config_content": runtime.kilo_config_content,
            "allowed_packet_paths": list(runtime.allowed_paths),
            "denied_canary_paths": list(runtime.denied_paths),
            "effective_permissions": effective,
            "task_created": False,
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
