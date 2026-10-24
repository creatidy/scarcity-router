#!/usr/bin/env python3
"""Deterministic offline packet/trace validators and temporary test fixtures.

These legacy-format validators are not a development workflow or acceptance authority.
They never start/export a harness session, select/invoke a model or fetch platform
state. Visible Kilo/Codex sessions use Git and canonical acceptance directly; no
local packet or receipt directory is a prerequisite. Fixture creators are confined
to normal OS temporary space outside Git worktrees and must be cleaned by callers.
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


def standalone_packet_path_plan(
    changed_paths: tuple[str, ...] | list[str],
    owner_evidence_paths: tuple[str, ...] | list[str] = (),
) -> tuple[str, ...]:
    """Return the private-review packet file inventory for a sorted PR delta."""
    if not changed_paths or tuple(sorted(set(changed_paths))) != tuple(changed_paths):
        raise PacketError("standalone packet plan requires nonempty sorted unique changed paths")
    for changed_path in changed_paths:
        _relative_source_path(changed_path)
    if tuple(sorted(set(owner_evidence_paths))) != tuple(owner_evidence_paths):
        raise PacketError("standalone owner-evidence paths must be sorted and unique")
    for path in owner_evidence_paths:
        _relative_file(path)
    source_files = tuple(f"source-{index:02d}.md" for index in range(len(changed_paths)))
    paths = (*STANDALONE_PACKET_FIXED_PATHS, *source_files, *owner_evidence_paths)
    if len(set(paths)) != len(paths):
        raise PacketError("standalone packet plan contains duplicate file paths")
    return tuple(sorted(paths))


def validate_standalone_packet_path_plan(inspection: PacketInspection) -> None:
    expected = standalone_packet_path_plan(
        inspection.changed_paths, inspection.owner_evidence_paths
    )
    if inspection.file_paths != expected:
        raise PacketError("standalone packet inventory differs from the complete canonical path plan")


def _temporary_fixture_parent(parent: Path) -> None:
    resolved = parent.resolve()
    if (parent.is_symlink() or not parent.is_dir()
            or not resolved.is_relative_to(Path(tempfile.gettempdir()).resolve())
            or any(part.startswith(".review-evidence-") for part in resolved.parts)):
        raise PacketError("offline fixtures require normal OS temporary space, not review-evidence storage")
    probe = subprocess.run(
        ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if probe.returncode == 0:
        raise PacketError("offline fixtures cannot be created inside a repository worktree")


def create_synthetic_standalone_packet(
    parent: Path,
    *,
    changed_paths: tuple[str, ...] | list[str],
    head: str,
    base: str,
    merge_base: str,
    owner_evidence_paths: tuple[str, ...] | None = None,
    issue_id: int = 1683,
    issue_number: int = 212,
    pull_id: int = 927,
    pull_number: int = 213,
    head_branch: str = "issue-212-d069a-bootstrap",
) -> tuple[Path, Path]:
    """Create a harmless packet with the exact production standalone file-path plan."""
    if owner_evidence_paths is None:
        owner_evidence_paths = OWNER_EVIDENCE_PACKET_PATHS
    paths = standalone_packet_path_plan(changed_paths, owner_evidence_paths)
    if not _SHA1.fullmatch(head) or not _SHA1.fullmatch(base) or not _SHA1.fullmatch(merge_base):
        raise PacketError("synthetic packet refs must be full SHA-1 values")
    _temporary_fixture_parent(parent)
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
                "Synthetic-only fixture: every other listed file is harmless.\n"
                "PR body text is non-authoritative; digest only.\n"
                "<BEGIN-PACKET-READ-PLAN>\n"
                f"{json.dumps(read_plan, indent=2, sort_keys=True)}\n"
                "<END-PACKET-READ-PLAN>\n"
                f'<BEGIN-CANONICAL-ISSUE-BODY issue="{issue_number}" sha256="{issue_body_sha}">\n'
                f"{issue_body.decode()}\n<END-CANONICAL-ISSUE-BODY>"
            )
            contents[path] = text.encode("utf-8")
        elif path == "patch.diff":
            contents[path] = b"diff --git a/synthetic b/synthetic\n+synthetic-only patch\n"
        elif path in {"owner-bootstrap-receipt.txt", "owner-210-receipt.txt"}:
            contents[path] = b"SYNTHETIC Kilo transcription receipt; not owner-authored.\n"
        elif path == "source-00.md":
            contents[path] = (
                b'SYNTHETIC ONLY: literal "<system-reminder>" and "</system-reminder>" '
                b"strings are ordinary frozen packet bytes, not appended context.\n"
            )
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
    owner_source_paths = tuple(path for path in owner_evidence_paths if not path.endswith("-receipt.txt"))
    for index, source_path in enumerate(owner_source_paths):
        receipt_path = f"{source_path.removesuffix('.txt')}-receipt.txt"
        if receipt_path in owner_evidence_paths and receipt_path not in owner_receipts:
            owner_receipts[receipt_path] = (issue_number, 30000 + index, "2026-10-17T22:15:33Z")
    file_rows: list[dict[str, Any]] = []
    for path in paths:
        data = contents[path]
        if path == "index.md":
            kind, origin, revision = "index", "synthetic preflight index", head
        elif path == "patch.diff":
            kind, origin, revision = "patch", "synthetic preflight patch", head
        elif path in owner_receipts:
            receipt_issue_number, comment_id, timestamp = owner_receipts[path]
            kind = "transcription-receipt"
            origin = (
                f"https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/"
                f"{receipt_issue_number}#issuecomment-{comment_id}"
            )
            revision = timestamp
        elif path in owner_evidence_paths:
            kind, origin, revision = "owner-source", "synthetic owner source placeholder", head
        elif path in KILO_SOURCE_PACKET_PATHS:
            source_path = KILO_SOURCE_REPOSITORY_PATHS[path]
            revision = KILO_SOURCE_REVISION
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
            "change_type": "added",
            "base_exists": False,
            "base_blob": None,
            "head_blob": hashlib.sha1(f"synthetic:{source_path}".encode()).hexdigest(),
            "sha256": sha256(contents[packet_path]),
            "ranges": [[1, 1]],
        }
    owner_sources = []
    for index, path in enumerate(owner_source_paths):
        receipt_path = f"{path.removesuffix('.txt')}-receipt.txt"
        record: dict[str, Any] = {
            "packet_path": path,
            "sha256": sha256(contents[path]),
            "source_type": "synthetic_fixture",
            "source_origin": "synthetic-only owner evidence path placeholder",
            "decision_type": f"synthetic permission-preflight owner source {index}",
        }
        receipt_metadata = owner_receipts.get(receipt_path)
        if receipt_path in owner_evidence_paths and receipt_metadata is not None:
            receipt_issue, comment_id, timestamp = receipt_metadata
            record["transcription_receipt"] = {
                "packet_path": receipt_path,
                "comment_id": comment_id,
                "issue_number": receipt_issue,
                "author": "kilo",
                "created_at": timestamp,
                "url": f"https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/{receipt_issue}#issuecomment-{comment_id}",
                "sha256": sha256(contents[receipt_path]),
            }
        owner_sources.append(record)
    external_sources = []
    for path in KILO_SOURCE_PACKET_PATHS:
        source_path = KILO_SOURCE_REPOSITORY_PATHS[path]
        revision = KILO_SOURCE_REVISION
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
    issue_url = f"{repository_url}/issues/{issue_number}"
    pr_url = f"{repository_url}/pulls/{pull_number}"
    manifest = {
        "schema_version": 2,
        "synthetic_fixture": True,
        "repository": {"id": 15, "owner": "BioMedical-IT", "name": "scarcity-router", "url": repository_url},
        "issue": {"id": issue_id, "number": issue_number, "url": issue_url, "body_sha256": issue_body_sha},
        "pull_request": {
            "id": pull_id, "number": pull_number, "url": pr_url,
            "head_branch": head_branch, "base_branch": "develop",
            "head": head, "base": base, "merge_base": merge_base,
            "body_sha256": "b" * 64,
        },
        "issue_state": "open", "pr_state": "open", "pr_merged": False,
        "head": head, "base": base, "merge_base": merge_base,
        "currentness": {
            "checked_at": "2026-10-17T22:15:33Z", "repo_id": 15,
            "issue_id": issue_id, "pr_id": pull_id,
            "head": head, "base": base, "merge_base": merge_base,
        },
        "changed_paths": list(changed_paths),
        "source_bindings": source_bindings,
        "owner_sources": owner_sources,
        "review_policy": {
            "default_maximum": 10,
            "effective_maximum": 10,
            "scope": {"issue_number": issue_number, "pull_number": pull_number},
            "authority_type": "default",
            "authority_source_path": None,
            "authority_source_sha256": None,
        },
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


def _owner_evidence_paths(manifest: dict[str, Any]) -> tuple[str, ...]:
    paths: set[str] = set()
    for raw in _array(manifest.get("owner_sources"), "manifest.owner_sources"):
        owner = _object(raw, "owner source record")
        paths.add(_relative_file(owner.get("packet_path")))
        receipt = owner.get("transcription_receipt")
        if receipt is not None:
            paths.add(_relative_file(_object(receipt, "owner transcription receipt").get("packet_path")))
    return tuple(sorted(paths))


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
    owner_evidence_paths = _owner_evidence_paths(manifest)
    expected_paths = set(standalone_packet_path_plan(changed_paths, owner_evidence_paths))
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
    manifest_records = {
        _relative_file(_object(row, "synthetic manifest file").get("path")):
        _object(row, "synthetic manifest file")
        for row in _array(manifest.get("files"), "synthetic manifest files")
    }
    validate_kilo_source_records(
        _array(manifest.get("external_sources"), "synthetic external sources"),
        manifest_records,
        synthetic=True,
    )
    return validate_packet(
        packet_root, manifest_path, require_external_sources=True, allow_synthetic_fixture=True
    )


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
    owner_evidence_paths: tuple[str, ...]
    source_line_counts: dict[str, int]
    external_source_count: int
    optional_read_paths: tuple[str, ...]
    full_read_paths: tuple[str, ...]
    review_ranges: dict[str, tuple[tuple[int, int], ...]]


@dataclass(frozen=True)
class RuntimeWorkspace:
    workspace: Path
    packet: Path
    inspection: PacketInspection
    source_root: Path | None
    external_source_roots: dict[str, Path]
    synthetic_fixture: bool
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


def validate_standalone_instruction_boundary(
    runtime: RuntimeWorkspace, launch_environment: dict[str, str]
) -> dict[str, Any]:
    """Fail closed if Kilo can discover an unapproved instruction file."""
    if (launch_environment.get("KILO_DISABLE_PROJECT_CONFIG") != "1"
            or launch_environment.get("KILO_CONFIG_CONTENT") != "{}"):
        raise PacketError("standalone instruction isolation requires disabled project config and empty config content")
    if (launch_environment.get("KILO_DISABLE_PRUNE") != "1"
            or launch_environment.get("KILO_DISABLE_AUTOCOMPACT") != "1"):
        raise PacketError("standalone review must preserve evidence without pruning or automatic compaction")
    if {path.name for path in runtime.workspace.iterdir()} != {"packet"}:
        raise PacketError("standalone workspace contains entries outside the frozen packet")

    names = {"AGENTS.md", "CLAUDE.md", "CONTEXT.md"}
    read_scope = [
        path for path in runtime.packet.rglob("*")
        if path.name in names or path.is_symlink()
    ]
    read_scope.extend(
        path for path in (runtime.workspace / name for name in names)
        if path.exists() or path.is_symlink()
    )
    home = Path(launch_environment["HOME"])
    xdg_config = Path(launch_environment["XDG_CONFIG_HOME"])
    kilo_config = Path(launch_environment["KILO_CONFIG_DIR"])
    system_scope = [
        kilo_config / "AGENTS.md",
        xdg_config / "kilo" / "AGENTS.md",
        home / ".claude" / "CLAUDE.md",
    ]
    global_config_files = [xdg_config / "kilo" / name for name in ("kilo.json", "kilo.jsonc")]
    found = [
        path for path in (*read_scope, *system_scope, *global_config_files)
        if path.exists() or path.is_symlink()
    ]
    if found:
        labels = sorted(f"{path.parent.name}/{path.name}" for path in found)
        raise PacketError("standalone instruction boundary found context candidates: " + ", ".join(labels))

    env_config = _object(json.loads(launch_environment["KILO_CONFIG_CONTENT"]), "KILO_CONFIG_CONTENT")
    profile_config = _object(
        json.loads((kilo_config / "kilo.json").read_bytes()), "per-run Kilo config"
    )
    for label, config in (("KILO_CONFIG_CONTENT", env_config), ("per-run Kilo config", profile_config)):
        instructions = config.get("instructions")
        if instructions not in (None, []):
            raise PacketError(f"{label} contains unapproved instruction sources")
    return {
        "status": "PASS",
        "project_config_disabled": True,
        "tool_result_pruning_disabled": True,
        "automatic_compaction_disabled": True,
        "config_instructions_count": 0,
        "read_scope_instruction_candidates_checked": [
            "workspace/packet/{AGENTS.md,CLAUDE.md,CONTEXT.md}",
            "workspace/{AGENTS.md,CLAUDE.md,CONTEXT.md}",
        ],
        "system_instruction_candidates_checked": [
            "KILO_CONFIG_DIR/AGENTS.md",
            "XDG_CONFIG_HOME/kilo/AGENTS.md",
            "HOME/.claude/CLAUDE.md",
            "XDG_CONFIG_HOME/kilo/kilo.json",
            "XDG_CONFIG_HOME/kilo/kilo.jsonc",
        ],
        "found_unapproved_sources": [],
    }


_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
KILO_SOURCE_REVISION = "e2887f08e5a6394299c810ba508f358aaf4c5bc3"
KILO_SOURCE_PACKET_PATHS = (
    "kilo-LICENSE.txt",
    "kilo-agent-config-schema.ts.txt",
    "kilo-config-directory-files.ts.txt",
    "kilo-config-load-order.ts.txt",
    "kilo-config-merge.ts.txt",
    "kilo-permission-resolution.ts.txt",
    "kilo-read-path.ts.txt",
    "kilo-read-cap-constants.ts.txt",
    "kilo-native-denial.ts.txt",
    "kilo-native-deny-path.ts.txt",
    "kilo-native-denial-serialization.ts.txt",
    "kilo-native-error-message.ts.txt",
    "kilo-session-instruction.ts.txt",
    "kilo-session-tools.ts.txt",
    "kilo-task-inheritance.ts.txt",
    "kilo-task-parent.ts.txt",
    "kilo-cli-run-agent-selection.ts.txt",
    "kilo-cli-json-events.ts.txt",
    "kilo-cli-session-export.ts.txt",
    "kilo-project-global.ts.txt",
    "kilo-project-resolve.ts.txt",
    "kilo-session-message-schema.ts.txt",
    "kilo-session-processor-lifecycle.ts.txt",
    "kilo-session-tool-completion.ts.txt",
    "kilo-session-message-order.ts.txt",
    "kilo-session-export-order.ts.txt",
    "kilo-session-user-summary.ts.txt",
    "kilo-session-user-conversion.ts.txt",
    "kilo-board-notice.ts.txt",
    "kilo-evidence-preservation-flags.ts.txt",
    "kilo-evidence-preservation-config.ts.txt",
    "kilo-evidence-preservation-prune.ts.txt",
)
KILO_SOURCE_REPOSITORY_PATHS = {
    "kilo-LICENSE.txt": "LICENSE",
    "kilo-agent-config-schema.ts.txt": "packages/core/src/v1/config/agent.ts",
    "kilo-config-directory-files.ts.txt": "packages/opencode/src/kilocode/config/config.ts",
    "kilo-config-load-order.ts.txt": "packages/opencode/src/config/config.ts",
    "kilo-config-merge.ts.txt": "packages/opencode/src/config/config.ts",
    "kilo-permission-resolution.ts.txt": "packages/opencode/src/permission/index.ts",
    "kilo-read-path.ts.txt": "packages/opencode/src/tool/read.ts",
    "kilo-read-cap-constants.ts.txt": "packages/opencode/src/tool/read.ts",
    "kilo-native-denial.ts.txt": "packages/core/src/v1/permission.ts",
    "kilo-native-deny-path.ts.txt": "packages/opencode/src/permission/index.ts",
    "kilo-native-denial-serialization.ts.txt": "packages/opencode/src/session/processor.ts",
    "kilo-native-error-message.ts.txt": "packages/tui/src/util/error.ts",
    "kilo-session-instruction.ts.txt": "packages/opencode/src/session/instruction.ts",
    "kilo-session-tools.ts.txt": "packages/opencode/src/session/tools.ts",
    "kilo-task-inheritance.ts.txt": "packages/opencode/src/kilocode/tool/task.ts",
    "kilo-task-parent.ts.txt": "packages/opencode/src/tool/task.ts",
    "kilo-cli-run-agent-selection.ts.txt": "packages/opencode/src/cli/cmd/run.ts",
    "kilo-cli-json-events.ts.txt": "packages/opencode/src/cli/cmd/run.ts",
    "kilo-cli-session-export.ts.txt": "packages/opencode/src/cli/cmd/export.ts",
    "kilo-project-global.ts.txt": "packages/opencode/src/project/project.ts",
    "kilo-project-resolve.ts.txt": "packages/core/src/project.ts",
    "kilo-session-message-schema.ts.txt": "packages/schema/src/v1/session.ts",
    "kilo-session-processor-lifecycle.ts.txt": "packages/opencode/src/session/processor.ts",
    "kilo-session-tool-completion.ts.txt": "packages/opencode/src/session/processor.ts",
    "kilo-session-message-order.ts.txt": "packages/opencode/src/session/message-v2.ts",
    "kilo-session-export-order.ts.txt": "packages/opencode/src/session/session.ts",
    "kilo-session-user-summary.ts.txt": "packages/opencode/src/session/summary.ts",
    "kilo-session-user-conversion.ts.txt": "packages/opencode/src/session/message-v2.ts",
    "kilo-board-notice.ts.txt": "packages/opencode/src/kilocode/board/notice.ts",
    "kilo-evidence-preservation-flags.ts.txt": "packages/core/src/flag/flag.ts",
    "kilo-evidence-preservation-config.ts.txt": "packages/opencode/src/config/config.ts",
    "kilo-evidence-preservation-prune.ts.txt": "packages/opencode/src/session/compaction.ts",
}
KILO_REQUIRED_SOURCE_RANGES = {
    "kilo-read-path.ts.txt": (198, 385),
    "kilo-read-cap-constants.ts.txt": (23, 30),
    "kilo-native-denial.ts.txt": (21, 27),
    "kilo-native-deny-path.ts.txt": (216, 249),
    "kilo-native-denial-serialization.ts.txt": (297, 309),
    "kilo-native-error-message.ts.txt": (125, 140),
    "kilo-project-global.ts.txt": (214, 240),
    "kilo-project-resolve.ts.txt": (110, 123),
    "kilo-session-message-schema.ts.txt": (102, 534),
    "kilo-session-processor-lifecycle.ts.txt": (665, 927),
    "kilo-session-tool-completion.ts.txt": (265, 312),
    "kilo-session-message-order.ts.txt": (561, 601),
    "kilo-session-export-order.ts.txt": (1032, 1055),
    "kilo-cli-session-export.ts.txt": (245, 297),
    "kilo-session-user-summary.ts.txt": (105, 145),
    "kilo-session-user-conversion.ts.txt": (320, 495),
    "kilo-board-notice.ts.txt": (1, 25),
    "kilo-evidence-preservation-flags.ts.txt": (4, 57),
    "kilo-evidence-preservation-config.ts.txt": (1049, 1054),
    "kilo-evidence-preservation-prune.ts.txt": (296, 354),
}
# Default owner paths for unit-test fixtures only; production paths come from the manifest.
OWNER_EVIDENCE_PACKET_PATHS = (
    "owner-210-receipt.txt",
    "owner-210.txt",
    "owner-bootstrap-receipt.txt",
    "owner-bootstrap.txt",
)
STANDALONE_PACKET_FIXED_PATHS = (
    "index.md", *KILO_SOURCE_PACKET_PATHS, "patch.diff",
)
_SECRET_PATTERNS = (
    ("private-key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("aws-access-key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9_]{30,}\b")),
    ("gitlab-token", re.compile(rb"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("slack-token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("authorization-token", re.compile(
        rb"(?im)\bAuthorization\s*:\s*(?:token|bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}"
    )),
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


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PacketError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise PacketError(f"non-standard JSON constant is not allowed: {value}")


def _strict_json_loads(text: str, label: str) -> Any:
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except PacketError as err:
        raise PacketError(f"{label} is not strict JSON: {err}") from err
    except json.JSONDecodeError as err:
        raise PacketError(f"{label} is not valid JSON") from err


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
    if not argv or argv[0] != "git":
        raise PacketError("offline validation permits Git only; model/harness subprocesses are prohibited")
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


def validate_kilo_source_records(
    external_sources: list[Any], records: dict[str, dict[str, Any]],
    *, synthetic: bool = False,
) -> None:
    paths = [
        _relative_file(_object(raw, "external source binding").get("packet_path"))
        for raw in external_sources
    ]
    if len(paths) != len(KILO_SOURCE_PACKET_PATHS) or set(paths) != set(KILO_SOURCE_PACKET_PATHS):
        raise PacketError("packet must contain the complete pinned Kilo source set")
    for raw in external_sources:
        external = _object(raw, "external source binding")
        packet_path = _relative_file(external.get("packet_path"))
        record = records.get(packet_path)
        if (record is None or record.get("kind") != "external-source"
                or external.get("repository") != "Kilo-Org/kilocode"
                or external.get("revision") != KILO_SOURCE_REVISION
                or external.get("source_path") != KILO_SOURCE_REPOSITORY_PATHS[packet_path]):
            raise PacketError(f"external source is not the complete pinned Kilo mapping: {packet_path}")
        required_range = KILO_REQUIRED_SOURCE_RANGES.get(packet_path)
        if not synthetic and required_range is not None:
            start, end = external.get("start_line"), external.get("end_line")
            if (type(start) is not int or type(end) is not int
                    or start > required_range[0] or end < required_range[1]):
                raise PacketError(f"required pinned Kilo implementation lines omitted: {packet_path}")


def _git(root: Path, *args: str) -> bytes:
    return _run(["git", *args], root)


COMPACT_DIFF_FLAGS = ("--binary", "--full-index", "--no-ext-diff", "--no-renames", "--unified=3")


def changed_source_ranges(
    root: Path, base: str, head: str, source_path: str, *, deleted: bool = False,
) -> list[list[int]]:
    """Use Git's compact hunk coordinates, including immediate context, on the bound side."""
    patch = _git(root, "diff", *COMPACT_DIFF_FLAGS, f"{base}...{head}", "--", source_path)
    ranges: list[list[int]] = []
    for match in re.finditer(rb"(?m)^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", patch):
        start = int(match[1] if deleted else match[3])
        count = int((match[2] if deleted else match[4]) or b"1")
        if not count:
            continue
        end = start + count - 1
        if ranges and start <= ranges[-1][1] + 1:
            ranges[-1][1] = max(ranges[-1][1], end)
        else:
            ranges.append([start, end])
    return ranges


