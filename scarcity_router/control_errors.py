"""Shared control-surface vocabulary (M09, issue #94).

One tiny module owns the control surface's error type and wire constants
so the control API (:mod:`scarcity_router.control_api`) and the web UI
(:mod:`scarcity_router.server_ui`) can share them without a runtime
import cycle. It contains no logic and performs no I/O.
"""

from __future__ import annotations

MACHINE_STATUS_PATH = "/v1/status"
MACHINE_SELECT_PATH = "/v1/select"
MACHINE_SIMULATE_PATH = "/v1/simulate"

CONTROL_PREFIX = "/control"
ADMIN_PREFIX = "/admin"
ROOT_PATH = "/"

SESSION_COOKIE_NAME = "scarcity-router-admin"
CSRF_HEADER_NAME = "X-Scarcity-CSRF"
CSRF_FORM_FIELD = "csrf"


class ControlHTTPError(Exception):
    """A typed control-surface failure carrying its safe response.

    The default ``invalid_request``/``internal_error`` code/message pair is
    byte-identical to the machine-interface v1 payloads, so the M08 remote
    bridge sees exactly the frozen error vocabulary on the machine
    endpoints while administration endpoints may carry more specific safe
    codes.
    """

    status: int
    code: str
    message: str

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    @staticmethod
    def invalid_request(message: str = "invalid request") -> "ControlHTTPError":
        return ControlHTTPError(400, "invalid_request", message)

    @staticmethod
    def unauthenticated(message: str = "authentication required") -> "ControlHTTPError":
        return ControlHTTPError(401, "unauthenticated", message)

    @staticmethod
    def forbidden(message: str = "forbidden") -> "ControlHTTPError":
        return ControlHTTPError(403, "forbidden", message)

    @staticmethod
    def not_found(message: str = "not found") -> "ControlHTTPError":
        return ControlHTTPError(404, "not_found", message)

    @staticmethod
    def conflict(message: str) -> "ControlHTTPError":
        return ControlHTTPError(409, "conflict", message)

    @staticmethod
    def method_not_allowed() -> "ControlHTTPError":
        return ControlHTTPError(405, "method_not_allowed", "method not allowed")


__all__ = [
    "ADMIN_PREFIX",
    "CONTROL_PREFIX",
    "CSRF_FORM_FIELD",
    "CSRF_HEADER_NAME",
    "ControlHTTPError",
    "MACHINE_SELECT_PATH",
    "MACHINE_SIMULATE_PATH",
    "MACHINE_STATUS_PATH",
    "ROOT_PATH",
    "SESSION_COOKIE_NAME",
]
