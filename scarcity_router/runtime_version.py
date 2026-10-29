"""Bounded exact runtime-version identities for source evidence."""

import re

_VERSION = re.compile(
    r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"
    + r"(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"
)


def validate_runtime_version(value: object) -> str:
    """Accept a complete, bounded version token without exposing invalid input."""
    if not isinstance(value, str) or len(value) > 64 or _VERSION.fullmatch(value) is None:
        raise ValueError("runtime version must be a bounded complete version identity")
    return value