def _verify_profile_git_binding(profile: Path, source_root: Path, expected_head: str) -> str:
    source_root = source_root.resolve()
    expected_path = source_root / ".kilo/agents/pr-reviewer.md"
    if (source_root.is_symlink() or not source_root.is_dir()
            or profile.resolve() != expected_path.resolve()):
        raise PacketError("standalone profile source must be the tracked profile in the exact checkout")
    if _git(source_root, "rev-parse", "HEAD").decode().strip() != expected_head:
        raise PacketError("standalone profile source checkout HEAD differs from the frozen packet")
    if _git(source_root, "status", "--porcelain"):
        raise PacketError("standalone profile source checkout is not clean")
    source = _git(source_root, "show", f"{expected_head}:.kilo/agents/pr-reviewer.md")
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
    allow_synthetic_fixture: bool = False,
) -> PacketInspection:
    """Validate sealed files, identities, changed-path coverage and source pins."""
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = _object(json.loads(manifest_bytes), "manifest")
    except (OSError, json.JSONDecodeError) as err:
        raise PacketError("packet manifest is missing or malformed") from err

    if "synthetic_fixture" in manifest and type(manifest["synthetic_fixture"]) is not bool:
        raise PacketError("synthetic fixture marker must be a boolean")
    synthetic_marker = manifest.get("synthetic_fixture", False)
    if synthetic_marker is not allow_synthetic_fixture:
        raise PacketError("synthetic fixture mode must be explicit and match packet provenance")
    if not allow_synthetic_fixture and source_root is None:
        raise PacketError("production packet validation requires the frozen source checkout")
    if not allow_synthetic_fixture and not external_source_roots:
        raise PacketError("production packet validation requires pinned external-source checkouts")
    if manifest.get("schema_version") != 2:
        raise PacketError("unsupported reviewer packet manifest version")
    compact_diff = "diff_context" in manifest
    if compact_diff and (type(manifest["diff_context"]) is not int or manifest["diff_context"] != 3):
        raise PacketError("new reviewer packets require exactly three lines of Git diff context")
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
    issue_id = issue.get("id")
    issue_number = issue.get("number")
    pull_id = pull.get("id")
    pull_number = pull.get("number")
    if (type(issue_id) is not int or issue_id <= 0
            or type(issue_number) is not int or issue_number <= 0):
        raise PacketError("manifest Issue ID/number must be positive integers")
    if (type(pull_id) is not int or pull_id <= 0
            or type(pull_number) is not int or pull_number <= 0):
        raise PacketError("manifest PR ID/number must be positive integers")
    repository_url = "https://forgejo.creatidy.com/BioMedical-IT/scarcity-router"
    if issue.get("url") != f"{repository_url}/issues/{issue_number}":
        raise PacketError("manifest issue URL is not canonical")
    head_branch = _string(pull.get("head_branch"), "manifest.pull_request.head_branch")
    if (pull.get("url") != f"{repository_url}/pulls/{pull_number}"
            or not head_branch or any(char.isspace() for char in head_branch)
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
        currentness.get("repo_id") != repo.get("id")
        or currentness.get("issue_id") != issue_id
        or currentness.get("pr_id") != pull_id
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

    required = {"index.md", "patch.diff"}
    if not required.issubset(records):
        raise PacketError("packet is missing acceptance, patch or raw owner-source evidence")
    index_text = inventory["index.md"]["data"].decode("utf-8", errors="strict")
    for marker in ("<BEGIN-CANONICAL-ISSUE-BODY", "<END-CANONICAL-ISSUE-BODY>",
                   "PR body text is non-authoritative"):
        if marker not in index_text:
            raise PacketError(f"index is missing a required canonical binding: {marker}")
    body_marker = re.search(
        r'(?m)^<BEGIN-CANONICAL-ISSUE-BODY issue="(\d+)" sha256="([0-9a-f]{64})">$',
        index_text,
    )
    end_marker = "<END-CANONICAL-ISSUE-BODY>"
    if (body_marker is None or index_text.count("<BEGIN-CANONICAL-ISSUE-BODY") != 1
            or index_text.count(end_marker) != 1):
        raise PacketError("index canonical issue body markers are malformed or duplicated")
    body_start = index_text.find("\n", body_marker.end()) + 1
    body_end = index_text.find(end_marker, body_start)
    if body_end < 0 or body_end == 0 or index_text[body_end - 1] != "\n":
        raise PacketError("canonical issue body closing tag is not a separate final line")
    if index_text[body_end + len(end_marker):].strip():
        raise PacketError("index contains unbound trailing content after the canonical issue body")
    canonical_issue_body = index_text[body_start:body_end - 1]
    issue_body_sha = _string(issue.get("body_sha256"), "issue body digest")
    if (int(body_marker.group(1)) != issue_number
            or body_marker.group(2) != issue_body_sha
            or sha256(canonical_issue_body.encode("utf-8")) != issue_body_sha):
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
    source_binding_paths: set[str] = set()
    source_line_counts: dict[str, int] = {}
    for source_path in changed_paths:
        binding = _object(bindings[source_path], f"source binding {source_path}")
        packet_path = _relative_file(binding.get("packet_path"))
        if packet_path in source_binding_paths:
            raise PacketError(f"multiple changed sources bind to one packet file: {packet_path}")
        source_binding_paths.add(packet_path)
        record = records.get(packet_path)
        if record is None or record.get("kind") != "git-source":
            raise PacketError(f"changed path lacks an indexed Git source: {source_path}")
        change_type = binding.get("change_type", "modified" if binding.get("base_exists") else "added")
        if change_type not in {"added", "modified", "deleted"}:
            raise PacketError(f"changed source change type is invalid: {source_path}")
        source_revision = base if change_type == "deleted" else head
        if record.get("origin") != source_path or record.get("revision") != source_revision:
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
        if base_exists is not (change_type != "added"):
            raise PacketError(f"changed source type contradicts base presence: {source_path}")
        if change_type == "deleted":
            if binding.get("head_blob") is not None:
                raise PacketError(f"deleted source must not claim a HEAD blob: {source_path}")
        elif not _SHA1.fullmatch(_string(binding.get("head_blob"), f"{source_path} head blob")):
            raise PacketError(f"changed source head blob malformed: {source_path}")
        ranges = _array(binding.get("ranges"), f"{source_path} ranges")
        line_count = len(inventory[packet_path]["data"].splitlines())
        if not ranges and line_count and not compact_diff:
            raise PacketError(f"changed source lacks required review ranges: {source_path}")
        source_line_counts[source_path] = line_count
        previous_end = 0
        for raw_range in ranges:
            if (not isinstance(raw_range, list) or len(raw_range) != 2
                    or type(raw_range[0]) is not int or type(raw_range[1]) is not int
                    or raw_range[0] <= previous_end or raw_range[1] < raw_range[0]
                    or raw_range[1] > line_count):
                raise PacketError(f"changed source range is malformed or outside file: {source_path}")
            previous_end = raw_range[1]

    owner_sources = _array(manifest.get("owner_sources"), "manifest.owner_sources")
    owner_records: dict[str, dict[str, Any]] = {}
    receipt_paths: set[str] = set()
    for raw in owner_sources:
        owner = _object(raw, "owner source record")
        packet_path = _relative_file(owner.get("packet_path"))
        if packet_path in owner_records:
            raise PacketError(f"duplicate owner source record: {packet_path}")
        record = records.get(packet_path)
        if record is None or record.get("kind") != "owner-source":
            raise PacketError(f"owner decision is not a raw packet file: {packet_path}")
        if owner.get("sha256") != record.get("sha256"):
            raise PacketError(f"owner decision digest mismatch: {packet_path}")
        source_type = _string(owner.get("source_type"), f"{packet_path} source type")
        if source_type not in {
            "direct_owner_message", "forgejo_owner_comment",
            "forgejo_comment_transcription", "synthetic_fixture",
        }:
            raise PacketError(f"unsupported owner source type: {packet_path}")
        if source_type == "synthetic_fixture" and not allow_synthetic_fixture:
            raise PacketError(f"production owner source cannot be synthetic: {packet_path}")
        _string(owner.get("source_origin"), f"{packet_path} source origin")
        _string(owner.get("decision_type"), f"{packet_path} decision type")
        receipt_raw = owner.get("transcription_receipt")
        if receipt_raw is not None:
            receipt = _object(receipt_raw, f"{packet_path} transcription receipt")
            receipt_path = _relative_file(receipt.get("packet_path"))
            receipt_paths.add(receipt_path)
            receipt_record = records.get(receipt_path)
            if receipt_record is None or receipt_record.get("kind") != "transcription-receipt":
                raise PacketError(f"owner source lacks its indexed transcription receipt: {packet_path}")
            comment_id = receipt.get("comment_id")
            receipt_issue_number = receipt.get("issue_number")
            author = receipt.get("author")
            created_at = _string(receipt.get("created_at"), f"{packet_path} receipt timestamp")
            url = _string(receipt.get("url"), f"{packet_path} receipt URL")
            receipt_sha = _string(receipt.get("sha256"), f"{packet_path} receipt digest")
            if (type(comment_id) is not int or comment_id <= 0
                    or type(receipt_issue_number) is not int or receipt_issue_number <= 0
                    or author != "kilo"
                    or url != f"{repository_url}/issues/{receipt_issue_number}#issuecomment-{comment_id}"
                    or receipt_record.get("origin") != url
                    or receipt_record.get("revision") != created_at
                    or receipt_record.get("sha256") != receipt_sha
                    or not _SHA256.fullmatch(receipt_sha)):
                raise PacketError(f"canonical transcription receipt metadata mismatch: {packet_path}")
            receipt_text = inventory[receipt_path]["data"].decode("utf-8", errors="strict")
            if "Kilo transcription receipt" not in receipt_text or "not owner-authored" not in receipt_text:
                raise PacketError(f"transcription receipt attribution is not explicit: {receipt_path}")
        elif source_type == "forgejo_comment_transcription":
            raise PacketError(f"Forgejo-transcribed owner source lacks its receipt: {packet_path}")
        if source_type == "direct_owner_message":
            if owner.get("owner") != "adrian.tkacz":
                raise PacketError(f"{packet_path} direct-owner source is not attributed to Adrian Tkacz")
            # Context-only historical sources may lack timestamps; authority sources may not.
            if owner.get("source_timestamp") is not None:
                _string(owner.get("source_timestamp"), f"{packet_path} source timestamp")
        if source_type == "forgejo_owner_comment":
            comment_id = owner.get("comment_id")
            owner_issue = owner.get("issue_number")
            author = owner.get("author")
            created_at = _string(owner.get("created_at"), f"{packet_path} owner comment timestamp")
            url = _string(owner.get("url"), f"{packet_path} owner comment URL")
            expected_url = (
                f"{repository_url}/issues/{owner_issue}#issuecomment-{comment_id}"
            )
            if (type(comment_id) is not int or comment_id <= 0
                    or type(owner_issue) is not int or owner_issue <= 0
                    or author != "adrian.tkacz" or url != expected_url
                    or record.get("origin") != url or record.get("revision") != created_at):
                raise PacketError(f"{packet_path} is not a canonical owner-authored Forgejo comment")
        owner_records[packet_path] = owner
    owner_evidence_paths = tuple(sorted(set(owner_records) | receipt_paths))
    indexed_owner_paths = {
        path for path, record in records.items()
        if record.get("kind") in {"owner-source", "transcription-receipt"}
    }
    if indexed_owner_paths != set(owner_evidence_paths):
        raise PacketError("owner-source manifest does not cover the exact indexed owner evidence")

    review_policy = _object(manifest.get("review_policy"), "manifest.review_policy")
    default_maximum = review_policy.get("default_maximum")
    effective_maximum = review_policy.get("effective_maximum")
    policy_scope = _object(review_policy.get("scope"), "review_policy.scope")
    if (default_maximum != 10 or type(effective_maximum) is not int or effective_maximum < 10
            or policy_scope.get("issue_number") != issue_number
            or policy_scope.get("pull_number") != pull_number):
        raise PacketError("review-budget policy has an invalid default, cap or delivery scope")
    authority_type = review_policy.get("authority_type")
    authority_path_raw = review_policy.get("authority_source_path")
    authority_sha = review_policy.get("authority_source_sha256")
    if effective_maximum == default_maximum:
        if (authority_type != "default" or authority_path_raw is not None or authority_sha is not None):
            raise PacketError("default-ten review policy must not claim exception authority")
    else:
        if authority_type not in {"direct_owner_supersession", "d069a_integrated"}:
            raise PacketError("review exception authority type is unsupported")
        authority_path = _relative_file(authority_path_raw)
        authority = owner_records.get(authority_path)
        if authority is None or authority_sha != authority.get("sha256"):
            raise PacketError("review exception lacks a bound raw owner authority source")
        authority_source_type = authority.get("source_type")
        if authority_type == "direct_owner_supersession":
            if (authority_source_type != "direct_owner_message"
                    or authority.get("source_timestamp") is None
                    or authority.get("owner") is None):
                raise PacketError("direct owner exception lacks its raw direct-owner source")
            if (repo.get("id") != 15 or issue_id != 1683 or issue_number != 212
                    or pull_id != 927 or pull_number != 213
                    or effective_maximum != 20
                    or authority_path != "owner-d077-max20.txt"
                    or authority.get("owner") != "adrian.tkacz"
                    or authority.get("source_timestamp") != "2026-10-19T22:41:20Z"):
                raise PacketError("direct D-077 supersession is restricted to its exact #212/#213 max-20 authority")
            if not allow_synthetic_fixture and (
                authority.get("sha256") != "222e3be1eadd381ce7a5a351943e7490a44edf907febea4e2f45fa013a53e4af"
                or authority.get("source_origin")
                != "Adrian direct Kilo message Section 1, 2026-10-19T22:41:20Z"
                or authority.get("decision_type") != "D-069/D-077 max-20 owner review allocation"
            ):
                raise PacketError("direct D-077 supersession does not match the exact owner-source receipt")
        elif authority_source_type not in {"direct_owner_message", "forgejo_owner_comment"}:
            raise PacketError("D-069A exception lacks its canonical owner source")
        authority_scope = _object(authority.get("scope"), f"{authority_path} scope")
        if (authority_scope.get("issue_number") != issue_number
                or authority_scope.get("pull_number") != pull_number
                or authority_scope.get("maximum_reviews") != effective_maximum):
            raise PacketError("direct-owner review ceiling does not match the exact issue/PR scope")
        if authority_type == "direct_owner_supersession":
            authority_text = inventory[authority_path]["data"].decode("utf-8", errors="strict")
            match = re.search(
                r"(?m)^The owner has explicitly approved increasing the maximum review allocation to "
                r"\*\*(\d+) total invocations\*\*, exclusively for Issue #(\d+) / PR #(\d+)\.$",
                authority_text,
            )
            if (match is None or int(match.group(1)) != effective_maximum
                    or int(match.group(2)) != issue_number or int(match.group(3)) != pull_number):
                raise PacketError("direct owner source text does not authorize this exact finite review scope")
        if authority_type == "d069a_integrated":
            integrated_head = _string(review_policy.get("d069a_integrated_head"), "D-069A integrated head")
            if source_root is not None:
                validate_d069a_integration(source_root, integrated_head, base)
            elif not allow_synthetic_fixture:
                raise PacketError("D-069A integration must be verified against the frozen Git base")
            elif not _SHA1.fullmatch(integrated_head):
                raise PacketError("D-069A integration head is not a full Git ID")

    external_sources = _array(manifest.get("external_sources"), "manifest.external_sources")
    if require_external_sources and not external_sources:
        raise PacketError("required pinned external-source evidence is missing")
    if not allow_synthetic_fixture:
        validate_kilo_source_records(external_sources, records)
        if set(external_source_roots or {}) != {"Kilo-Org/kilocode"}:
            raise PacketError("production packet requires the exact pinned Kilo source checkout")
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
    optional_paths = tuple(
        _relative_file(path) for path in _array(coverage.get("optional_read_paths", []), "optional_read_paths")
    )
    if tuple(sorted(set(optional_paths))) != optional_paths:
        raise PacketError("optional-read paths must be a sorted unique list")
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
    mandatory_paths = set(full_read_paths) | set(review_ranges)
    if mandatory_paths & set(optional_paths) or mandatory_paths | set(optional_paths) != set(records):
        raise PacketError("mandatory/optional coverage does not partition the indexed packet files")
    if not compact_diff and not source_binding_paths.issubset(set(full_read_paths)):
        raise PacketError("all changed source files must be covered by full reads")
    if compact_diff:
        for source_path in changed_paths:
            binding = _object(bindings[source_path], f"source binding {source_path}")
            path = binding["packet_path"]
            if path not in full_read_paths and binding["ranges"] != [list(pair) for pair in review_ranges.get(path, ())]:
                raise PacketError(f"mandatory review ranges do not match source binding: {source_path}")
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
    if compact_diff or optional_paths:
        expected_read_plan["optional_read_paths"] = [f"packet/{path}" for path in optional_paths]
    if read_plan != expected_read_plan:
        raise PacketError("index Read plan differs from manifest full paths/ranges")
    applicable_owner_paths = set(owner_evidence_paths)
    if compact_diff:
        applicable_owner_paths = set()
        for owner in owner_records.values():
            scope = owner.get("scope")
            if not isinstance(scope, dict) or scope.get("issue_number") == issue_number:
                applicable_owner_paths.add(owner["packet_path"])
                receipt = owner.get("transcription_receipt")
                if receipt is not None:
                    applicable_owner_paths.add(_object(receipt, "applicable owner receipt")["packet_path"])
    if not (required | applicable_owner_paths).issubset(full_read_paths):
        raise PacketError("review must read the index, patch, raw owner sources and provenance receipts")

    if source_root is not None:
        actual_head = _git(source_root, "rev-parse", "HEAD").decode().strip()
        actual_merge_base = _git(source_root, "merge-base", head, base).decode().strip()
        if actual_head != head or actual_merge_base != merge_base:
            raise PacketError("source checkout head/merge-base differs from packet")
        if _git(source_root, "status", "--porcelain"):
            raise PacketError("source checkout is not clean")
        name_flags = ("--no-renames",) if compact_diff else ()
        actual_changed = _git(source_root, "diff", *name_flags, "--name-only", f"{base}...{head}").decode().splitlines()
        if actual_changed != list(changed_paths):
            raise PacketError("packet changed-path coverage differs from exact Git delta")
        # The absent field retains byte-verification of already frozen historical packets.
        diff_flags = COMPACT_DIFF_FLAGS if compact_diff else ("--binary", "--full-index", "--no-ext-diff", "--unified=80")
        canonical_patch = _git(source_root, "diff", *diff_flags, f"{merge_base}...{head}", "--", *changed_paths)
        if inventory["patch.diff"]["data"] != canonical_patch:
            raise PacketError("packet patch bytes differ from the canonical frozen Git diff")
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
            head_result = subprocess.run(
                ["git", "cat-file", "-e", f"{head}:{source_path}"], cwd=source_root,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, check=False,
            )
            deleted = binding.get("change_type") == "deleted"
            if (head_result.returncode == 0) is deleted:
                raise PacketError(f"HEAD Git path-presence mismatch for {source_path}")
            if not deleted and _git(source_root, "rev-parse", f"{head}:{source_path}").decode().strip() != binding["head_blob"]:
                raise PacketError(f"HEAD Git blob mismatch for {source_path}")
            source_revision = base if deleted else head
            source_bytes = _git(source_root, "show", f"{source_revision}:{source_path}")
            if sha256(source_bytes) != binding["sha256"]:
                raise PacketError(f"source bytes differ from bound Git blob: {source_path}")
            if compact_diff:
                derived_ranges = changed_source_ranges(source_root, base, head, source_path, deleted=deleted)
                if binding["ranges"] != derived_ranges:
                    raise PacketError(f"source review ranges differ from canonical Git hunks: {source_path}")
                if binding["packet_path"] in full_read_paths and derived_ranges != [[1, len(source_bytes.splitlines())]]:
                    raise PacketError(f"full-source requirement exceeds changed hunks/context: {source_path}")

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

    inspection = PacketInspection(
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
        owner_evidence_paths=owner_evidence_paths,
        source_line_counts=source_line_counts,
        external_source_count=len(external_sources),
        optional_read_paths=optional_paths,
        full_read_paths=tuple(sorted(full_read_paths)),
        review_ranges=review_ranges,
    )
    if not allow_synthetic_fixture:
        validate_standalone_packet_path_plan(inspection)
    return inspection


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
) -> dict[str, str]:
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
        permission_patterns[packet_path] = _kilo_permission_pattern(worktree_root, absolute)
    return permission_patterns


