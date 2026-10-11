"""Self-authored offline child, not a model/backend or permission-enforcement proof."""

import json
import os
from pathlib import Path
import sys
import time

from tools.review_result import Json


def main() -> int:
    mode, head, base = sys.argv[1:4]
    if mode == "input-disconnect":
        _ = sys.stdin.close()
    else:
        _ = sys.stdin.buffer.read()
    result: dict[str, Json] = {
        "reviewed_head": head, "reviewed_base": base,
        "verdict": mode if mode in ("APPROVE", "REQUEST_CHANGES", "COMMENT") else "COMMENT",
        "findings": [], "limitations": [], "checks_run": [],
    }
    if mode == "missing-field":
        del result["checks_run"]
    elif mode == "unknown-field":
        result["other"] = "untrusted"
    elif mode == "wrong-type":
        result["limitations"] = 42
    elif mode == "revision":
        result["reviewed_head"] = "c" * 40
    elif mode == "unsafe":
        result["limitations"] = ["Bearer synthetic-credential-value"]
    elif mode == "forbidden-field":
        result["token"] = "synthetic"
    elif mode == "findings":
        result["verdict"] = "REQUEST_CHANGES"
        result["findings"] = [{"severity": "P1", "file": "tools/example.py", "line_start": 2,
                               "line_end": 3, "evidence": "offline source", "consequence": "failure",
                               "required_remediation": "fix source"}]
    text = json.dumps(result)
    if mode == "malformed":
        text = "not JSON"
    elif mode == "duplicate":
        text = text[:-1] + ', "verdict": "APPROVE"}'
    elif mode == "nonfinite":
        text = text.replace('"findings": []', '"findings": NaN')

    def event(value: dict[str, Json]) -> None:
        print(json.dumps(value), flush=True)

    event({"type": "thread.started", "thread_id": "12345678-1234-1234-1234-123456789abc"})
    event({"type": "turn.started"})
    event({"type": "item.completed", "item": {"type": "agent_message", "text": "initial commentary"}})
    event({"type": "item.completed", "item": {"type": "reasoning", "text": "PRIVATE reasoning never persisted"}})
    event({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
    if mode == "timeout":
        time.sleep(10)
    if mode not in ("incomplete", "interrupted"):
        event({"type": "turn.completed"})
    elif mode == "interrupted":
        event({"type": "turn.failed", "error": {"message": "private ignored error"}})
    if mode == "later-incomplete":
        event({"type": "turn.started"})
    if mode == "jsonl-malformed":
        print("malformed event", flush=True)
    if mode == "conflict":
        other = dict(result)
        other["verdict"] = "APPROVE"
        text = json.dumps(other)
    elif mode == "file-malformed":
        text = "native non-JSON despite exit zero"
    elif mode == "file-unsafe":
        other = dict(result)
        other["limitations"] = ["Bearer synthetic-credential-value"]
        text = json.dumps(other)
    if mode not in ("missing-file", "incomplete", "timeout"):
        _ = Path("last-message.json").write_text(text)
        if mode == "mode":
            Path("last-message.json").chmod(0o644)
    if mode == "nonzero":
        print("PRIVATE stderr never persisted", file=sys.stderr)
        return 7
    if mode == "environment":
        assert os.environ.get("SYNTHETIC_VALUE") == "fixture-only"
        assert os.environ.get("HOME") == os.getcwd()
        assert os.environ.get("OWNER_SECRET") is None
    return 0


if __name__ == "__main__":
    sys.exit(main())
