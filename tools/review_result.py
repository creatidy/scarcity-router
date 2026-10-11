"""Development-only capture of one parent-prepared CLI; never dispatch or approve.

POSIX private storage is deliberate, not a product platform requirement. The
parent owns permissions, frozen objects, ordinal reservation and model binding.
No arguments, prompts, stderr, reasoning or tool payloads are logged.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import stat
import subprocess
import threading
import time
from typing import cast
from types import FrameType
import unicodedata
import uuid

type Json = dict[str, Json] | list[Json] | str | int | float | bool | None
LIMIT = 128 * 1024
INPUT_LIMIT = 900_000
LINE_LIMIT = 256 * 1024
FIELDS: frozenset[str] = frozenset(("reviewed_head", "reviewed_base", "verdict", "findings", "limitations", "checks_run"))
FINDING_FIELDS: frozenset[str] = frozenset(("severity", "file", "line_start", "line_end", "evidence", "consequence", "required_remediation"))
SHA = re.compile(r"[0-9a-f]{40}")
SECRET = re.compile(
    r"(?i)(?:\bsk-[a-z0-9_-]{8,}|\bbearer\s+\S+|"
    + r"eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+|"
    + r"-----BEGIN [^-]*PRIVATE KEY-----|"
    + r"(?:password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    + r"client[_-]?secret|authorization|cookie)\s*[=:]\s*\S+|"
    + r"https?://[^\s/]+@|https?://\S*[?&](?:token|key|secret|auth)=)"
)
FORBIDDEN_FIELD = re.compile(r"(?i)password|passwd|api[_-]?key|token|credential|secret|authorization|cookie|private[_-]?key")
OPAQUE = re.compile(r"[A-Za-z0-9_+/=-]{32,}")
BOUNDARY = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_+/=-.\\")
SESSION = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
HOST = r"[A-Za-z0-9_.-]{1,100}"


class Stage(StrEnum):
    FILE = "file"
    PROCESS = "process"
    TURN = "turn"
    EXTRACTION = "extraction"
    SCHEMA = "schema"
    REVISION = "revision"
    SAFETY = "content_safety"
    VERDICT = "verdict"
    RECOVERY = "recovery"


class Code(StrEnum):
    OK = "OK"
    MISSING = "MISSING_FILE"
    FILE = "UNSAFE_FILE"
    MODE = "UNSAFE_FILE_MODE"
    OWNER = "UNSAFE_FILE_OWNER"
    CHANGED = "FILE_CHANGED"
    OVERSIZE = "OVERSIZE"
    DUPLICATE = "DUPLICATE_JSON_KEY"
    JSON = "MALFORMED_JSON"
    UTF8 = "INVALID_UTF8"
    NUMBER = "NONFINITE_NUMBER"
    UNSAFE = "UNSAFE_CONTENT"
    FIELDS = "ROOT_FIELDS"
    TYPE = "FIELD_TYPE"
    FINDING = "FINDING_SCHEMA"
    CHECK = "CHECK_SCHEMA"
    VERDICT = "VERDICT_ENUM"
    APPROVE = "APPROVE_WITH_FINDINGS"
    REVISION = "REVISION_MISMATCH"
    EVENT = "INVALID_JSONL_EVENT"
    MESSAGE = "MISSING_LAST_MESSAGE"
    CONFLICT = "RESULT_SOURCES_CONFLICT"
    INCOMPLETE = "TURN_INCOMPLETE"
    FAILED = "TURN_FAILED"
    EXIT = "CLI_NONZERO_EXIT"
    NO_EXIT = "MISSING_DURABLE_EXIT"
    NO_SPAWN = "MISSING_SPAWN_EVIDENCE"
    CLAIM = "CLAIM_CONTEXT_MISMATCH"
    LAUNCH = "DUPLICATE_LAUNCH_REFUSED"
    IO = "SPAWN_OR_IO_FAILURE"
    INPUT = "INPUT_DELIVERY_INCOMPLETE"
    TIMEOUT = "CAPTURE_TIMEOUT"
    INTERRUPTED = "CAPTURE_INTERRUPTED"


class Rejected(Exception):
    def __init__(self, stage: Stage, code: Code) -> None:
        super().__init__(code.value)
        self.stage: Stage = stage
        self.code: Code = code


def encode(value: Json) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pairs(pairs: list[tuple[str, Json]]) -> dict[str, Json]:
    result: dict[str, Json] = {}
    for key, value in pairs:
        if key in result:
            raise Rejected(Stage.EXTRACTION, Code.DUPLICATE)
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise Rejected(Stage.EXTRACTION, Code.NUMBER)


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise Rejected(Stage.EXTRACTION, Code.NUMBER)
    return number


def parse(data: bytes, limit: int = LIMIT) -> Json:
    if len(data) > limit:
        raise Rejected(Stage.EXTRACTION, Code.OVERSIZE)
    try:
        return cast(Json, json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                                     parse_constant=_constant, parse_float=_float))
    except UnicodeError:
        raise Rejected(Stage.EXTRACTION, Code.UTF8) from None
    except (ValueError, RecursionError):
        raise Rejected(Stage.EXTRACTION, Code.JSON) from None


@dataclass(frozen=True)
class Context:
    head: str
    base: str
    ordinal: int
    requested_model: str
    job_id: str

    def __post_init__(self) -> None:
        if not SHA.fullmatch(self.head) or not SHA.fullmatch(self.base):
            raise ValueError("exact_revisions_required")
        if type(self.ordinal) is not int or self.ordinal < 1:
            raise ValueError("reserved_positive_ordinal_required")
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value) for value in (self.requested_model, self.job_id)):
            raise ValueError("bounded_identifiers_required")

    def json(self) -> dict[str, Json]:
        return {"head": self.head, "base": self.base, "ordinal": self.ordinal,
                "requested_model": self.requested_model, "job_id": self.job_id}


@dataclass(frozen=True)
class PublicSource:
    """Exact parent-attested public Git manifest, not a helper proof of provenance."""

    revision: str
    manifest_sha256: str
    tracked_paths: tuple[str, ...]
    pins: tuple[str, ...] = ()
    parent_verified: bool = False

    def __post_init__(self) -> None:
        if (self.parent_verified is not True or not SHA.fullmatch(self.revision)
                or not re.fullmatch(r"[0-9a-f]{64}", self.manifest_sha256)
                or len(self.tracked_paths) > 2048 or len(self.pins) > 128):
            raise ValueError("verified_bounded_public_manifest_required")
        for path in self.tracked_paths:
            if (not re.fullmatch(r"[A-Za-z0-9_.+/-]{1,512}", path) or path.startswith("/")
                    or any(part in ("", ".", "..") for part in path.split("/"))
                    or not ("/" in path or "." in path) or SECRET.search(path)):
                raise ValueError("exact_tracked_public_path_required")
        if any(not SHA.fullmatch(pin) for pin in self.pins):
            raise ValueError("exact_public_pin_required")


@dataclass(frozen=True)
class TextPolicy:
    forbidden: tuple[str, ...] = field(default=(), repr=False)
    sources: tuple[PublicSource, ...] = ()

    def __post_init__(self) -> None:
        if len(self.sources) > 16:
            raise ValueError("bounded_public_sources_required")

    def binding(self) -> str:
        # No credential values or hashes of credentials in durable metadata.
        for source in self.sources:
            for value in (source.revision, source.manifest_sha256, *source.tracked_paths, *source.pins):
                self.check_identity(value)
        return sha256(encode(cast(Json, [asdict(source) for source in self.sources])))

    def check_identity(self, value: str, pattern: str | None = None) -> None:
        # Shape-validated metadata is not reviewer prose; length is not a secret
        # exemption, and known literals/patterns always take precedence.
        if len(value) > 8192:
            raise Rejected(Stage.SAFETY, Code.OVERSIZE)
        if (SECRET.search(value) or any(secret and secret in value for secret in self.forbidden)
                or any(unicodedata.category(char).startswith("C") and char not in "\n\t" for char in value)):
            raise Rejected(Stage.SAFETY, Code.UNSAFE)
        if pattern is not None and not re.fullmatch(pattern, value):
            raise Rejected(Stage.SAFETY, Code.TYPE)

    def check(self, obj: Json, context: Context) -> None:
        literals = tuple(value for source in self.sources
                         for value in (*source.tracked_paths, source.revision, *source.pins))
        pending: list[tuple[Json, int, str | None]] = [(obj, 0, None)]
        count = 0
        while pending:
            value, depth, key = pending.pop()
            count += 1
            if depth > 12 or count > 4096:
                raise Rejected(Stage.SAFETY, Code.OVERSIZE)
            if isinstance(value, dict):
                for name, item in value.items():
                    if FORBIDDEN_FIELD.search(name):
                        raise Rejected(Stage.SAFETY, Code.UNSAFE)
                    pending.extend(((name, depth + 1, None), (item, depth + 1, name)))
            elif isinstance(value, list):
                pending.extend((item, depth + 1, None) for item in value)
            elif isinstance(value, str):
                self.check_identity(value)
                if key in ("reviewed_head", "reviewed_base") and SHA.fullmatch(value):
                    if value not in (context.head, context.base):
                        # An unrecognized opaque value in a revision field is
                        # diagnosed, never retained as an arbitrary hex exception.
                        raise Rejected(Stage.REVISION, Code.REVISION)
                    continue
                for match in OPAQUE.finditer(value):
                    if match.group() in (context.head, context.base):
                        continue
                    allowed = False
                    for literal in literals:
                        start = value.find(literal)
                        while start >= 0:
                            end = start + len(literal)
                            if (start <= match.start() and match.end() <= end
                                    and (start == 0 or value[start - 1] not in BOUNDARY)
                                    and (end == len(value) or value[end] not in BOUNDARY
                                         or (value[end] == "." and (end + 1 == len(value) or value[end + 1].isspace())))):
                                allowed = True
                            start = value.find(literal, start + 1)
                    if not allowed:
                        raise Rejected(Stage.SAFETY, Code.UNSAFE)


@dataclass
class Diagnostic:
    stage: Stage = Stage.EXTRACTION
    code: Code = Code.OK
    size: int | None = None
    sha256: str | None = None
    root_type: str = ""
    field_types: dict[str, str] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    unknown_field_count: int = 0
    observed_mode: str | None = None
    schema_valid: bool = False
    revisions_valid: bool = False
    actual_verdict: str | None = None
    lifecycle: str = "retrieved"
    source: str = "native_last_message"
    execution_failures: list[str] = field(default_factory=list)
    complete: bool = False
    parent_approval: bool = False
    model_identity: str = "UNVERIFIED"

    def json(self) -> Json:
        return cast(Json, asdict(self))


def validate(obj: Json, context: Context) -> str:
    if not isinstance(obj, dict) or set(obj) != set(FIELDS):
        raise Rejected(Stage.SCHEMA, Code.FIELDS)
    if (any(not isinstance(obj[key], str) for key in ("reviewed_head", "reviewed_base", "verdict"))
            or any(not isinstance(obj[key], list) for key in ("findings", "limitations", "checks_run"))):
        raise Rejected(Stage.SCHEMA, Code.TYPE)
    findings = cast(list[Json], obj["findings"])
    if not all(isinstance(value, str) for value in cast(list[Json], obj["limitations"])):
        raise Rejected(Stage.SCHEMA, Code.TYPE)
    verdict = cast(str, obj["verdict"])
    if verdict not in ("APPROVE", "REQUEST_CHANGES", "COMMENT"):
        raise Rejected(Stage.SCHEMA, Code.VERDICT)
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != set(FINDING_FIELDS):
            raise Rejected(Stage.SCHEMA, Code.FINDING)
        start, end = finding["line_start"], finding["line_end"]
        if (finding["severity"] not in ("P0", "P1", "P2", "P3")
                or type(start) is not int or type(end) is not int or start < 1 or end < start
                or any(not isinstance(finding[key], str) or not cast(str, finding[key]).strip()
                       for key in ("file", "evidence", "consequence", "required_remediation"))):
            raise Rejected(Stage.SCHEMA, Code.FINDING)
    for check in cast(list[Json], obj["checks_run"]):
        if (not isinstance(check, dict) or set(check) != {"command", "result"}
                or any(not isinstance(value, str) or not value.strip() for value in check.values())):
            raise Rejected(Stage.SCHEMA, Code.CHECK)
    if verdict == "APPROVE" and findings:
        raise Rejected(Stage.SCHEMA, Code.APPROVE)
    if obj["reviewed_head"] != context.head or obj["reviewed_base"] != context.base:
        raise Rejected(Stage.REVISION, Code.REVISION)
    return verdict


def inspect(data: bytes, context: Context, policy: TextPolicy) -> tuple[Diagnostic, Json]:
    diagnostic = Diagnostic(size=len(data), sha256=sha256(data))
    obj: Json = None
    safe = False
    try:
        obj = parse(data)
        diagnostic.root_type = type(obj).__name__
        if isinstance(obj, dict):
            diagnostic.field_types = {key: type(obj[key]).__name__ for key in sorted(FIELDS & obj.keys())}
            diagnostic.missing_fields = sorted(FIELDS - obj.keys())
            diagnostic.unknown_field_count = len(obj.keys() - FIELDS)
        policy.check(obj, context)
        safe = True
        diagnostic.actual_verdict = validate(obj, context)
        diagnostic.schema_valid = diagnostic.revisions_valid = True
        diagnostic.stage = Stage.VERDICT
        diagnostic.lifecycle = "validated"
    except Rejected as error:
        diagnostic.stage, diagnostic.code = error.stage, error.code
        diagnostic.lifecycle = "rejected"
        if error.stage == Stage.REVISION:
            try:
                _ = validate(obj, context)
            except Rejected as schema_error:
                diagnostic.schema_valid = schema_error.stage == Stage.REVISION
    # Only bounded known structures receive screened rejected-payload retention.
    bounded = isinstance(obj, dict) and not (obj.keys() - FIELDS)
    if isinstance(obj, dict) and bounded:
        for key, fields in (("findings", FINDING_FIELDS), ("checks_run", {"command", "result"})):
            items = obj.get(key, [])
            bounded = bounded and isinstance(items, list) and all(
                isinstance(item, dict) and not (item.keys() - fields) for item in items)
    if safe and bounded and len(encode(obj)) > LIMIT - 8192:
        diagnostic.stage, diagnostic.code = Stage.SAFETY, Code.OVERSIZE
        diagnostic.actual_verdict, diagnostic.lifecycle = None, "rejected"
        safe = False
    return diagnostic, obj if safe and bounded else None


class PrivateStore:
    """Pinned dirfd below the parent-designated private directory; no chmod repair."""

    def __init__(self, path: Path, expected_parent: Path) -> None:
        if ".." in path.parts or ".." in expected_parent.parts:
            raise Rejected(Stage.FILE, Code.FILE)
        path, expected_parent = path.absolute(), expected_parent.absolute()
        if path.parent != expected_parent or os.name != "posix":
            raise Rejected(Stage.FILE, Code.FILE)
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for index, component in enumerate(path.parts[1:], start=1):
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
                info = os.fstat(fd)
                if info.st_uid not in (0, os.getuid()):
                    raise Rejected(Stage.FILE, Code.OWNER)
                if info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
                    raise Rejected(Stage.FILE, Code.MODE)
                if index == len(path.parts) - 2:
                    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                        raise Rejected(Stage.FILE, Code.MODE)
            if os.fstat(fd).st_uid != os.getuid() or stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
                raise Rejected(Stage.FILE, Code.MODE)
            self.fd: int = fd
            self.path: Path = path
            fd = -1
        except OSError:
            raise Rejected(Stage.FILE, Code.FILE) from None
        finally:
            if fd >= 0:
                os.close(fd)

    def close(self) -> None:
        os.close(self.fd)

    @staticmethod
    def name(name: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name) or name in (".", ".."):
            raise Rejected(Stage.FILE, Code.FILE)
        return name

    def info(self, name: str) -> os.stat_result:
        try:
            return os.stat(self.name(name), dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            raise Rejected(Stage.FILE, Code.MISSING) from None

    @staticmethod
    def identity(info: os.stat_result) -> tuple[int, ...]:
        return (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink,
                info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def read(self, name: str) -> tuple[bytes, os.stat_result]:
        info = self.info(name)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Rejected(Stage.FILE, Code.FILE)
        if info.st_uid != os.getuid():
            raise Rejected(Stage.FILE, Code.OWNER)
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise Rejected(Stage.FILE, Code.MODE)
        try:
            fd = os.open(self.name(name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        except OSError:
            raise Rejected(Stage.FILE, Code.CHANGED) from None
        try:
            if self.identity(os.fstat(fd)) != self.identity(info):
                raise Rejected(Stage.FILE, Code.CHANGED)
            data = bytearray()
            while len(data) <= LIMIT:
                chunk = os.read(fd, min(16384, LIMIT + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > LIMIT:
                raise Rejected(Stage.FILE, Code.OVERSIZE)
            if (self.identity(os.fstat(fd)) != self.identity(info)
                    or self.identity(self.info(name)) != self.identity(info)):
                raise Rejected(Stage.FILE, Code.CHANGED)
            return bytes(data), info
        finally:
            os.close(fd)

    def atomic(self, name: str, value: Json, *, exclusive: bool = False) -> None:
        name = self.name(name)
        data = encode(value)
        if len(data) > LIMIT:
            raise Rejected(Stage.FILE, Code.OVERSIZE)
        tmp = ".atomic-" + uuid.uuid4().hex
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, "wb") as output:
                _ = output.write(data)
                output.flush()
                os.fsync(output.fileno())
            if exclusive:
                try:
                    os.link(tmp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd, follow_symlinks=False)
                except FileExistsError:
                    raise Rejected(Stage.PROCESS, Code.LAUNCH) from None
            else:
                try:
                    _ = self.read(name)
                except Rejected as error:
                    if error.code != Code.MISSING:
                        raise
                os.replace(tmp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(tmp, dir_fd=self.fd)
            except FileNotFoundError:
                pass
            os.fsync(self.fd)

    def remove(self, name: str, info: os.stat_result) -> None:
        if self.identity(self.info(name)) != self.identity(info):
            raise Rejected(Stage.FILE, Code.CHANGED)
        os.unlink(self.name(name), dir_fd=self.fd)
        os.fsync(self.fd)


@dataclass
class EventCapture:
    context: Context
    policy: TextPolicy
    active: bool = False
    completed: bool = False
    failed: bool = False
    protocol_failed: bool = False
    last: Json = None
    diagnostic: Diagnostic = field(default_factory=Diagnostic)
    sequence: int = 0
    session: str | None = None

    def feed(self, data: bytes) -> None:
        self.sequence += 1
        if self.protocol_failed:
            return
        try:
            if self.sequence > 4096:
                raise Rejected(Stage.EXTRACTION, Code.OVERSIZE)
            event = parse(data, LINE_LIMIT)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise Rejected(Stage.EXTRACTION, Code.EVENT)
            kind = event["type"]
            if kind == "thread.started":
                session = event.get("thread_id")
                if isinstance(session, str):
                    self.policy.check_identity(session, SESSION)
                    self.session = session
            elif kind == "turn.started":
                if self.active or self.failed:
                    raise Rejected(Stage.TURN, Code.EVENT)
                self.active, self.completed, self.last = True, False, None
                self.diagnostic = Diagnostic(stage=Stage.TURN, code=Code.MESSAGE)
            elif kind == "turn.completed":
                if not self.active or self.failed:
                    raise Rejected(Stage.TURN, Code.EVENT)
                self.active, self.completed = False, True
            elif kind in ("turn.failed", "error"):
                self.failed, self.completed, self.last = True, False, None
            elif kind == "item.completed":
                item = event.get("item")
                if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                    raise Rejected(Stage.EXTRACTION, Code.EVENT)
                if item["type"] == "agent_message":
                    text = item.get("text")
                    if not self.active or not isinstance(text, str):
                        raise Rejected(Stage.EXTRACTION, Code.EVENT)
                    self.diagnostic, self.last = inspect(text.encode(), self.context, self.policy)
            elif kind not in ("item.started", "item.updated"):
                raise Rejected(Stage.EXTRACTION, Code.EVENT)
        except (Rejected, UnicodeError) as error:
            unsafe = isinstance(error, Rejected) and error.stage == Stage.SAFETY
            self.diagnostic = Diagnostic(stage=Stage.SAFETY if unsafe else Stage.EXTRACTION,
                                         code=error.code if isinstance(error, Rejected) else Code.UTF8,
                                         size=len(data), sha256=None if unsafe else sha256(data), lifecycle="rejected")
            self.session = None
            self.failed, self.completed, self.last, self.protocol_failed = True, False, None, True

    def persist(self, store: PrivateStore) -> None:
        store.atomic("turn.json", {"completed": self.completed and not self.active and not self.failed,
                                  "failed": self.failed, "sequence": self.sequence,
                                  "session": self.session, "last": self.last,
                                  "diagnostic": self.diagnostic.json(), "NOT_APPROVAL": True})


def _object(store: PrivateStore, name: str) -> dict[str, Json] | None:
    try:
        value = parse(store.read(name)[0])
    except Rejected as error:
        if error.code == Code.MISSING:
            return None
        raise
    except OSError:
        raise Rejected(Stage.FILE, Code.IO) from None
    if not isinstance(value, dict):
        raise Rejected(Stage.RECOVERY, Code.TYPE)
    return value


def _finish_cleanup(store: PrivateStore, native: PrivateStore, envelope: dict[str, Json],
                    diagnostic: Diagnostic) -> Diagnostic:
    """Finish only the durably bound original; missing may mean unlink already ran."""
    cleanup = envelope.get("cleanup")
    if not isinstance(cleanup, dict) or cleanup.get("state") != "pending":
        raise Rejected(Stage.RECOVERY, Code.TYPE)
    identity = cleanup.get("identity")
    digest = cleanup.get("sha256")
    if (not isinstance(identity, list) or len(identity) != 8
            or any(type(value) is not int for value in identity)
            or (digest is not None and (not isinstance(digest, str)
                                       or not re.fullmatch(r"[0-9a-f]{64}", digest)))):
        raise Rejected(Stage.RECOVERY, Code.TYPE)
    try:
        try:
            info = native.info("last-message.json")
        except Rejected as error:
            if error.code != Code.MISSING:
                raise
        else:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise Rejected(Stage.FILE, Code.FILE)
            if info.st_uid != os.getuid():
                raise Rejected(Stage.FILE, Code.OWNER)
            if list(native.identity(info)) != identity:
                raise Rejected(Stage.FILE, Code.CHANGED)
            # Metadata includes ctime/mtime/size/mode as well as dev/inode. No
            # refused or rejected raw bytes are read to complete disposal.
            native.remove("last-message.json", info)
        os.fsync(native.fd)
        cleanup["state"] = "done"
    except (Rejected, OSError) as error:
        stage, code = (error.stage, error.code) if isinstance(error, Rejected) else (Stage.FILE, Code.IO)
        cleanup["state"] = "blocked"
        envelope["cleanup_failure"] = {"stage": stage, "code": code}
        if envelope.get("source_rejection") is None:
            diagnostic.stage, diagnostic.code = stage, code
        diagnostic.lifecycle, diagnostic.complete = "rejected", False
        diagnostic.actual_verdict, diagnostic.revisions_valid = None, False
        _ = envelope.setdefault("source_rejection", diagnostic.json())
    envelope["diagnostic"] = diagnostic.json()
    store.atomic("receipt.json", envelope)
    return diagnostic


def _discard_only(original: Json, redacted: Json) -> bool:
    """A journal may discard strings, never supply identity or change JSON types."""
    if isinstance(original, dict):
        return (isinstance(redacted, dict) and original.keys() == redacted.keys()
                and all(_discard_only(value, redacted[key]) for key, value in original.items()))
    return (isinstance(original, str) and redacted is None
            or type(original) is type(redacted) and encode(original) == encode(redacted))


def _finalize_rescreen(store: PrivateStore, native: PrivateStore, envelope: dict[str, Json],
                       diagnostic: Diagnostic, target: dict[str, Json], exit_code: Json,
                       failure: Rejected | None = None) -> Diagnostic:
    """Tombstone identity only after disposal; the original claim authorizes retries."""
    if envelope.get("rescreen_pending") is not True:
        return diagnostic
    cleanup = envelope.get("cleanup")
    if (type(exit_code) is not int
            or isinstance(cleanup, dict) and cleanup.get("state") in ("pending", "blocked")):
        if failure is not None:
            raise failure
        return diagnostic
    try:
        _ = native.info("last-message.json")
    except Rejected as error:
        if error.code != Code.MISSING:
            raise
    else:
        # A late/changed artifact cannot be adopted as the original disposal target.
        if failure is not None:
            raise failure
        return diagnostic
    envelope["binding"] = target
    del envelope["rescreen_pending"]
    del envelope["rescreen_metadata"]
    store.atomic("receipt.json", envelope)
    # A crash here is recoverable from the discard-only receipt binding while
    # the still-original claim independently verifies caller identity.
    store.atomic("launch.claim", target)
    if failure is not None:
        raise failure
    return diagnostic


def recover(store: PrivateStore, native: PrivateStore, context: Context, policy: TextPolicy) -> Diagnostic:
    """Retrieve the existing job, including after handle loss. Never launch a child."""
    claim = _object(store, "launch.claim")
    # Compare canonical JSON before policy rescreening can alter retained evidence.
    # The public manifest fingerprint is computed without safety-induced scrubbing;
    # a valid same-context caller may still tighten forbidden literals below.
    expected_binding: dict[str, Json] = {
        "context": context.json(),
        "public_policy_sha256": sha256(encode(cast(Json, [asdict(source) for source in policy.sources]))),
    }
    if claim is None or encode(claim) != encode(expected_binding):
        raise Rejected(Stage.RECOVERY, Code.CLAIM)
    original_binding = parse(encode(claim))
    # The typed receipt is parent-owned, never exposed to reviewer tools.
    prior = _object(store, "receipt.json")
    if prior is not None:
        receipt_binding = prior.get("binding")
        if (prior.get("NOT_APPROVAL") is not True or type(prior.get("terminal")) is not bool
                or not isinstance(receipt_binding, dict)
                or set(receipt_binding) != {"context", "public_policy_sha256"}
                or not isinstance(receipt_binding.get("context"), dict)):
            raise Rejected(Stage.RECOVERY, Code.TYPE)
        transitioning = encode(receipt_binding) != encode(claim)
        if transitioning and not _discard_only(claim, receipt_binding):
            raise Rejected(Stage.RECOVERY, Code.CLAIM)
        if "rescreen_pending" in prior and prior["rescreen_pending"] is not True:
            raise Rejected(Stage.RECOVERY, Code.TYPE)
        if prior.get("rescreen_pending") is True and not isinstance(prior.get("source_rejection"), dict):
            raise Rejected(Stage.RECOVERY, Code.TYPE)
        queued = prior.get("rescreen_metadata", {})
        if (not isinstance(queued, dict) or set(queued) - {"launch.claim", "spawn.json", "exit.json", "capture.json", "turn.json"}
                or any(not isinstance(value, dict) for value in queued.values())
                or queued and prior.get("rescreen_pending") is not True):
            raise Rejected(Stage.RECOVERY, Code.TYPE)
        queued_turn = queued.get("turn.json")
        if isinstance(queued_turn, dict) and queued_turn.get("last") is not None:
            raise Rejected(Stage.RECOVERY, Code.TYPE)
        queued_claim = queued.get("launch.claim")
        if queued_claim is not None and not _discard_only(claim, queued_claim):
            raise Rejected(Stage.RECOVERY, Code.CLAIM)
        # Missing/invalid diagnostic fields must not reconstruct as defaults and
        # erase the source history after terminal raw cleanup.
        for key in ("diagnostic", "source_diagnostic", "source_rejection"):
            if key != "diagnostic" and key not in prior:
                continue
            value = prior.get(key)
            if not isinstance(value, dict):
                raise Rejected(Stage.RECOVERY, Code.TYPE)
            diagnostic = _diagnostic(value)
            if (encode(diagnostic.json()) != encode(value)
                    or diagnostic.complete and (diagnostic.code != Code.OK
                        or not diagnostic.schema_valid or not diagnostic.revisions_valid
                        or diagnostic.actual_verdict is None or diagnostic.lifecycle != "validated"
                        or diagnostic.execution_failures)
                    or key == "source_rejection" and diagnostic.code == Code.OK):
                raise Rejected(Stage.RECOVERY, Code.TYPE)
        identity = prior.get("native_identity")
        if "native_identity" in prior and (not isinstance(identity, list) or len(identity) != 2
                                          or any(type(value) is not int or value < 0 for value in identity)):
            raise Rejected(Stage.RECOVERY, Code.TYPE)
        if transitioning:
            rejection = prior.get("source_rejection")
            prior_diagnostic = prior.get("diagnostic")
            if (not isinstance(rejection, dict) or rejection.get("stage") != Stage.SAFETY
                    or prior.get("result") is not None or not isinstance(prior_diagnostic, dict)
                    or prior_diagnostic.get("complete") is not False):
                raise Rejected(Stage.RECOVERY, Code.CLAIM)
            # This is a validated transformation of the verified original claim,
            # not authority supplied by the caller or arbitrary queued metadata.
            queued["launch.claim"] = receipt_binding
            prior["rescreen_metadata"] = queued
            prior["rescreen_pending"] = True
    else:
        prior = {}
    turn = _object(store, "turn.json") or {}
    spawned, exited = _object(store, "spawn.json") or {}, _object(store, "exit.json") or {}
    capture_state = _object(store, "capture.json") or {}
    # Replay already sanitized metadata, never the caller's binding, after an
    # interrupted scrub. Relaxing policy cannot restore its discarded strings.
    queued = cast(dict[str, Json], prior.get("rescreen_metadata", {}))
    claim = cast(dict[str, Json], queued.get("launch.claim", claim))
    turn = cast(dict[str, Json], queued.get("turn.json", turn))
    spawned = cast(dict[str, Json], queued.get("spawn.json", spawned))
    exited = cast(dict[str, Json], queued.get("exit.json", exited))
    capture_state = cast(dict[str, Json], queued.get("capture.json", capture_state))
    prior_value = prior.get("diagnostic")
    cached = _diagnostic(prior_value) if isinstance(prior_value, dict) else Diagnostic()
    rescreened: Rejected | None = None
    unsafe_public_binding = False
    try:
        binding: dict[str, Json] = {"context": context.json(), "public_policy_sha256": policy.binding()}
    except Rejected as rejection:
        binding = {"context": context.json(), "public_policy_sha256": None}
        rescreened = rejection
        unsafe_public_binding = True
    # Prepare every scrub before writing. Typed internal state is not reviewer
    # text; the same literals in result/last still receive the full text policy.
    changes = {name: cast(dict[str, Json], value) for name, value in queued.items()}
    for name, envelope in (("launch.claim", claim), ("spawn.json", spawned), ("exit.json", exited),
                           ("capture.json", capture_state), ("turn.json", turn), ("receipt.json", prior),
                           (None, binding)):
        metadata_stack: list[Json] = [envelope]
        changed = False
        while metadata_stack:
            value = metadata_stack.pop()
            if isinstance(value, dict):
                for key, item in list(value.items()):
                    if key in ("result", "last"):
                        continue
                    if key in ("diagnostic", "source_diagnostic", "source_rejection"):
                        if not isinstance(item, dict):
                            raise Rejected(Stage.RECOVERY, Code.TYPE)
                        typed = _diagnostic(item)
                        if encode(typed.json()) != encode(item):
                            raise Rejected(Stage.RECOVERY, Code.TYPE)
                        # Only the digest is externally derived string data.
                        # Counts, field types, enums and flags are finite internal
                        # observations reconstructed from the validated original.
                        if typed.sha256 is not None:
                            try:
                                policy.check_identity(typed.sha256)
                            except Rejected as rejection:
                                typed.sha256 = None
                                rescreened, changed = rejection, True
                        value[key] = typed.json()
                        continue
                    if ((value is capture_state or value is prior.get("cleanup_failure"))
                            and (key == "stage" and item in tuple(Stage) or key == "code" and item in tuple(Code))
                            or value is exited and key == "capture_code" and item in tuple(Code)
                            or value is spawned and key == "model_identity" and item == "UNVERIFIED"
                            or value is spawned and key == "version_source" and item == "parent_observed"
                            or value is spawned and key == "lifecycle" and item == "started"
                            or value is exited and key == "lifecycle" and item == "exited"
                            or value is prior.get("cleanup") and key == "state" and item in ("pending", "done", "blocked")):
                        continue
                    if key == "public_policy_sha256" and unsafe_public_binding and item is not None:
                        value[key], changed = None, True
                        continue
                    if isinstance(item, str):
                        try:
                            policy.check_identity(item, SESSION if key == "session" else HOST if key == "host" else None)
                        except Rejected as rejection:
                            value[key] = None
                            rescreened, changed = rejection, True
                    elif isinstance(item, (dict, list)):
                        metadata_stack.append(item)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    if isinstance(item, str):
                        try:
                            policy.check_identity(item)
                        except Rejected as rejection:
                            value[index] = None
                            rescreened, changed = rejection, True
                    elif isinstance(item, (dict, list)):
                        metadata_stack.append(item)
        if changed and name is not None:
            changes[name] = envelope
    prior_value = prior.get("diagnostic")
    cached = _diagnostic(prior_value) if isinstance(prior_value, dict) else cached
    # A previous safety rejection is durable even if policy is later relaxed.
    # Finish payload disposal after a crash between receipt and turn writes.
    sticky = prior.get("source_rejection")
    sticky_safety = (prior.get("rescreen_pending") is True and isinstance(sticky, dict)
                     and sticky.get("stage") == Stage.SAFETY)
    if sticky_safety:
        assert isinstance(sticky, dict)
        original_rejection = _diagnostic(sticky)
        rescreened = Rejected(original_rejection.stage, original_rejection.code)
    for envelope, key in ((prior, "result"), (turn, "last")):
        payload = envelope.get(key)
        if payload is None:
            continue
        try:
            policy.check(payload, context)
        except Rejected as rejection:
            envelope[key] = None
            rescreened = rejection
        else:
            if sticky_safety:
                envelope[key] = None
    if rescreened is not None:
        # A safety failure invalidates the capture, not just one source. Dispose
        # retained reviewer text together; journal only the typed observations.
        if "result" in prior:
            prior["result"] = None
        if "last" in turn:
            turn["last"] = None
        cached.stage, cached.code = rescreened.stage, rescreened.code
        cached.lifecycle, cached.complete, cached.actual_verdict = "rejected", False, None
        cached.revisions_valid = False
        turn["diagnostic"] = cached.json()
        prior["diagnostic"] = cached.json()
        prior["source_rejection"] = cached.json()
        if "source_diagnostic" in prior:
            prior["source_diagnostic"] = cached.json()
        # The receipt is the existing durable rejection/cleanup journal. Keep
        # its original binding until payload removal, never a caller replacement.
        _ = prior.setdefault("binding", claim)
        _ = prior.setdefault("terminal", False)
        prior["NOT_APPROVAL"] = True
        prior["rescreen_pending"] = True
        changes["turn.json"] = turn
        prior["rescreen_metadata"] = {name: value for name, value in changes.items()
                                      if name in ("launch.claim", "spawn.json", "exit.json", "capture.json", "turn.json")}
        changes["receipt.json"] = prior
    # Persist rejection first, payload scrub second, identity scrub last. Every
    # prefix is typed and recoverable; no policy collision can poison an enum.
    for name in ("receipt.json", "turn.json", "spawn.json", "exit.json", "capture.json", "launch.claim"):
        if name in changes:
            if name == "receipt.json":
                # Binding scrubs must not prevent recovery of an interrupted
                # payload scrub. Preserve the attested original until disposal.
                pending_receipt = dict(changes[name])
                pending_receipt["binding"] = original_binding
                store.atomic(name, pending_receipt)
            elif name != "launch.claim":
                store.atomic(name, changes[name])
    if rescreened is not None:
        prior["binding"] = original_binding
    exit_code = exited.get("exit_code")
    try:
        observed = native.info("last-message.json")
    except Rejected as error:
        if error.code != Code.MISSING:
            raise
        observed = None
    identity = prior.get("native_identity")
    rejection = prior.get("source_rejection")
    turn_diagnostic = turn.get("diagnostic")
    if (rejection is None and turn.get("failed") is True and isinstance(turn_diagnostic, dict)
            and turn_diagnostic.get("stage") == Stage.SAFETY):
        rejection = _diagnostic(turn_diagnostic).json()
    if (rejection is None and prior.get("binding") == binding
            and cached.source == "native_last_message"
            and cached.code not in (Code.OK, Code.NO_EXIT, Code.MESSAGE, Code.MISSING)):
        rejection = cached.json()
    # Terminal capture is immutable except for rejection under today's policy or
    # a late artifact. Never extract a replacement as a new result for this job.
    if (prior.get("binding") == binding or rescreened is not None) and prior.get("terminal") is True:
        cleanup = prior.get("cleanup")
        if isinstance(cleanup, dict) and (cleanup.get("state") == "pending"
                or rescreened is not None and cleanup.get("state") == "blocked"):
            if type(exit_code) is not int:
                raise Rejected(Stage.PROCESS, Code.NO_EXIT)
            original = prior.get("source_diagnostic")
            if rejection is None and isinstance(original, dict):
                cached = _diagnostic(original)
            cleanup["state"] = "pending"
            recovered = _finish_cleanup(store, native, prior, cached)
            return _finalize_rescreen(store, native, prior, recovered, claim, exit_code,
                                      rescreened if unsafe_public_binding else None)
        if observed is not None and not (isinstance(cleanup, dict) and cleanup.get("state") == "blocked"):
            prior["cleanup_failure"] = {"stage": Stage.FILE, "code": Code.CHANGED}
        if observed is not None and cached.code == Code.OK:
            _ = prior.setdefault("source_diagnostic", cached.json())
            cached.stage, cached.code = Stage.EXTRACTION, Code.CONFLICT
            cached.lifecycle, cached.complete, cached.actual_verdict = "rejected", False, None
            cached.revisions_valid = False
            prior["diagnostic"] = cached.json()
            _ = prior.setdefault("source_rejection", cached.json())
        if observed is not None:
            store.atomic("receipt.json", prior)
        return _finalize_rescreen(store, native, prior, cached, claim, exit_code,
                                  rescreened if unsafe_public_binding else None)
    diagnostic = Diagnostic()
    obj: Json = None
    raw_info: os.stat_result | None = None
    cleanup_failure: dict[str, Json] | None = None
    try:
        if observed is not None and identity is None:
            identity = cast(list[Json], [observed.st_dev, observed.st_ino])
        if isinstance(rejection, dict):
            diagnostic = _diagnostic(rejection)
            raise Rejected(diagnostic.stage, diagnostic.code)
        if identity is not None and (observed is None or identity != [observed.st_dev, observed.st_ino]):
            raise Rejected(Stage.FILE, Code.CHANGED)
        if observed is not None:
            identity = cast(list[Json], [observed.st_dev, observed.st_ino])
        # A live writer may have only partially written its file. Bind the inode,
        # but neither extract nor delete its bytes until exit is durable.
        if type(exit_code) is not int:
            raise Rejected(Stage.PROCESS, Code.NO_EXIT)
        data, raw_info = native.read("last-message.json")
        if identity != [raw_info.st_dev, raw_info.st_ino]:
            raise Rejected(Stage.FILE, Code.CHANGED)
        diagnostic, obj = inspect(data, context, policy)
        diagnostic.observed_mode = "0600"
    except Rejected as error:
        diagnostic.stage, diagnostic.code, diagnostic.lifecycle = error.stage, error.code, "rejected"
        if error.code == Code.MISSING and identity is None:
            diagnostic.source = "jsonl_last_completed_agent_message"
            last = turn.get("last")
            if last is not None and turn.get("completed") is True and turn.get("failed") is False:
                diagnostic, obj = inspect(encode(last), context, policy)
                diagnostic.source = "jsonl_last_completed_agent_message"
            else:
                diagnostic.code = Code.MESSAGE
                metadata = turn.get("diagnostic")
                if isinstance(metadata, dict):
                    diagnostic = _diagnostic(metadata)
                    diagnostic.source = "jsonl_last_completed_agent_message"
                    if diagnostic.code == Code.OK:
                        diagnostic.code = Code.MESSAGE
        elif observed is not None and type(exit_code) is int:
            info = observed
            diagnostic.observed_mode = format(stat.S_IMODE(info.st_mode), "04o")
            diagnostic.size = info.st_size
            # Do not read a refused-mode file. Only the exact owned regular raw
            # inode may be removed, after its safe diagnostic is durable.
            if identity != [info.st_dev, info.st_ino]:
                cleanup_failure = {"stage": Stage.FILE, "code": Code.CHANGED}
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                cleanup_failure = {"stage": Stage.FILE, "code": Code.FILE}
            elif info.st_uid != os.getuid():
                cleanup_failure = {"stage": Stage.FILE, "code": Code.OWNER}
            else:
                raw_info = info
        elif isinstance(rejection, dict) and identity is not None and type(exit_code) is int:
            cleanup_failure = {"stage": Stage.FILE, "code": Code.CHANGED}
    if rescreened is not None:
        diagnostic.stage, diagnostic.code = rescreened.stage, rescreened.code
        diagnostic.actual_verdict, diagnostic.revisions_valid, obj = None, False, None
    elif obj is not None and turn.get("last") is not None and encode(obj) != encode(turn["last"]):
        diagnostic.stage, diagnostic.code = Stage.EXTRACTION, Code.CONFLICT
        diagnostic.actual_verdict, diagnostic.revisions_valid = None, False
    elif (obj is not None and diagnostic.code == Code.OK
          and diagnostic.source == "native_last_message" and turn.get("completed") is True):
        metadata = turn.get("diagnostic")
        if isinstance(metadata, dict) and metadata.get("code") != Code.OK:
            diagnostic.stage, diagnostic.code = Stage.EXTRACTION, Code.CONFLICT
            diagnostic.actual_verdict, diagnostic.revisions_valid = None, False
    failures: list[str] = []
    if type(spawned.get("pid")) is not int or cast(int, spawned.get("pid", 0)) <= 0:
        failures.append(Code.NO_SPAWN)
    if type(exit_code) is not int:
        failures.append(Code.NO_EXIT)
    elif exit_code != 0:
        failures.append(Code.EXIT)
    if turn.get("completed") is not True or turn.get("failed") is not False:
        failures.append(Code.FAILED if turn.get("failed") is True else Code.INCOMPLETE)
    interruption = exited.get("capture_code")
    if interruption is None:
        interruption = capture_state.get("code")
    if interruption in (Code.TIMEOUT, Code.INTERRUPTED, Code.IO, Code.INPUT):
        failures.append(cast(str, interruption))
    diagnostic.execution_failures = failures
    diagnostic.complete = not failures and diagnostic.schema_valid and diagnostic.revisions_valid and diagnostic.code == Code.OK
    if not diagnostic.complete:
        diagnostic.lifecycle = "rejected"
    envelope: dict[str, Json] = {"binding": binding, "diagnostic": diagnostic.json(), "NOT_APPROVAL": True,
                                 "terminal": type(exit_code) is int}
    if rescreened is not None:
        envelope["binding"] = original_binding
        envelope["rescreen_pending"] = True
        envelope["rescreen_metadata"] = prior["rescreen_metadata"]
    if identity is not None:
        envelope["native_identity"] = identity
    if isinstance(rejection, dict):
        envelope["source_rejection"] = _diagnostic(rejection).json()
    elif diagnostic.code not in (Code.OK, Code.NO_EXIT, Code.MESSAGE):
        envelope["source_rejection"] = diagnostic.json()
    if obj is not None:
        envelope["result"] = obj
    if cleanup_failure is not None:
        envelope["cleanup_failure"] = cleanup_failure
    if raw_info is not None and type(exit_code) is int:
        envelope["source_diagnostic"] = diagnostic.json()
        envelope["cleanup"] = {"state": "pending", "identity": cast(Json, list(native.identity(raw_info))),
                               "sha256": diagnostic.sha256}
        pending = cast(dict[str, Json], diagnostic.json())
        pending["complete"] = False
        envelope["diagnostic"] = pending
        store.atomic("receipt.json", envelope)
        recovered = _finish_cleanup(store, native, envelope, diagnostic)
        return _finalize_rescreen(store, native, envelope, recovered, claim, exit_code,
                                  rescreened if unsafe_public_binding else None)
    store.atomic("receipt.json", envelope)
    return _finalize_rescreen(store, native, envelope, diagnostic, claim, exit_code,
                              rescreened if unsafe_public_binding else None)


def _diagnostic(obj: dict[str, Json]) -> Diagnostic:
    """Rebuild only allowlisted typed metadata, never arbitrary child strings."""
    result = Diagnostic()
    try:
        result.stage = Stage(cast(str, obj.get("stage")))
        result.code = Code(cast(str, obj.get("code")))
    except ValueError:
        raise Rejected(Stage.RECOVERY, Code.TYPE) from None
    for name in ("size", "unknown_field_count"):
        value = obj.get(name)
        if type(value) is int and value >= 0:
            setattr(result, name, value)
    digest = obj.get("sha256")
    if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
        result.sha256 = digest
    root = obj.get("root_type")
    if root in ("dict", "list", "str", "int", "float", "bool", "NoneType"):
        result.root_type = cast(str, root)
    types = obj.get("field_types")
    if isinstance(types, dict):
        result.field_types = {key: value for key, value in types.items() if key in FIELDS
                              and isinstance(value, str) and value in ("dict", "list", "str", "int", "float", "bool", "NoneType")}
    missing = obj.get("missing_fields")
    if isinstance(missing, list):
        result.missing_fields = [value for value in missing if isinstance(value, str) and value in FIELDS]
    mode = obj.get("observed_mode")
    if isinstance(mode, str) and re.fullmatch(r"0[0-7]{3}", mode):
        result.observed_mode = mode
    for name in ("schema_valid", "revisions_valid", "complete"):
        setattr(result, name, obj.get(name) is True)
    verdict = obj.get("actual_verdict")
    if verdict in ("APPROVE", "REQUEST_CHANGES", "COMMENT"):
        result.actual_verdict = cast(str, verdict)
    lifecycle = obj.get("lifecycle")
    if lifecycle in ("retrieved", "validated", "rejected"):
        result.lifecycle = cast(str, lifecycle)
    source = obj.get("source")
    if source in ("native_last_message", "jsonl_last_completed_agent_message"):
        result.source = cast(str, source)
    failures = obj.get("execution_failures")
    if isinstance(failures, list):
        result.execution_failures = [value for value in failures if isinstance(value, str) and value in set(Code)]
    return result


def run_prepared(store: PrivateStore, native: PrivateStore, context: Context,
                 argv: list[str], prompt: bytes, env: dict[str, str], policy: TextPolicy,
                 *, executable_version: str, timeout: float = 600,
                 progress: Callable[[dict[str, Json]], None] | None = None) -> Diagnostic:
    """One explicit prepared command, no shell, credential lookup, selection or retry.

    Invoke with an existing tracked Kilo process or foreground POSIX wait. The
    parent must isolate the receipt store from tools and attest version separately.
    """
    if (not argv or not os.path.isabs(argv[0]) or not 0 < timeout <= 3600
            or len(prompt) > INPUT_LIMIT or not re.fullmatch(r"[A-Za-z0-9 ._+-]{1,80}", executable_version)
            or native.path == store.path or threading.current_thread() is not threading.main_thread()):
        raise ValueError("isolated_explicit_prepared_launch_required")
    store_info, native_info = os.fstat(store.fd), os.fstat(native.fd)
    if (store_info.st_dev, store_info.st_ino) == (native_info.st_dev, native_info.st_ino):
        raise ValueError("isolated_explicit_prepared_launch_required")
    policy.check(context.json(), context)
    policy.check(executable_version, context)
    public_binding = policy.binding()
    executable = Path(argv[0]).resolve()
    policy.check(str(executable), context)
    host = socket.gethostname()
    policy.check_identity(host, HOST)
    for name in ("launch.claim", "spawn.json", "exit.json", "turn.json", "receipt.json", "capture.json"):
        try:
            _ = store.info(name)
        except Rejected as error:
            if error.code != Code.MISSING:
                raise
        else:
            raise Rejected(Stage.PROCESS, Code.LAUNCH)
    try:
        _ = native.info("last-message.json")
    except Rejected as error:
        if error.code != Code.MISSING:
            raise
    else:
        raise Rejected(Stage.FILE, Code.CHANGED)
    store.atomic("launch.claim", {"context": context.json(), "public_policy_sha256": public_binding}, exclusive=True)
    capture = EventCapture(context, policy)
    child: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    capture_code = Code.OK
    stderr_size = 0
    stderr_digest = hashlib.sha256()
    deadline = time.monotonic() + timeout
    old_term = signal.getsignal(signal.SIGTERM)

    def interrupted(_signum: int, _frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    try:
        _ = signal.signal(signal.SIGTERM, interrupted)
        # Only the native output dirfd crosses exec, never the receipt store.
        child = subprocess.Popen(argv, cwd=f"/dev/fd/{native.fd}", pass_fds=(native.fd,), env=env, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 start_new_session=True, umask=0o077)
        receipt: dict[str, Json] = {"pid": child.pid, "host": host, "executable": str(executable),
                                    "version": executable_version, "version_source": "parent_observed",
                                    "context": context.json(), "model_identity": "UNVERIFIED", "lifecycle": "started",
                                    "started_ns": time.time_ns()}
        store.atomic("spawn.json", receipt)
        if progress is not None:
            progress({"lifecycle": "started", "pid": child.pid, "model_identity": "UNVERIFIED"})
        streams = (child.stdin, child.stdout, child.stderr)
        assert all(stream is not None for stream in streams)
        assert child.stdin is not None and child.stdout is not None and child.stderr is not None
        os.set_blocking(child.stdin.fileno(), False)
        _ = selector.register(child.stdin, selectors.EVENT_WRITE, "stdin")
        _ = selector.register(child.stdout, selectors.EVENT_READ, "stdout")
        _ = selector.register(child.stderr, selectors.EVENT_READ, "stderr")
        pending = memoryview(prompt)
        buffer = bytearray()
        dropping = False
        progress_count = 0
        progress_at = time.monotonic()
        while selector.get_map():
            if time.monotonic() >= deadline:
                capture_code = Code.TIMEOUT
                break
            for key, _mask in selector.select(timeout=min(0.2, max(0, deadline - time.monotonic()))):
                kind = cast(str, key.data)
                if kind == "stdin":
                    try:
                        amount = os.write(key.fd, pending[:4096]) if pending else 0
                        pending = pending[amount:]
                    except BrokenPipeError:
                        capture_code = Code.INPUT
                        pending = memoryview(b"")
                    except BlockingIOError:
                        continue
                    if not pending:
                        _ = selector.unregister(key.fd)
                        child.stdin.close()
                    continue
                chunk = os.read(key.fd, 16384)
                if not chunk:
                    if kind == "stdout" and buffer and not dropping:
                        capture.feed(bytes(buffer))
                        capture.persist(store)
                    _ = selector.unregister(key.fd)
                    continue
                if kind == "stderr":
                    stderr_size += len(chunk)
                    stderr_digest.update(chunk)
                    continue
                for fragment in chunk.splitlines(keepends=True):
                    if not dropping:
                        buffer.extend(fragment)
                    if len(buffer) > LINE_LIMIT:
                        capture.feed(b" " * (LINE_LIMIT + 1))
                        buffer.clear()
                        dropping = True
                    if fragment.endswith(b"\n"):
                        if not dropping:
                            capture.feed(bytes(buffer))
                        capture.persist(store)
                        if progress is not None and progress_count < 128 and time.monotonic() >= progress_at:
                            progress({"lifecycle": "running", "sequence": capture.sequence,
                                      "turn_completed": capture.completed, "model_identity": "UNVERIFIED"})
                            progress_count += 1
                            progress_at = time.monotonic() + 0.25
                        buffer.clear()
                        dropping = False
        if capture_code == Code.OK:
            try:
                _ = child.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                capture_code = Code.TIMEOUT
    except KeyboardInterrupt:
        capture_code = Code.INTERRUPTED
    except (OSError, ValueError):
        capture_code = Code.IO
    finally:
        _ = signal.signal(signal.SIGTERM, old_term)
        selector.close()
        if child is not None:
            if capture_code != Code.OK or child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            code = child.wait()
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
            store.atomic("exit.json", {"exit_code": code, "capture_code": capture_code,
                                       "stderr_size": stderr_size, "stderr_sha256": stderr_digest.hexdigest(),
                                       "lifecycle": "exited", "exited_ns": time.time_ns()})
        if capture_code != Code.OK:
            capture.failed, capture.completed = True, False
            store.atomic("capture.json", {"stage": Stage.PROCESS, "code": capture_code})
        capture.persist(store)
    result = recover(store, native, context, policy)
    if progress is not None:
        progress({"lifecycle": result.lifecycle, "complete": result.complete,
                  "parent_approval": False, "model_identity": "UNVERIFIED"})
    return result


def status_code(result: Diagnostic) -> int:
    """0=capture complete for ANY verdict; 1=incomplete execution; 2=rejected payload."""
    if not result.schema_valid or not result.revisions_valid or result.code != Code.OK:
        return 2
    return 0 if result.complete else 1