def make_per_run_permission_map(
    packet_paths: tuple[str, ...] | list[str], *, workspace: Path,
) -> dict[str, object]:
    """Allow only exact Kilo worktree-relative Read patterns for one packet."""
    permission_patterns = _kilo_packet_path_maps(packet_paths, workspace=workspace)
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
    read_block = re.search(
        r"(?m)^  read:\s*\n(?P<rules>(?: {4}[^\n]*(?:\n|$))*)", frontmatter
    )
    if read_block is None:
        raise PacketError("reviewer Read must have an explicit workspace allowlist")
    for rule in (
        '    "*": allow',
        '    "*.env*": deny',
        '    "**/.env*": deny',
        '    "*.task_progress.md": deny',
        '    "**/.task_progress.md": deny',
        '    ".git": deny',
        '    ".git/**": deny',
        '    "**/.git/**": deny',
        '    ".kilo/agent-manager.json.*.tmp": deny',
        '    "**/.kilo/agent-manager.json.*.tmp": deny',
        '    "**/.config/kilo/**": deny',
        '    "**/.local/share/kilo/**": deny',
        '    "**/.kilocode/**": deny',
        '    "**/tmp/kilo/**": deny',
    ):
        if rule not in read_block.group("rules"):
            raise PacketError("reviewer Read boundary lacks an approved sensitive-path rule")
    for tool in ("edit", "write", "apply_patch", "task", "external_directory"):
        if not re.search(rf"(?m)^  {tool}:\s*deny\s*$", frontmatter):
            raise PacketError(f"reviewer must deny {tool}")
    for rule in (
        "  glob: allow", "  grep: allow", "  list: allow", "  semantic_search: allow",
        '  "forgejo-mcp_get_*": allow', '  "forgejo-mcp_list_*": allow',
        '  "forgejo-mcp_search_*": allow', '  bash:\n    "*": deny',
        '    "git diff --check": allow', '    "*--output*": deny',
        '    "*--ext-diff*": deny', '    "*--textconv*": deny',
        '    "*>*": deny', '    "*<*": deny', '    "*|*": deny',
        '    "*;*": deny', '    "*&*": deny',
    ):
        if rule not in frontmatter:
            raise PacketError("reviewer read-only tool allowlist is incomplete")
    description = re.search(r"(?m)^description:\s*(.+?)\s*$", frontmatter)
    if description is None:
        raise PacketError("canonical reviewer description is missing")
    return raw, description.group(1), parts[2].lstrip("\n").rstrip()


def _verify_overlay_files(runtime: RuntimeWorkspace) -> None:
    root = runtime.profile_config
    if not root.is_dir() or root.is_symlink() or stat.S_IMODE(root.stat().st_mode) != 0o555:
        raise PacketError("per-run Kilo config directory is missing, linked or writable")
    expected = {
        ".gitignore", "kilo.json", "agents", "agents/pr-reviewer.md"
    }
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*")}
    if actual != expected:
        raise PacketError("per-run Kilo config directory contains missing or unexpected entries")
    agents = root / "agents"
    if (not agents.is_dir() or agents.is_symlink()
            or stat.S_IMODE(agents.stat().st_mode) != 0o555):
        raise PacketError("per-run Kilo agents directory is missing, linked or writable")
    expected_hashes = {
        "agents/pr-reviewer.md": runtime.overlay_profile_sha256,
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
    profile_path = root / "agents/pr-reviewer.md"
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
    allow_synthetic_fixture: bool = False,
) -> RuntimeWorkspace:
    """Create an offline permission fixture only; never a live reviewer runtime."""
    if not allow_synthetic_fixture:
        raise PacketError("offline fixture preparation is not a live reviewer workflow")
    _temporary_fixture_parent(workspace_parent)
    _temporary_fixture_parent(overlay_parent)
    inspection = validate_packet(
        packet_root,
        manifest_path,
        source_root=source_root,
        external_source_roots=external_source_roots,
        allow_synthetic_fixture=allow_synthetic_fixture,
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
        canonical_profile_relative = ".kilo/agents/pr-reviewer.md"
        frozen_profile = _git(source_root, "show", f"{inspection.head}:{canonical_profile_relative}")
        if profile_bytes != frozen_profile:
            raise PacketError("effective reviewer source file differs from frozen HEAD")
        source_bindings = _object(
            _object(json.loads(manifest_path.read_bytes()), "manifest").get("source_bindings"),
            "manifest.source_bindings",
        )
        profile_binding_row = source_bindings.get(canonical_profile_relative)
        if profile_binding_row is not None:
            bound_profile = _object(profile_binding_row, "reviewer profile source binding")
            frozen_blob = _git(
                source_root, "rev-parse", f"{inspection.head}:{canonical_profile_relative}"
            ).decode().strip()
            if frozen_blob != bound_profile.get("head_blob"):
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
    overlay_profile = agents / "pr-reviewer.md"
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
        source_root=source_root,
        external_source_roots=external_source_roots or {},
        synthetic_fixture=allow_synthetic_fixture,
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


def verify_effective_standalone_agent(
    reviewer: dict[str, Any], overlay: RuntimeWorkspace,
    canonical_profile: Path,
) -> dict[str, Any]:
    """Check the Kilo v7.8.8 standalone primary profile and exact Read rules."""
    if reviewer.get("name") != "pr-reviewer":
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
    indexed_read_patterns = set(allowed_read_patterns)
    for rule in reviewer_rules:
        if rule.get("action") != "allow":
            continue
        permission_name = rule.get("permission")
        pattern = rule.get("pattern")
        if not isinstance(permission_name, str) or not isinstance(pattern, str):
            raise PacketError("effective permission allow lacks a string permission or path")
        if (not _wildcard_match("read", permission_name)
                or (permission_name == "read" and pattern in indexed_read_patterns)):
            continue
        if _effective_rule(reviewer_rules, "read", pattern) is rule:
            raise PacketError("effective Read allows include an unindexed packet path")
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
        (overlay.profile_config / "agents/pr-reviewer.md").read_bytes()
    )
    if (source_sha != overlay.canonical_profile_sha256
            or overlay_profile_sha != overlay.overlay_profile_sha256
            or prompt_sha != sha256(expected_prompt.encode())):
        raise PacketError("per-run reviewer prompt/profile does not match the canonical source and overlay")
    return {
        "status": "EFFECTIVE_STANDALONE_PERMISSION_PASS",
        "reviewer_agent": "pr-reviewer",
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


def validate_read_context_boundary(output: str, loaded: Any) -> int:
    """Reject Read-added context outside the exact numbered file-content block."""
    if not isinstance(loaded, list) or loaded:
        raise PacketError("allowed Read auto-loaded instruction paths outside its exact result")
    if "<content>\n" not in output:
        raise PacketError("successful Read omitted a delimited content block")
    header = re.match(r"<path>[^\r\n]+</path>\n<type>file</type>\n<content>\n", output)
    if header is None:
        raise PacketError("allowed Read returned unapproved text before the packet content block")
    content_start = header.end() - len("<content>\n")
    content_end = output.find("\n</content>", content_start + len("<content>\n"))
    if content_end < 0:
        raise PacketError("successful Read omitted the content closing boundary")
    suffix = output[content_end + len("\n</content>"):]
    if suffix.strip():
        raise PacketError("allowed Read returned data outside the exact packet content block")
    return len(suffix.encode("utf-8"))


def _read_lines(output: str) -> dict[int, str]:
    start = output.find("<content>\n")
    end = output.find("\n</content>", start + 10)
    if start < 0 or end < 0:
        raise PacketError("successful Read omitted a delimited content block")
    block = output[start + len("<content>\n"):end]
    result: dict[int, str] = {}
    footer_seen = False
    for line in block.splitlines():
        match = re.fullmatch(r"(\d+): (.*)", line)
        if match:
            if footer_seen:
                raise PacketError("successful Read returned numbered lines after its footer")
            line_number = int(match.group(1))
            if line_number in result:
                raise PacketError("successful Read repeated a line number within one output")
            result[line_number] = match.group(2)
            continue
        if line == "":
            continue
        if re.fullmatch(r"\(End of file - total \d+ lines\)", line):
            if footer_seen:
                raise PacketError("successful Read returned multiple footers")
            footer_seen = True
            continue
        if re.fullmatch(
            r"\(Showing lines \d+-\d+ of \d+\. Use offset=\d+ to continue\.\)", line
        ) or re.fullmatch(
            r"\(Output capped at 50 KB\. Showing lines \d+-\d+\. Use offset=\d+ to continue\.\)",
            line,
        ):
            if footer_seen:
                raise PacketError("successful Read returned multiple footers")
            footer_seen = True
            continue
        raise PacketError("successful Read returned unnumbered content")
    return result


def _read_footer(output: str) -> str:
    start = output.find("<content>\n")
    end = output.find("\n</content>", start + len("<content>\n"))
    if start < 0 or end < 0:
        raise PacketError("successful Read omitted a delimited content block")
    footers = [
        line for line in output[start + len("<content>\n"):end].splitlines()
        if (re.fullmatch(r"\(End of file - total \d+ lines\)", line)
            or re.fullmatch(r"\(Showing lines \d+-\d+ of \d+\. Use offset=\d+ to continue\.\)", line)
            or re.fullmatch(r"\(Output capped at 50 KB\. Showing lines \d+-\d+\. Use offset=\d+ to continue\.\)", line))
    ]
    if len(footers) != 1:
        raise PacketError("successful Read omitted or duplicated Kilo pagination metadata")
    return footers[0]


def validate_read_display(
    *,
    packet_path: str,
    expected_absolute: str,
    output: str,
    lines: dict[int, str],
    display: dict[str, Any],
    packet_line_count: int,
) -> None:
    if display.get("path") != expected_absolute or display.get("type") != "file":
        raise PacketError(f"allowed Read display path differs from the sealed packet: {packet_path}")
    start = display.get("lineStart")
    end = display.get("lineEnd")
    display_total = display.get("totalLines")
    truncated = display.get("truncated")
    text = display.get("text")
    if (not isinstance(start, int) or isinstance(start, bool)
            or not isinstance(end, int) or isinstance(end, bool)
            or not isinstance(display_total, int) or isinstance(display_total, bool)
            or not isinstance(truncated, bool) or not isinstance(text, str)):
        raise PacketError(f"allowed Read display metadata is malformed: {packet_path}")
    ordered = sorted(lines.items())
    if packet_line_count == 0:
        if (ordered or start != 1 or end != 0 or display_total != 0 or truncated or text
                or _read_footer(output) != "(End of file - total 0 lines)"):
            raise PacketError(f"empty-source Read differs from pinned Kilo empty-file output: {packet_path}")
        return
    if (not ordered or start != ordered[0][0] or end != ordered[-1][0]
            or ordered != [(line, lines[line]) for line in range(start, end + 1)]
            or text != "\n".join(content for _, content in ordered)):
        raise PacketError(f"allowed Read display text/range differs from returned packet lines: {packet_path}")
    if display_total < end or display_total > packet_line_count + 1:
        raise PacketError(f"allowed Read display count is outside the frozen packet: {packet_path}")

    footer = _read_footer(output)
    end_match = re.fullmatch(r"\(End of file - total (\d+) lines\)", footer)
    page_match = re.fullmatch(
        r"\(Showing lines (\d+)-(\d+) of (\d+)\. Use offset=(\d+) to continue\.\)", footer
    )
    capped_match = re.fullmatch(
        r"\(Output capped at 50 KB\. Showing lines (\d+)-(\d+)\. Use offset=(\d+) to continue\.\)",
        footer,
    )
    if end_match:
        total = int(end_match.group(1))
        if (truncated or display_total != total
                or end != total or total != packet_line_count):
            raise PacketError(f"Kilo end-of-file metadata is inconsistent with the packet: {packet_path}")
        return
    if page_match:
        page_start, page_end, total, next_offset = map(int, page_match.groups())
        if (not truncated or (page_start, page_end) != (start, end)
                or total != display_total or next_offset != end + 1
                or total > packet_line_count + 1):
            raise PacketError(f"Kilo pagination metadata is inconsistent with the packet: {packet_path}")
        return
    if capped_match:
        page_start, page_end, next_offset = map(int, capped_match.groups())
        if (not truncated or (page_start, page_end) != (start, end)
                or display_total != end + 1 or next_offset != end + 1
                or end > packet_line_count + 1):
            raise PacketError(f"Kilo output-cap metadata is inconsistent with the packet: {packet_path}")
        return
    raise PacketError(f"unsupported Kilo Read footer metadata: {packet_path}")


def _expected_line_map(path: Path) -> dict[int, str]:
    return {index: line for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)}


def _verify_workspace_copy(runtime: RuntimeWorkspace) -> str:
    source_packet = Path(runtime.inspection.packet_root)
    manifest_path = Path(runtime.inspection.manifest_path)
    current = validate_packet(
        source_packet,
        manifest_path,
        source_root=runtime.source_root,
        profile_source_root=runtime.profile_source_root,
        external_source_roots=runtime.external_source_roots,
        require_external_sources=True,
        allow_synthetic_fixture=runtime.synthetic_fixture,
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


def _kilo_permission_path_map(runtime: RuntimeWorkspace) -> dict[str, str]:
    return _kilo_packet_path_maps(
        [Path(path).name for path in runtime.allowed_paths], workspace=runtime.workspace
    )


def standalone_review_prompt(runtime: RuntimeWorkspace) -> str:
    """Build a context-minimal prompt; the indexed packet is the only PR evidence."""
    inspection = runtime.inspection
    if not all(_SHA1.fullmatch(value) for value in (
        inspection.head, inspection.base, inspection.merge_base,
    )):
        raise PacketError("standalone review prompt requires exact repository identity and refs")
    paths = "\n".join(
        f"- `{packet_path}`"
        for packet_path in runtime.allowed_paths
    )
    return (
        "Review the complete frozen pull request independently using only `packet/index.md` "
        "and its indexed packet files. Use only the exact relative Read `filePath` values "
        "listed below; Kilo 7.8.8 resolves them against the session directory before checking "
        "the exact per-file permission rule. Do not construct or alter file paths. "
        "Read the index first, then inspect every mandatory file/range yourself. Optional "
        "references are available for material questions, not compulsory full-file reads. Do not infer "
        "evidence from the primary or outside this packet.\n"
        f"Exact relative native Read filePath values:\n{paths}\n\n"
        f"Repository: {inspection.repository}\nIssue: #{inspection.issue_number}\n"
        f"PR: #{inspection.pull_number}\nHEAD: {inspection.head}\n"
        f"Base: {inspection.base}\nMerge-base: {inspection.merge_base}\n\n"
        "Assess correctness, regressions, architecture, security/privacy, UX and tests by "
        "source inspection. Do not run tests or claim direct Forgejo/Git verification. "
        "Return only the exact JSON result contract in your reviewer profile."
    )


def standalone_preflight_prompt(runtime: RuntimeWorkspace, *, short: bool = False) -> str:
    if not runtime.denied_paths or set(runtime.probe_labels) != set(runtime.denied_paths):
        raise PacketError("synthetic standalone preflight requires the complete labeled escape set")
    short_paths = ("packet/index.md", "packet/source-00.md")
    if short and not set(short_paths).issubset(runtime.allowed_paths):
        raise PacketError("short preflight requires the index and first changed-source file")
    read_paths = short_paths if short else runtime.allowed_paths
    allowed_paths = "\n".join(f"- `{path}`" for path in read_paths)
    denied_paths = "\n".join(
        f"- `{path}` ({runtime.probe_labels[path]})" for path in runtime.denied_paths
    )
    read_scope = "the index and first changed-source file" if short else "every indexed packet file"
    return (
        "Synthetic permission preflight only; this is not a PR review and consumes no review "
        "ordinal. Kilo 7.8.8 resolves relative Read `filePath` values against the session "
        "directory before checking the exact per-file permission rule. Read "
        f"{read_scope} exactly once with no partial ranges, in this order, using only these "
        "indexed relative paths:\n"
        f"{allowed_paths}\n"
        "Then attempt one native Read for each of these denied paths, in order, using the exact "
        "path string shown:\n"
        f"{denied_paths}\n"
        "Do not enumerate, search, delegate, retry, or use another tool. Do not repeat any "
        "synthetic canary value. In `allowed_paths`, return the exact relative packet paths "
        "above in order. Return only JSON with keys `preflight`, `allowed_paths`, "
        "`complete_read_count`, `probe_results`, and `unauthorized_bytes_returned`. Each probe "
        "result must contain its exact `path`, `result` equal to `DENIED_BEFORE_CONTENT`, and "
        "`bytes_returned` equal to false. Set `preflight` to PASS only if every allowed read "
        "was complete and every listed probe was denied before content."
    )


def _final_assistant_text(session: dict[str, Any], agent_name: str) -> str:
    messages = _array(session.get("messages"), "session messages")
    if not messages:
        raise PacketError("standalone session has no terminal reviewer response")
    final_message = _object(messages[-1], "terminal reviewer message")
    final_info = _object(final_message.get("info"), "terminal reviewer info")
    final_time = _object(final_info.get("time"), "terminal reviewer time")
    created = final_time.get("created")
    completed = final_time.get("completed")
    if (final_info.get("role") != "assistant" or final_info.get("agent") != agent_name
            or final_info.get("finish") != "stop" or final_info.get("error") is not None
            or type(created) is not int or type(completed) is not int
            or created < 0 or completed < created):
        raise PacketError("reviewer response is not successfully completed and terminal")
    final_parts = [_object(part, "terminal reviewer part")
                   for part in _array(final_message.get("parts"), "terminal reviewer parts")]
    text_parts = [part for part in final_parts if part.get("type") == "text"]
    if not text_parts or any(part.get("type") == "tool" for part in final_parts):
        raise PacketError("terminal reviewer response has no text or contains tool activity")
    text_start = completed
    previous_text_end = created
    for part in text_parts:
        timing = _object(part.get("time"), "terminal text time")
        start, end = timing.get("start"), timing.get("end")
        if (type(start) is not int or type(end) is not int
                or not created <= previous_text_end <= start <= end <= completed):
            raise PacketError("terminal text temporal provenance is incomplete or inconsistent")
        previous_text_end = end
        text_start = min(text_start, start)
    user_id: str | None = None
    seen_ids: set[str] = set()
    previous_created = -1
    for position, raw_message in enumerate(messages):
        message = _object(raw_message, "session message")
        info = _object(message.get("info"), "session message info")
        message_id = _string(info.get("id"), "message id")
        timing = _object(info.get("time"), "message time")
        message_created = timing.get("created")
        if (message_id in seen_ids or type(message_created) is not int
                or message_created < 0
                or not previous_created <= message_created <= created):
            raise PacketError("reviewer message order has incomplete temporal provenance")
        seen_ids.add(message_id)
        previous_created = message_created
        if info.get("role") == "user" and position == 0:
            user_id = message_id
            continue
        if info.get("role") != "assistant" or info.get("agent") != agent_name:
            raise PacketError("standalone session contains a different assistant identity")
        if user_id is None or info.get("parentID") != user_id:
            raise PacketError("reviewer response does not answer the single fresh user prompt")
        message_completed = timing.get("completed")
        if type(message_completed) is not int or message_completed < message_created:
            raise PacketError("reviewer message completion provenance is missing")
        for raw_part in _array(message.get("parts"), "assistant parts"):
            part = _object(raw_part, "assistant part")
            if part.get("type") != "tool":
                continue
            state = _object(part.get("state"), "temporal tool state")
            tool_time = _object(state.get("time"), "tool temporal provenance")
            start, end = tool_time.get("start"), tool_time.get("end")
            if (type(start) is not int or type(end) is not int
                    or not message_created <= start <= end <= message_completed <= text_start):
                raise PacketError("Read did not complete before the terminal verdict")
    return "".join(_string(part.get("text"), "terminal text") for part in text_parts).strip()


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
    user_info = _object(user_messages[0].get("info"), "initial user metadata")
    if any(key in user_info for key in ("system", "tools", "editorContext", "format")):
        raise PacketError("standalone user metadata contains unapproved out-of-band context")
    # Kilo adds this empty UI diff summary after execution; it is not model input.
    if "summary" in user_info and user_info["summary"] != {"diffs": []}:
        raise PacketError("standalone user metadata contains unapproved out-of-band context")
    parts = _array(user_messages[0].get("parts"), "initial user message parts")
    if any(_object(part, "user prompt part").get("type") != "text" for part in parts):
        raise PacketError("standalone reviewer initial input contains non-text context")
    if any(_object(part, "user text").get("synthetic")
           or _object(part, "user text").get("ignored")
           or _object(part, "user text").get("metadata") not in (None, {}) for part in parts):
        raise PacketError("standalone user text contains unapproved synthetic or metadata context")
    return "".join(
        _string(_object(part, "user prompt part").get("text"), "user prompt text")
        for part in parts
    )


def validate_review_result(text: str, inspection: PacketInspection) -> dict[str, Any]:
    try:
        result = _object(_strict_json_loads(text, "standalone reviewer result"), "standalone reviewer result")
    except PacketError as err:
        raise PacketError("standalone reviewer result is not a single strict JSON object") from err
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
        finding_path = _string(finding.get("file"), "reviewer finding file")
        if finding_path not in changed_paths:
            raise PacketError("standalone reviewer finding is not bound to a changed PR path")
        start = finding.get("line_start")
        end = finding.get("line_end")
        if (not isinstance(start, int) or isinstance(start, bool) or start < 1
                or not isinstance(end, int) or isinstance(end, bool) or end < start):
            raise PacketError("standalone reviewer finding has an invalid line range")
        source_line_count = inspection.source_line_counts.get(finding_path)
        if source_line_count is None or end > source_line_count:
            raise PacketError("standalone reviewer finding line range exceeds its frozen HEAD source")
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


def validate_d069a_integration(source_root: Path, integrated_head: str, base: str) -> None:
    if not _SHA1.fullmatch(integrated_head) or not _SHA1.fullmatch(base):
        raise PacketError("D-069A integration/base refs must be full Git IDs")
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", integrated_head, base],
        cwd=source_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        raise PacketError("D-069A exception policy is not integrated into the frozen base")
    required_markers = {
        "docs/decisions.md": (
            "### D-069A - Canonical owner-authorized finite review exceptions",
        ),
        "tools/reviewer_packet.py": (
            'authority_type == "d069a_integrated"',
            "validate_d069a_integration",
        ),
        ".kilo/command/finish-pr.md": (
            "D-069A finite exception",
            "integrated in freshly fetched",
        ),
        "tests/test_reviewer_packet.py": (
            "test_d069a_integration_head_must_be_ancestor_of_frozen_base",
        ),
    }
    for source_path, markers in required_markers.items():
        for revision in dict.fromkeys((integrated_head, base)):
            blob = subprocess.run(
                ["git", "show", f"{revision}:{source_path}"],
                cwd=source_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if blob.returncode != 0:
                raise PacketError(f"integrated D-069A source is missing from frozen ref: {source_path}")
            content = blob.stdout.decode("utf-8", errors="strict")
            if any(marker not in content for marker in markers):
                raise PacketError(f"frozen ref lacks the accepted D-069A implementation marker: {source_path}")


def validate_preflight_result(
    text: str, runtime: RuntimeWorkspace, expected_paths: tuple[str, ...],
) -> dict[str, Any]:
    try:
        result = _object(_strict_json_loads(text, "standalone preflight result"), "standalone preflight result")
    except PacketError as err:
        raise PacketError("standalone preflight response is not one strict JSON object") from err
    if set(result) != {
        "preflight", "allowed_paths", "complete_read_count", "probe_results",
        "unauthorized_bytes_returned",
    }:
        raise PacketError("standalone preflight result has missing or unexpected fields")
    if (result.get("preflight") != "PASS"
            or result.get("allowed_paths") != list(expected_paths)
            or result.get("complete_read_count") != len(expected_paths)
            or result.get("unauthorized_bytes_returned") is not False):
        raise PacketError("standalone preflight did not report the exact allowed reads and denial result")
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
    short_preflight: bool = False,
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
        loaded = metadata.get("loaded") if isinstance(metadata, dict) else None
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
            "metadata_loaded_paths": loaded if isinstance(loaded, list) else None,
            "content_output_present": output not in (None, ""),
            "content_output_sha256": sha256(output.encode()) if isinstance(output, str) else None,
        })
    final_summary: dict[str, object] = {"parsed": False}
    try:
        final_text = _final_assistant_text(session, "pr-reviewer")
        final_result = _object(
            _strict_json_loads(final_text, "failed standalone final response"),
            "failed standalone final response",
        )
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
        "short_preflight": preflight and short_preflight,
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
    short_preflight: bool = False,
) -> dict[str, Any]:
    """Audit a fresh standalone primary session and native Read trace without echoing bytes."""
    _verify_overlay_files(runtime)
    _verify_probe_inventory(runtime)
    info = _object(session.get("info"), "standalone session info")
    agent_name = "pr-reviewer"
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
    calls = _tool_calls(session)
    tool_names = [_string(call.get("tool"), "standalone tool name") for call in calls]
    if any(tool != "read" for tool in tool_names):
        raise PacketError("standalone reviewer used a tool other than native Read")
    visible_calls: set[int] = set()
    for raw_message in _array(session.get("messages"), "standalone messages"):
        message = _object(raw_message, "model-bound message")
        message_info = _object(message.get("info"), "message model info")
        if message_info.get("agent") != agent_name:
            raise PacketError("message agent differs from the frozen reviewer")
        dispatch = (_object(message_info.get("model"), "user model provenance")
                    if message_info.get("role") == "user" else message_info)
        if ({"provider_id": dispatch.get("providerID"), "model_id": dispatch.get("modelID"),
             "variant": dispatch.get("variant")} != expected_model):
            raise PacketError("message model/provider/variant provenance differs from frozen binding")
        if message_info.get("role") == "assistant" and message_info.get("summary"):
            raise PacketError("compacted summary context does not establish original evidence visibility")
        parts = _array(message.get("parts"), "model-bound parts")
        error = message_info.get("error")
        # Kilo message-v2.ts:373-380 drops errored messages except meaningful aborts.
        replayed = error is None or (
            _object(error, "assistant error").get("name") == "MessageAbortedError"
            and any(_object(part, "assistant part").get("type") not in {"step-start", "reasoning"}
                    for part in parts)
        )
        for raw_part in parts:
            part = _object(raw_part, "model-bound part")
            if part.get("type") == "tool" and replayed:
                state = _object(part.get("state"), "visible tool state")
                if not _object(state.get("time"), "visible tool time").get("compacted"):
                    visible_calls.add(id(part))
            if part.get("type") == "step-finish" and "model" in part:
                step_model = _object(part["model"], "step-finish model provenance")
                if (step_model.get("providerID") != expected_model["provider_id"]
                        or step_model.get("modelID") != expected_model["model_id"]):
                    raise PacketError("step-finish model provenance conflicts with frozen binding")
    if info.get("projectID") != "global":
        raise PacketError("standalone packet workspace did not resolve as Kilo's non-VCS global project")
    if short_preflight and not preflight:
        raise PacketError("short standalone preflight cannot be used for a PR review")
    permission_path_map = _kilo_permission_path_map(runtime)
    allowed_permission_paths = set(permission_path_map.values())
    session_permissions = info.get("permission", [])
    for raw_rule in _array(session_permissions, "standalone session permissions"):
        rule = _object(raw_rule, "standalone session permission")
        if rule.get("action") == "allow" and (
            rule.get("permission") != "read" or rule.get("pattern") not in allowed_permission_paths
        ):
            raise PacketError("standalone saved-session permission reopens a capability or path")

    final_text = _final_assistant_text(session, agent_name)
    allowed = set(runtime.allowed_paths)
    allowed_by_input = {path: path for path in runtime.allowed_paths}
    preflight_paths = runtime.allowed_paths
    if short_preflight:
        preflight_paths = ("packet/index.md", "packet/source-00.md")
        if not set(preflight_paths).issubset(allowed):
            raise PacketError("short standalone preflight lacks its exact synthetic read paths")
    denied = set(runtime.denied_paths)
    covered: dict[str, dict[int, str]] = {path: {} for path in allowed}
    sealed_packet = Path(runtime.inspection.packet_root)
    sealed_lines = {
        path: _expected_line_map(sealed_packet / Path(path).name)
        for path in runtime.allowed_paths
    }
    seen_denied: set[str] = set()
    seen_visible: set[str] = set()
    read_records: list[dict[str, Any]] = []
    repeated_read_line_count = 0
    for call in calls:
        state = _object(call.get("state"), "standalone Read state")
        request = _object(state.get("input"), "standalone Read input")
        raw_path = _string(request.get("filePath"), "standalone Read path")
        output = state.get("output")
        metadata = _object(state.get("metadata"), "standalone Read metadata")
        # Pinned conversion also replays attachments, interrupted output and board notices.
        if state.get("attachments") not in (None, []):
            raise PacketError("sealed text Read returned additional attachment context")
        if metadata.get("loaded") not in (None, []):
            raise PacketError("Read auto-loaded instruction paths outside its exact result")
        if (state.get("status") == "error" and metadata.get("interrupted") is True
                and isinstance(metadata.get("output"), str) and metadata["output"]):
            raise PacketError("Read returned interrupted error output outside the sealed text channel")
        notice = metadata.get("shared_agent_board_notice")
        if state.get("status") == "completed" and (notice == "unavailable" or (
            isinstance(notice, (int, float)) and not isinstance(notice, bool)
            and 0 < notice <= 2**53 - 1 and int(notice) == notice
        )):
            raise PacketError("Read metadata would replay additional shared-board notice context")
        approval = _object(metadata.get("approval"), "standalone Read permission decision")
        permission_rule = _object(approval.get("rule"), "standalone Read permission rule")
        if approval.get("agent") != agent_name:
            raise PacketError("native Read trace is not bound to the standalone reviewer profile")

        packet_path = allowed_by_input.get(raw_path)
        if packet_path is not None:
            if (permission_rule.get("permission") != "read"
                    or permission_rule.get("action") != "allow"
                    or permission_rule.get("pattern") != permission_path_map[packet_path]):
                raise PacketError(f"allowed Read did not use its exact runtime rule: {raw_path}")
            if state.get("status") == "error":
                if output not in (None, ""):
                    raise PacketError(f"failed allowed Read returned unexpected content: {packet_path}")
                if metadata.get("loaded") not in (None, []):
                    raise PacketError("failed allowed Read loaded unapproved instruction context")
                raise PacketError(f"allowed Read tool errored before packet content: {packet_path}")
            if state.get("status") != "completed" or not isinstance(output, str):
                raise PacketError(f"allowed Read has an invalid completion result: {packet_path}")
            expected_absolute = str((runtime.packet / Path(packet_path).name).resolve())
            output_path = re.search(r"(?m)^<path>(.*)</path>$", output)
            output_type = re.search(r"(?m)^<type>(.*)</type>$", output)
            if (output_path is None or output_path.group(1) != expected_absolute
                    or output_type is None or output_type.group(1) != "file"):
                raise PacketError(f"allowed Read output path differs from its exact packet file: {packet_path}")
            output_suffix_bytes = validate_read_context_boundary(output, metadata.get("loaded"))
            lines = _read_lines(output)
            if not lines and sealed_lines[packet_path]:
                raise PacketError(f"allowed Read returned no numbered packet lines: {raw_path}")
            display = _object(metadata.get("display"), "standalone Read display metadata")
            validate_read_display(
                packet_path=packet_path,
                expected_absolute=expected_absolute,
                output=output,
                lines=lines,
                display=display,
                packet_line_count=len(sealed_lines[packet_path]),
            )
            credited = id(call) in visible_calls
            if credited:
                seen_visible.add(packet_path)
            overlap_for_call = 0
            for line_number, text in lines.items():
                expected_text = sealed_lines[packet_path].get(line_number)
                if expected_text is None or text != expected_text:
                    raise PacketError(f"allowed Read bytes differ from immutable packet: {packet_path}:{line_number}")
                previous = covered[packet_path].get(line_number)
                if previous is not None:
                    if previous != text:
                        raise PacketError(
                            f"repeated Read returned different immutable packet bytes: {packet_path}:{line_number}"
                        )
                    repeated_read_line_count += 1
                    overlap_for_call += 1
                elif credited:
                    covered[packet_path][line_number] = text
            read_records.append({
                "path": packet_path,
                "requested_path": raw_path,
                "permission_pattern": permission_rule.get("pattern"),
                "status": "completed",
                "line_start": min(lines, default=1),
                "line_end": max(lines, default=0),
                "line_count": len(lines),
                "overlap_line_count": overlap_for_call,
                "output_sha256": sha256(output.encode("utf-8")),
                "metadata_loaded_paths": metadata["loaded"],
                "output_suffix_bytes": output_suffix_bytes,
                "coverage_credited": credited,
            })
            continue

        if raw_path not in denied:
            raise PacketError(f"standalone reviewer attempted an unlisted Read path: {raw_path}")
        if raw_path in seen_denied:
            raise PacketError(f"standalone reviewer repeated a denied probe: {raw_path}")
        if state.get("status") != "error":
            raise PacketError(f"unauthorized Read was not denied by Kilo: {raw_path}")
        label = runtime.probe_labels[raw_path]
        permission = "read" if label == "symlink_to_packet" else "external_directory"
        expected_rule = {"permission": permission, "pattern": "*", "action": "deny"}
        expected_approval: dict[str, Any] = {"source": "agent", "agent": agent_name, "rule": expected_rule}
        if permission == "external_directory":
            expected_approval.update({
                "outsideWorkspace": True,
                "outsideWorkspacePath": str((runtime.workspace / raw_path).resolve()),
            })
        if approval != expected_approval:
            raise PacketError(f"unauthorized Read lacks its exact native deny rule/path binding: {raw_path}")
        # PermissionDeniedError.message -> errorMessage(error), observed in all six native probes.
        expected_denial = (
            "The user has specified a rule which prevents you from using this specific tool call. "
            + "Here are some of the relevant rules "
            + json.dumps({**expected_rule, "source": "agent"}, separators=(",", ":"), ensure_ascii=False)
        )
        if output not in (None, "") or state.get("error") != expected_denial:
            raise PacketError(f"unauthorized Read returned an unsupported native denial representation or output: {raw_path}")
        seen_denied.add(raw_path)
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
        allowed_sequence = list(dict.fromkeys(
            record["path"] for record in read_records if record["status"] == "completed"
        ))
        denied_sequence = [
            record["path"] for record in read_records if record["status"] == "denied_before_content"
        ]
        if (list(dict.fromkeys(allowed_sequence)) != list(preflight_paths)
                or denied_sequence != list(runtime.denied_paths)):
            raise PacketError("standalone preflight did not follow the frozen read/probe order")
        for path in preflight_paths:
            expected = sealed_lines[path]
            if path not in seen_visible or covered[path] != expected:
                raise PacketError(f"standalone preflight did not read the complete packet file: {path}")
        final_result = validate_preflight_result(
            final_text, runtime, preflight_paths
        )
    else:
        if seen_denied:
            raise PacketError("standalone PR review attempted an out-of-packet Read")
        full_paths = {f"packet/{path}" for path in runtime.inspection.full_read_paths}
        ranged_paths = {
            f"packet/{path}": ranges
            for path, ranges in runtime.inspection.review_ranges.items()
        }
        optional_paths = {f"packet/{path}" for path in runtime.inspection.optional_read_paths}
        if full_paths | set(ranged_paths) | optional_paths != allowed:
            raise PacketError("manifest coverage does not match the standalone packet")
        for path in full_paths | set(ranged_paths):
            filename = Path(path).name
            actual = sealed_lines[path]
            if path not in seen_visible:
                raise PacketError(f"standalone reviewer coverage is incomplete; required file Read omitted: {path}")
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
            if not required_lines.items() <= covered[path].items():
                raise PacketError(f"standalone reviewer packet coverage is incomplete: {filename}")
        final_result = validate_review_result(
            final_text, runtime.inspection
        )

    packet_inventory_sha256 = _verify_workspace_copy(runtime)
    return {
        "status": "STANDALONE_PREFLIGHT_TRACE_PASS" if preflight else "STANDALONE_REVIEW_TRACE_PASS",
        "reviewer_session": session_id,
        "reviewer_agent": agent_name,
        "reviewer_parent_session": None,
        "terminal_response_and_read_order": "PASS",
        "message_model_binding": "PASS",
        "user_context_boundary": "PASS",
        "model_visible_evidence_coverage": "PASS",
        "reviewer_directory": str(runtime.workspace),
        "model_dispatch": actual_model,
        "runtime_identity": "DISPATCH_VERIFIED",
        "runtime_backend_identity": "not independently observed",
        "session_export_sha256": session_export_sha256,
        "reviewer_tools": sorted(set(tool_names)),
        "allowed_file_count": len(preflight_paths) if preflight else len(covered),
        "mandatory_file_count": len(runtime.inspection.full_read_paths) + len(runtime.inspection.review_ranges),
        "denied_before_content_count": len(seen_denied),
        "repeated_read_line_count": repeated_read_line_count,
        "read_trace": read_records,
        "packet_inventory_sha256": packet_inventory_sha256,
        "preflight": preflight,
        "short_preflight": preflight and short_preflight,
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














def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="offline validation of caller-supplied immutable data; no model execution")
    validate.add_argument("--packet", type=Path, required=True)
    validate.add_argument("--manifest", type=Path, required=True)
    validate.add_argument("--source-root", type=Path, required=True)
    validate.add_argument("--profile-source-root", type=Path)
    validate.add_argument("--external-root", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        result = validate_packet(
            args.packet, args.manifest, source_root=args.source_root,
            profile_source_root=args.profile_source_root, external_source_roots=_external_roots(args.external_root),
        )
        print(json.dumps({"status": "OFFLINE_PACKET_VALID_NOT_REVIEW_APPROVAL", **asdict(result)}, sort_keys=True))
        return 0
    except (PacketError, OSError, json.JSONDecodeError, subprocess.SubprocessError) as err:
        print(json.dumps({"status": "FAIL_CLOSED", "reason": str(err)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
