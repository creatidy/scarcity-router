"""The lightweight administration web UI (M09, issue #94).

Intentionally small and professional: server-rendered standard-library
HTML with one inline stylesheet — no frontend framework, no client-side
build, no JavaScript requirement, and no new runtime dependency (D-013:
each dependency must justify itself; none of these pages needs one). The
UI is administration, not an agent framework, and it adds no selection
logic: every page reads from or calls into :class:`~scarcity_router.control_api.ControlPlane`
services, the same authoritative core the control API uses.

Flows (in the prioritized order of issue #94): first-run onboarding,
providers and resources, routing aliases/profiles, client keys, worker
pairing, diagnostics, and copyable client configuration.

Security posture (D-044/A0): every page except onboarding/login requires
an authenticated administrator session; every mutating form carries the
per-session CSRF token; the session cookie is ``HttpOnly``/``SameSite=
Strict`` (``Secure`` on TLS); no secret is ever placed in a URL; issued
client keys and worker pairing codes are rendered exactly once on their
issuance response and never again; pages never display provider
credentials (only whether one is stored); all dynamic values are
HTML-escaped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from .control_errors import ControlHTTPError

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for typing only
    from .control_api import ControlPlane
    from .gateway_server import GatewayRequestHandler

_NAV = (
    ("/admin", "Overview"),
    ("/admin/providers", "Providers"),
    ("/admin/resources", "Resources"),
    ("/admin/aliases", "Aliases"),
    ("/admin/clients", "Client keys"),
    ("/admin/workers", "Workers"),
    ("/admin/diagnostics", "Diagnostics"),
    ("/admin/client-config", "Client configuration"),
)

_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font-family: system-ui, sans-serif; margin: 0; background: #f6f7f9;
       color: #1c2430; }
header { background: #24354d; color: #fff; padding: 0.7rem 1.2rem;
         display: flex; gap: 1.2rem; align-items: baseline; flex-wrap: wrap; }
header .brand { font-weight: 700; letter-spacing: 0.02em; }
nav a { color: #cfe0f5; text-decoration: none; margin-right: 0.9rem; }
nav a:hover { text-decoration: underline; }
main { max-width: 60rem; margin: 1.2rem auto; padding: 0 1rem; }
h1 { font-size: 1.3rem; } h2 { font-size: 1.1rem; margin-top: 1.6rem; }
table { border-collapse: collapse; width: 100%; background: #fff;
        border: 1px solid #d7dde6; }
th, td { text-align: left; padding: 0.45rem 0.6rem; border-bottom: 1px solid #e6eaf0;
         vertical-align: top; font-size: 0.92rem; }
th { background: #eef2f7; }
code, pre { background: #eef2f7; padding: 0.1rem 0.3rem; font-size: 0.9em; }
pre { padding: 0.7rem; overflow-x: auto; border: 1px solid #d7dde6; }
form.inline { display: inline; }
fieldset { border: 1px solid #d7dde6; background: #fff; margin: 0 0 1.2rem 0;
           padding: 0.8rem 1rem 1rem; }
legend { font-weight: 600; padding: 0 0.3rem; }
label { display: block; margin: 0.5rem 0 0.15rem; font-size: 0.9rem; }
input[type=text], input[type=password], input[type=url], input[type=number],
select, textarea { width: 100%; max-width: 28rem; padding: 0.35rem;
                   border: 1px solid #b9c2cf; font: inherit; }
button { margin-top: 0.5rem; padding: 0.35rem 0.9rem; font: inherit;
         background: #24354d; color: #fff; border: 0; cursor: pointer; }
button.secondary { background: #64748b; }
.badge { display: inline-block; padding: 0.05rem 0.45rem; border-radius: 0.6rem;
         font-size: 0.78rem; color: #fff; }
.on { background: #1d7a46; } .off { background: #a83232; } .na { background: #64748b; }
.notice { background: #e8f5ec; border: 1px solid #9fd4b2; padding: 0.7rem 1rem;
          margin-bottom: 1rem; }
.error { background: #fbeaea; border: 1px solid #e2a1a1; padding: 0.7rem 1rem;
         margin-bottom: 1rem; }
.muted { color: #5b6675; font-size: 0.88rem; }
.once { background: #fff8e6; border: 1px solid #e5d28a; padding: 0.7rem 1rem;
        margin-bottom: 1rem; }
@media (prefers-color-scheme: dark) {
  body { background: #141a22; color: #e4e9f0; }
  table, fieldset, pre { background: #1d2530; border-color: #33404f; }
  th { background: #232e3c; }
  code { background: #232e3c; }
  input, select, textarea { background: #141a22; color: #e4e9f0;
                            border-color: #45536a; }
  .notice { background: #17301f; border-color: #2f6b42; }
  .error { background: #331a1a; border-color: #7a3d3d; }
  .once { background: #332c14; border-color: #6f6124; }
}
"""


def dispatch_ui(
    plane: "ControlPlane",
    method: str,
    path: str,
    handler: "GatewayRequestHandler",
) -> bool:
    """Serve one web-UI request; ``False`` when the path is not UI."""
    if not (path == "/" or path.startswith("/admin")):
        return False
    try:
        _route(plane, method, path, handler)
    except ControlHTTPError as exc:
        plane.send_html(
            handler,
            exc.status,
            _page(
                plane,
                handler,
                "Error",
                f'<div class="error"><strong>{_esc(exc.code)}</strong> '
                + f"{_esc(exc.message)}</div>"
                + '<p><a href="/admin">Back to the overview</a></p>',
                authenticated=False,
            ),
        )
    return True


def _route(
    plane: "ControlPlane",
    method: str,
    path: str,
    handler: "GatewayRequestHandler",
) -> None:
    if method not in ("GET", "POST", "HEAD"):
        plane.send_html(handler, 405, _error_page(plane, handler, "method not allowed"))
        return
    if path == "/":
        _home(plane, method, handler)
        return
    routes = {
        "/admin": _dashboard,
        "/admin/onboarding": _onboarding,
        "/admin/login": _login,
        "/admin/providers": _providers,
        "/admin/resources": _resources,
        "/admin/aliases": _aliases,
        "/admin/clients": _clients,
        "/admin/workers": _workers,
        "/admin/diagnostics": _diagnostics,
        "/admin/client-config": _client_config,
        "/admin/providers/add": _providers_add,
        "/admin/providers/delete": _providers_delete,
        "/admin/resources/add": _resources_add,
        "/admin/resources/toggle": _resources_toggle,
        "/admin/resources/delete": _resources_delete,
        "/admin/resources/connection-test": _resources_connection_test,
        "/admin/aliases/add": _aliases_add,
        "/admin/aliases/delete": _aliases_delete,
        "/admin/clients/issue": _clients_issue,
        "/admin/clients/revoke": _clients_revoke,
        "/admin/workers/initiate": _workers_initiate,
        "/admin/workers/revoke": _workers_revoke,
        "/admin/workers/rotate": _workers_rotate,
        "/admin/logout": _logout,
    }
    target = routes.get(path)
    if target is None:
        plane.send_html(handler, 404, _error_page(plane, handler, "unknown page"))
        return
    target(plane, method, handler)


# ── Shared rendering ──────────────────────────────────────────────────────────


def _esc(value: object) -> str:
    from html import escape

    return escape(str(value), quote=True)


def _as_dict(value: object) -> dict[str, object]:
    """Narrow one JSON-object view value for rendering."""
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


def _as_list_of_dicts(value: object) -> list[dict[str, object]]:
    return [item for item in _as_list(value) if isinstance(item, dict)]


def _as_str_list(value: object) -> list[str]:
    return [item for item in _as_list(value) if isinstance(item, str)]


def _badge(state: bool, on_text: str, off_text: str) -> str:
    if state:
        return f'<span class="badge on">{_esc(on_text)}</span>'
    return f'<span class="badge off">{_esc(off_text)}</span>'


def _page(
    plane: "ControlPlane",
    handler: "GatewayRequestHandler",
    title: str,
    body: str,
    *,
    authenticated: bool = True,
) -> str:
    nav = ""
    logout = ""
    if authenticated:
        links = "".join(
            f'<a href="{_esc(href)}">{_esc(label)}</a>' for href, label in _NAV
        )
        csrf = plane.csrf_token_for(handler) or ""
        logout = (
            '<form class="inline" method="post" action="/admin/logout">'
            + f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
            + '<button class="secondary" type="submit">Log out</button></form>'
        )
        nav = f"<nav>{links}</nav>"
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        + f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        + f"<title>{_esc(title)} — Scarcity Router</title>"
        + f"<style>{_STYLE}</style></head><body>"
        + f"<header><span class=\"brand\">Scarcity Router</span>{nav}{logout}</header>"
        + f"<main><h1>{_esc(title)}</h1>{body}</main></body></html>"
    )


def _error_page(plane: "ControlPlane", handler: "GatewayRequestHandler", message: str) -> str:
    return _page(
        plane,
        handler,
        "Error",
        f"<div class=\"error\">{_esc(message)}</div>"
        + '<p><a href="/admin">Back to the overview</a></p>',
        authenticated=False,
    )


def _form_open(action: str, csrf: str) -> str:
    return (
        f"<form method=\"post\" action=\"{_esc(action)}\">"
        + f"<input type=\"hidden\" name=\"csrf\" value=\"{_esc(csrf)}\">"
    )


def _redirect(plane: "ControlPlane", handler: "GatewayRequestHandler", location: str) -> None:
    plane.send_redirect(handler, location)


def _session_or_redirect(
    plane: "ControlPlane", handler: "GatewayRequestHandler"
) -> bool:
    """True when an administrator session is present; redirects otherwise."""
    if not plane.admin_configured():
        _redirect(plane, handler, "/admin/onboarding")
        return False
    try:
        _ = plane.require_admin_session(handler, mutating=False)
    except ControlHTTPError:
        _redirect(plane, handler, "/admin/login")
        return False
    return True


def _csrf_of(plane: "ControlPlane", handler: "GatewayRequestHandler") -> str:
    token = plane.csrf_token_for(handler)
    if token is None:
        raise ControlHTTPError.unauthenticated("administrator session expired")
    return token


# ── Pages ─────────────────────────────────────────────────────────────────────


def _home(
    plane: "ControlPlane", method: str, handler: "GatewayRequestHandler"
) -> None:
    _ = method
    if not plane.admin_configured():
        _redirect(plane, handler, "/admin/onboarding")
        return
    if plane.session_token(handler) is None:
        _redirect(plane, handler, "/admin/login")
        return
    _redirect(plane, handler, "/admin")


def _onboarding(
    plane: "ControlPlane", method: str, handler: "GatewayRequestHandler"
) -> None:
    if plane.admin_configured():
        _redirect(plane, handler, "/admin/login")
        return
    if method == "GET":
        body = (
            "<p>This server has no administrator credential yet. Choose a "
            + "strong password to finish first-run setup. There is no default "
            + "password (D-044); the credential is stored only as a PBKDF2 "
            + "verifier.</p>"
            + "<form method=\"post\" action=\"/admin/onboarding\">"
            + "<label for=\"password\">Administrator password (at least 12 characters)</label>"
            + "<input id=\"password\" name=\"password\" type=\"password\" "
            + "autocomplete=\"new-password\" required minlength=\"12\">"
            + "<label><input type=\"checkbox\" name=\"confirm\" value=\"yes\" required> "
            + "I understand this credential administrates this server</label>"
            + "<button type=\"submit\">Create administrator credential</button>"
            + "</form>"
        )
        plane.send_html(
            handler, 200, _page(plane, handler, "First-run setup", body, authenticated=False)
        )
        return
    form = plane.read_form(handler)
    plane.service_bootstrap_admin(
        password=form.get("password"),
        confirm=form.get("confirm") == "yes",
    )
    cookie = plane.create_session_cookie()
    _redirect_with_cookie(plane, handler, "/admin", cookie)


def _login(plane: "ControlPlane", method: str, handler: "GatewayRequestHandler") -> None:
    if not plane.admin_configured():
        _redirect(plane, handler, "/admin/onboarding")
        return
    if method == "GET":
        body = (
            "<form method=\"post\" action=\"/admin/login\">"
            + "<label for=\"password\">Administrator password</label>"
            + "<input id=\"password\" name=\"password\" type=\"password\" "
            + "autocomplete=\"current-password\" required>"
            + "<button type=\"submit\">Log in</button></form>"
        )
        plane.send_html(
            handler, 200, _page(plane, handler, "Log in", body, authenticated=False)
        )
        return
    form = plane.read_form(handler)
    plane.service_login(form)
    cookie = plane.create_session_cookie()
    _redirect_with_cookie(plane, handler, "/admin", cookie)


def _logout(plane: "ControlPlane", method: str, handler: "GatewayRequestHandler") -> None:
    _ = method
    try:
        token = plane.require_admin_session(handler, mutating=True)
    except ControlHTTPError:
        _redirect(plane, handler, "/admin/login")
        return
    plane.service_logout(token)
    _redirect_with_cookie(plane, handler, "/admin/login", plane.clear_session_cookie())


def _redirect_with_cookie(
    plane: "ControlPlane",
    handler: "GatewayRequestHandler",
    location: str,
    cookie: str,
) -> None:
    _ = plane
    try:
        handler.send_response(303)
        handler.send_header("Location", location)
        handler.send_header("Set-Cookie", cookie)
        handler.send_header("Content-Length", "0")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
    except OSError:
        handler.close_connection = True


def _dashboard(
    plane: "ControlPlane", method: str, handler: "GatewayRequestHandler"
) -> None:
    _ = method
    if not _session_or_redirect(plane, handler):
        return
    state = plane.state_document()
    rows = "".join(
        f"<tr><th>{_esc(key)}</th><td>{_esc(value)}</td></tr>"
        for key, value in (
            ("Administrator configured", state.get("admin_configured")),
            ("Provider endpoints", state.get("providers")),
            ("Resources enabled", state.get("resources_enabled")),
            ("Resources disabled", state.get("resources_disabled")),
            ("Routing aliases", state.get("aliases")),
            ("Active client keys", state.get("client_keys_active")),
            ("Revoked client keys", state.get("client_keys_revoked")),
            ("Workers", state.get("workers")),
            ("Adapter channels ready", state.get("adapters_ready")),
            (
                "Version",
                _as_dict(state.get("versions")).get("scarcity_router"),
            ),
        )
    )
    body = (
        "<table>"
        + "<tr><th>Server state</th><th>Value</th></tr>"
        + rows
        + "</table>"
        + "<p class=\"muted\">Control and diagnostic reads never consume "
        + "inference quota. A real generation test always requires an "
        + "explicit confirmation action.</p>"
    )
    plane.send_html(handler, 200, _page(plane, handler, "Overview", body))


def _providers(
    plane: "ControlPlane", method: str, handler: "GatewayRequestHandler"
) -> None:
    _ = method
    if not _session_or_redirect(plane, handler):
        return
    csrf = _csrf_of(plane, handler)
    rows = ""
    for provider in plane.providers_view():
        rows += (
            "<tr>"
            + f"<td><code>{_esc(provider['provider_id'])}</code></td>"
            + f"<td>{_esc(provider['adapter_id'])}</td>"
            + f"<td><code>{_esc(provider['base_url'])}</code></td>"
            + f"<td>{_badge(bool(provider['credential_configured']), 'credential stored', 'no credential')}</td>"
            + "<td><form class=\"inline\" method=\"post\" action=\"/admin/providers/delete\">"
            + f"<input type=\"hidden\" name=\"csrf\" value=\"{_esc(csrf)}\">"
            + f"<input type=\"hidden\" name=\"provider_id\" value=\"{_esc(provider['provider_id'])}\">"
            + "<button class=\"secondary\" type=\"submit\">Remove</button></form></td>"
            + "</tr>"
        )
    if not rows:
        rows = "<tr><td colspan=\"5\" class=\"muted\">No provider endpoints configured.</td></tr>"
    adapter_hint = "zai-coding-plan"
    body = (
        f"<div class=\"notice\">Provider credentials are stored in the "
        + "server's bounded credential store and are never displayed, "
        + "exported or logged. Plain HTTP is accepted only for loopback "
        + "provider endpoints.</div>"
        + "<table><tr><th>Provider</th><th>Adapter</th><th>Base URL</th>"
        + "<th>Credential</th><th></th></tr>"
        + rows
        + "</table>"
        + "<h2>Add a provider endpoint</h2>"
        + "<fieldset><legend>Provider endpoint</legend>"
        + _form_open("/admin/providers/add", csrf)
        + "<label>Provider id (lowercase id, e.g. <code>zai-http</code>)</label>"
        + "<input type=\"text\" name=\"provider_id\" required>"
        + f"<label>Adapter preset (e.g. <code>{_esc(adapter_hint)}</code>)</label>"
        + "<input type=\"text\" name=\"adapter_id\" required>"
        + "<label>Base URL</label>"
        + "<input type=\"url\" name=\"base_url\" placeholder=\"https://api.example.com\" required>"
        + "<label>Label (optional)</label>"
        + "<input type=\"text\" name=\"label\">"
        + "<label>API credential (stored once, never shown again)</label>"
        + "<input type=\"password\" name=\"secret\" autocomplete=\"off\">"
        + "<button type=\"submit\">Add provider endpoint</button></form></fieldset>"
    )
    plane.send_html(handler, 200, _page(plane, handler, "Providers", body))


def _providers_add(
    plane: "ControlPlane", method: str, handler: "GatewayRequestHandler"
) -> None:
    _ = method
    try:
        _ = plane.require_admin_session(handler, mutating=True)
        form = plane.read_form(handler)
        secret = form.get("secret") or None
        document: dict[str, object] = {
            "provider_id": form.get("provider_id"),
            "adapter_id": form.get("adapter_id"),
            "base_url": form.get("base_url"),
        }
        label = form.get("label")
        if label:
            document["label"] = label
        if secret:
            document["secret"] = secret
        _ = plane.service_add_provider(document)
    except ControlHTTPError as exc:
        _render_banner_page(plane, handler, "Providers", "/admin/providers", exc.message, error=True, status=exc.status)
        return
    _redirect(plane, handler, "/admin/providers")


def _providers_delete(
    plane: "ControlPlane", method: str, handler: "GatewayRequestHandler"
) -> None:
    _ = method
    try:
        _ = plane.require_admin_session(handler, mutating=True)
        form = plane.read_form(handler)
        plane.service_remove_provider(form.get("provider_id") or "")
    except ControlHTTPError as exc:
        _render_banner_page(plane, handler, "Providers", "/admin/providers", exc.message, error=True, status=exc.status)
        return
    _redirect(plane, handler, "/admin/providers")


def _resources(
    plane: "ControlPlane", method: str, handler: "GatewayRequestHandler"
) -> None:
    _ = method
    if not _session_or_redirect(plane, handler):
        return
    csrf = _csrf_of(plane, handler)
    rows = ""
    for resource in plane.resources_view():
        ladder = _as_dict(resource.get("ladder"))
        stages = "".join(
            _badge(bool(ladder.get(stage)), stage, stage)
            + " "
            for stage in (
                "detected",
                "authenticated",
                "protocol_compatible",
                "available",
                "eligible",
                "promotion_confirmed",
            )
        )
        identity = _as_dict(resource.get("identity"))
        remediation = ladder.get("remediation")
        rows += (
            "<tr>"
            + f"<td><code>{_esc(resource['resource_id'])}</code><br>"
            + f"<span class=\"muted\">{_esc(identity.get('provider'))}/"
            + f"{_esc(identity.get('model'))} via {_esc(identity.get('channel'))}"
            + f" ({_esc(identity.get('entitlement'))})</span></td>"
            + f"<td>{_badge(bool(resource['enabled']), 'enabled', 'disabled')}</td>"
            + f"<td>{stages}"
            + (
                f"<br><span class=\"muted\">{_esc(remediation)}</span>"
                if remediation
                else ""
            )
            + "</td>"
            + "<td>"
            + f"<form class=\"inline\" method=\"post\" action=\"/admin/resources/toggle\">"
            + f"<input type=\"hidden\" name=\"csrf\" value=\"{_esc(csrf)}\">"
            + f"<input type=\"hidden\" name=\"resource_id\" value=\"{_esc(resource['resource_id'])}\">"
            + f"<input type=\"hidden\" name=\"enabled\" value=\""
            + ("0" if resource["enabled"] else "1")
            + "\"><button class=\"secondary\" type=\"submit\">"
            + ("Disable" if resource["enabled"] else "Enable")
            + "</button></form> "
            + "<form class=\"inline\" method=\"post\" action=\"/admin/resources/connection-test\">"
            + f"<input type=\"hidden\" name=\"csrf\" value=\"{_esc(csrf)}\">"
            + f"<input type=\"hidden\" name=\"resource_id\" value=\"{_esc(resource['resource_id'])}\">"
            + "<button class=\"secondary\" type=\"submit\">Test</button></form> "
            + "<form class=\"inline\" method=\"post\" action=\"/admin/resources/delete\">"
            + f"<input type=\"hidden\" name=\"csrf\" value=\"{_esc(csrf)}\">"
            + f"<input type=\"hidden\" name=\"resource_id\" value=\"{_esc(resource['resource_id'])}\">"
            + "<button class=\"secondary\" type=\"submit\">Remove</button></form>"
            + "</td></tr>"
        )
    if not rows:
        rows = "<tr><td colspan=\"4\" class=\"muted\">No execution resources configured.</td></tr>"
    endpoint_options = "".join(
        f"<option value=\"{_esc(provider['provider_id'])}\">"
        + f"{_esc(provider['provider_id'])}</option>"
        for provider in plane.providers_view()
    )
    worker_options = "".join(
        f"<option value=\"{_esc(worker['worker_id'])}\">"
        + f"{_esc(worker['worker_id'])} ({_esc(worker['status'])})</option>"
        for worker in plane.workers_view()
        if worker.get("status") == "active"
    )
    body = (
        "<div class=\"notice\">The ladder shows, for each resource: detected, "
        + "authenticated, protocol-compatible, available, eligible and "
        + "promotion-confirmed. Health checks and discovery never consume "
        + "inference quota.</div>"
        + "<table><tr><th>Resource</th><th>State</th><th>Ladder</th><th></th></tr>"
        + rows
        + "</table>"
        + "<h2>Add an execution resource</h2>"
        + "<fieldset><legend>Resource</legend>"
        + _form_open("/admin/resources/add", csrf)
        + "<label>Resource id (lowercase id)</label>"
        + "<input type=\"text\" name=\"resource_id\" required>"
        + "<label>Execution channel</label>"
        + "<select name=\"channel\">"
        + "<option value=\"server_direct_http\">server_direct_http (server-direct HTTP)</option>"
        + "<option value=\"worker_bridged\">worker_bridged</option>"
        + "<option value=\"local_app_adapter\">local_app_adapter</option>"
        + "</select>"
        + "<label>Provider (e.g. <code>openai</code>, <code>zai</code>, <code>ollama</code>)</label>"
        + "<input type=\"text\" name=\"provider\" required>"
        + "<label>Model</label>"
        + "<input type=\"text\" name=\"model\" required>"
        + "<label>Variant (optional)</label>"
        + "<input type=\"text\" name=\"variant\">"
        + "<label>Entitlement</label>"
        + "<select name=\"entitlement\">"
        + "<option value=\"subscription_included\">subscription_included</option>"
        + "<option value=\"promotional\">promotional</option>"
        + "<option value=\"payg_metered\">payg_metered</option>"
        + "<option value=\"prepaid_credits\">prepaid_credits</option>"
        + "<option value=\"local_ungated\">local_ungated</option>"
        + "<option value=\"unknown\">unknown</option>"
        + "</select>"
        + "<label>Confirmed quota pools (comma-separated, optional)</label>"
        + "<input type=\"text\" name=\"quota_pool_ids\">"
        + "<label>Freshness TTL seconds</label>"
        + "<input type=\"number\" name=\"freshness_ttl_seconds\" value=\"3600\" min=\"1\" required>"
        + "<label>Poll interval seconds (optional)</label>"
        + "<input type=\"number\" name=\"poll_interval_seconds\" min=\"1\">"
        + "<label>Provider endpoint</label>"
        + f"<select name=\"endpoint_id\"><option value=\"\">(none)</option>{endpoint_options}</select>"
        + "<label>Worker</label>"
        + f"<select name=\"worker_id\"><option value=\"\">(none)</option>{worker_options}</select>"
        + "<label>Worker-local adapter id (worker_bridged only, e.g. <code>ollama</code>)</label>"
        + "<input type=\"text\" name=\"local_adapter_id\" placeholder=\"ollama\">"
        + "<label><input type=\"checkbox\" name=\"enabled\" value=\"yes\" checked> enabled</label>"
        + "<button type=\"submit\">Add resource</button></form></fieldset>"
    )
    plane.send_html(handler, 200, _page(plane, handler, "Resources", body))


def _resources_add(
    plane: "ControlPlane", method: str, handler: "GatewayRequestHandler"
) -> None:
    _ = method
    try:
        _ = plane.require_admin_session(handler, mutating=True)
        form = plane.read_form(handler)
        document = _resource_document_from_form(form)
        _ = plane.service_add_resource(document)
    except ControlHTTPError as exc:
        _render_banner_page(plane, handler, "Resources", "/admin/resources", exc.message, error=True, status=exc.status)
        return
    _redirect(plane, handler, "/admin/resources")


def _resource_document_from_form(form: dict[str, str]) -> dict[str, object]:
    from .control_errors import ControlHTTPError

    def _int_field(name: str) -> int | None:
        raw = (form.get(name) or "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            raise ControlHTTPError.invalid_request(
                f"{name} must be a whole number"
            ) from None

    identity: dict[str, object] = {
        "resource_id": form.get("resource_id"),
        "channel": form.get("channel"),
        "provider": form.get("provider"),
        "model": form.get("model"),
        "entitlement": form.get("entitlement"),
    }
    variant = (form.get("variant") or "").strip()
    if variant:
        identity["variant"] = variant
    pools = [
        pool.strip()
        for pool in (form.get("quota_pool_ids") or "").split(",")
        if pool.strip()
    ]
    if pools:
        identity["quota_pool_ids"] = pools
    freshness = _int_field("freshness_ttl_seconds")
    if freshness is None:
        raise ControlHTTPError.invalid_request(
            "freshness_ttl_seconds must be a whole number"
        )
    registration: dict[str, object] = {
        "identity": identity,
        "freshness_ttl_seconds": freshness,
    }
    poll = _int_field("poll_interval_seconds")
    if poll is not None:
        registration["poll_interval_seconds"] = poll
    document: dict[str, object] = {"registration": registration}
    endpoint = (form.get("endpoint_id") or "").strip()
    if endpoint:
        document["endpoint_id"] = endpoint
    worker = (form.get("worker_id") or "").strip()
    if worker:
        document["worker_id"] = worker
    local_adapter = (form.get("local_adapter_id") or "").strip()
    if local_adapter:
        document["local_adapter_id"] = local_adapter
    document["enabled"] = form.get("enabled") == "yes"
    return document


def _resources_toggle(
    plane: "ControlPlane", method: str, handler: "GatewayRequestHandler"
) -> None:
    _ = method
    try:
        _ = plane.require_admin_session(handler, mutating=True)
        form = plane.read_form(handler)
        plane.service_set_resource_enabled(
            form.get("resource_id") or "", form.get("enabled") == "1"
        )
    except ControlHTTPError as exc:
        _render_banner_page(plane, handler, "Resources", "/admin/resources", exc.message, error=True, status=exc.status)
        return
    _redirect(plane, handler, "/admin/resources")


def _resources_delete(
    plane: "ControlPlane", method: str, handler: "GatewayRequestHandler"
) -> None:
    _ = method
    try:
        _ = plane.require_admin_session(handler, mutating=True)
        form = plane.read_form(handler)
        plane.service_remove_resource(form.get("resource_id") or "")
    except ControlHTTPError as exc:
        _render_banner_page(plane, handler, "Resources", "/admin/resources", exc.message, error=True, status=exc.status)
        return
    _redirect(plane, handler, "/admin/resources")


def _resources_connection_test(
    plane: "ControlPlane", method: str, handler: "GatewayRequestHandler"
) -> None:
    _ = method
    resource_id = ""
    result: dict[str, object] | None = None
    error = ""
    error_status = 200
    try:
        _ = plane.require_admin_session(handler, mutating=True)
        form = plane.read_form(handler)
        resource_id = form.get("resource_id") or ""
        result = plane.connection_test_document(resource_id)
    except ControlHTTPError as exc:
        error = exc.message
        error_status = exc.status
    body = ""
    if result is not None:
        checks = "".join(
            "<tr><td>"
            + _badge(bool(check.get("passed")), "pass", "fail")
            + f" {_esc(check.get('check'))}</td><td>{_esc(check.get('detail'))}</td><td>"
            + (_esc(check.get("remediation")) if check.get("remediation") else "")
            + "</td></tr>"
            for check in _as_list_of_dicts(result.get("checks"))
        )
        body = (
            f"<div class=\"notice\">Connection test for "
            + f"<code>{_esc(resource_id)}</code>: <strong>"
            + f"{_esc(result.get('result'))}</strong>. "
            + "No provider request was made; no inference quota was consumed."
            + "</div><table><tr><th>Check</th><th>Detail</th><th>Remediation</th></tr>"
            + checks
            + "</table>"
        )
    elif error:
        body = f"<div class=\"error\">{_esc(error)}</div>"
    body += "<p><a href=\"/admin/resources\">Back to resources</a></p>"
    plane.send_html(
        handler,
        error_status if error else 200,
        _page(plane, handler, "Connection test", body),
    )


def _aliases(
    plane: "ControlPlane", method: str, handler: "GatewayRequestHandler"
) -> None:
    _ = method
    if not _session_or_redirect(plane, handler):
        return
    csrf = _csrf_of(plane, handler)
    rows = ""
    for alias in plane.aliases_view():
        providers = alias.get("allowed_providers")
        rows += (
            "<tr>"
            + f"<td><code>{_esc(alias['alias'])}</code></td>"
            + f"<td><code>{_esc(alias['profile_id'])}</code></td>"
            + f"<td>{_esc(', '.join(_as_str_list(providers)) if providers else '(all configured providers)')}</td>"
            + "<td><form class=\"inline\" method=\"post\" action=\"/admin/aliases/delete\">"
            + f"<input type=\"hidden\" name=\"csrf\" value=\"{_esc(csrf)}\">"
            + f"<input type=\"hidden\" name=\"alias\" value=\"{_esc(alias['alias'])}\">"
            + "<button class=\"secondary\" type=\"submit\">Remove</button></form></td>"
            + "</tr>"
        )
    if not rows:
        rows = (
            "<tr><td colspan=\"4\" class=\"muted\">No routing aliases "
            + "configured; OpenAI-compatible clients can still pin exact "
            + "targets.</td></tr>"
        )
    profile_options = "".join(
        f"<option value=\"{_esc(profile_id)}\">{_esc(profile_id)}</option>"
        for profile_id in plane.known_profile_ids()
    )
    body = (
        "<div class=\"notice\">An alias binds a client-facing model name to "
        + "an existing calibrated task profile. Aliases never add scoring "
        + "semantics (D-042).</div>"
        + "<table><tr><th>Alias</th><th>Profile</th><th>Provider narrowing</th><th></th></tr>"
        + rows
        + "</table>"
        + "<h2>Add a routing alias</h2>"
        + "<fieldset><legend>Alias</legend>"
        + _form_open("/admin/aliases/add", csrf)
        + "<label>Alias (the model name clients use)</label>"
        + "<input type=\"text\" name=\"alias\" required>"
        + "<label>Task profile</label>"
        + f"<select name=\"profile_id\">{profile_options}</select>"
        + "<label>Allowed providers (comma-separated, optional)</label>"
        + "<input type=\"text\" name=\"allowed_providers\">"
        + "<button type=\"submit\">Add alias</button></form></fieldset>"
    )
    plane.send_html(handler, 200, _page(plane, handler, "Routing aliases", body))


def _aliases_add(
    plane: "ControlPlane", method: str, handler: "GatewayRequestHandler"
) -> None:
    _ = method
    try:
        _ = plane.require_admin_session(handler, mutating=True)
        form = plane.read_form(handler)
        document: dict[str, object] = {"profile_id": form.get("profile_id")}
        providers = [
            provider.strip()
            for provider in (form.get("allowed_providers") or "").split(",")
            if provider.strip()
        ]
        if providers:
            document["allowed_providers"] = providers
        plane.service_put_alias(form.get("alias") or "", document)
    except ControlHTTPError as exc:
        _render_banner_page(plane, handler, "Routing aliases", "/admin/aliases", exc.message, error=True, status=exc.status)
        return
    _redirect(plane, handler, "/admin/aliases")


def _aliases_delete(
    plane: "ControlPlane", method: str, handler: "GatewayRequestHandler"
) -> None:
    _ = method
    try:
        _ = plane.require_admin_session(handler, mutating=True)
        form = plane.read_form(handler)
        plane.service_delete_alias(form.get("alias") or "")
    except ControlHTTPError as exc:
        _render_banner_page(plane, handler, "Routing aliases", "/admin/aliases", exc.message, error=True, status=exc.status)
        return
    _redirect(plane, handler, "/admin/aliases")


def _clients(
    plane: "ControlPlane", method: str, handler: "GatewayRequestHandler"
) -> None:
    _ = method
    if not _session_or_redirect(plane, handler):
        return
    csrf = _csrf_of(plane, handler)
    rows = ""
    for client in plane.clients_view():
        revoked = client.get("revoked_at") is not None
        rows += (
            "<tr>"
            + f"<td><code>{_esc(client['client_id'])}</code><br>"
            + f"<span class=\"muted\">{_esc(client['label'])}</span></td>"
            + f"<td>{_esc(client['created_at'])}</td>"
            + "<td>"
            + (
                '<span class="badge off">revoked</span>'
                if revoked
                else '<span class="badge on">active</span>'
            )
            + "</td>"
            + "<td>"
            + (
                ""
                if revoked
                else "<form class=\"inline\" method=\"post\" action=\"/admin/clients/revoke\">"
                + f"<input type=\"hidden\" name=\"csrf\" value=\"{_esc(csrf)}\">"
                + f"<input type=\"hidden\" name=\"client_id\" value=\"{_esc(client['client_id'])}\">"
                + "<button class=\"secondary\" type=\"submit\">Revoke</button></form>"
            )
            + "</td></tr>"
        )
    if not rows:
        rows = (
            "<tr><td colspan=\"4\" class=\"muted\">No client keys issued "
            + "yet.</td></tr>"
        )
    body = (
        "<div class=\"notice\">Client keys authorize inference only — never "
        + "administration. Only the SHA-256 hash of a key is stored; the key "
        + "itself is displayed exactly once at issuance.</div>"
        + "<table><tr><th>Client</th><th>Created</th><th>Status</th><th></th></tr>"
        + rows
        + "</table>"
        + "<h2>Issue a client key</h2>"
        + "<fieldset><legend>New client key</legend>"
        + _form_open("/admin/clients/issue", csrf)
        + "<label>Label (what is this client?)</label>"
        + "<input type=\"text\" name=\"label\" required>"
        + "<label>Client id (optional; generated when empty)</label>"
        + "<input type=\"text\" name=\"client_id\">"
        + "<label>Authorization grant JSON (optional; empty = unrestricted)</label>"
        + "<textarea name=\"authorization\" rows=\"3\" "
        + "placeholder='{\"allowed_providers\": [\"zai\"]}'></textarea>"
        + "<button type=\"submit\">Issue key</button></form></fieldset>"
    )
    plane.send_html(handler, 200, _page(plane, handler, "Client API keys", body))


def _clients_issue(
    plane: "ControlPlane", method: str, handler: "GatewayRequestHandler"
) -> None:
    _ = method
    result: dict[str, object] | None = None
    error = ""
    error_status = 200
    try:
        _ = plane.require_admin_session(handler, mutating=True)
        form = plane.read_form(handler)
        document: dict[str, object] = {"label": form.get("label")}
        client_id = (form.get("client_id") or "").strip()
        if client_id:
            document["client_id"] = client_id
        authorization = (form.get("authorization") or "").strip()
        if authorization:
            from .selection_app import load_strict_json as _load

            try:
                document["authorization"] = _load(authorization, label="authorization")
            except ValueError:
                raise ControlHTTPError.invalid_request(
                    "authorization grant is not valid JSON"
                ) from None
        result = plane.service_issue_client_key(document)
    except ControlHTTPError as exc:
        error = exc.message
        error_status = exc.status
    body = ""
    if result is not None:
        body = (
            "<div class=\"once\"><strong>Copy this API key now — it is shown "
            + "once.</strong><br><pre>"
            + _esc(result.get("api_key"))
            + "</pre>Client id: <code>"
            + _esc(result.get("client_id"))
            + "</code></div>"
        )
    elif error:
        body = f"<div class=\"error\">{_esc(error)}</div>"
    body += "<p><a href=\"/admin/clients\">Back to client keys</a></p>"
    plane.send_html(
        handler,
        error_status if error else 200,
        _page(plane, handler, "Client key issued", body),
    )


def _clients_revoke(
    plane: "ControlPlane", method: str, handler: "GatewayRequestHandler"
) -> None:
    _ = method
    try:
        _ = plane.require_admin_session(handler, mutating=True)
        form = plane.read_form(handler)
        plane.service_revoke_client_key(form.get("client_id") or "")
    except ControlHTTPError as exc:
        _render_banner_page(plane, handler, "Client API keys", "/admin/clients", exc.message, error=True, status=exc.status)
        return
    _redirect(plane, handler, "/admin/clients")


def _workers(
    plane: "ControlPlane", method: str, handler: "GatewayRequestHandler"
) -> None:
    _ = method
    if not _session_or_redirect(plane, handler):
        return
    csrf = _csrf_of(plane, handler)
    rows = ""
    for worker in plane.workers_view():
        status = str(worker.get("status", "unknown"))
        badge_class = {"active": "on", "revoked": "off"}.get(status, "na")
        connected = bool(worker.get("connected"))
        rows += (
            "<tr>"
            + f"<td><code>{_esc(worker['worker_id'])}</code><br>"
            + f"<span class=\"muted\">{_esc(worker.get('label') or '')}</span></td>"
            + f"<td><span class=\"badge {badge_class}\">{_esc(status)}</span>"
            + (
                " <span class=\"badge on\">connected</span>"
                if connected
                else ""
            )
            + "</td>"
            + f"<td>{_esc(worker.get('last_connected_at') or 'never')}</td>"
            + "<td>"
            + (
                ""
                if status == "revoked"
                else "<form class=\"inline\" method=\"post\" action=\"/admin/workers/revoke\">"
                + f"<input type=\"hidden\" name=\"csrf\" value=\"{_esc(csrf)}\">"
                + f"<input type=\"hidden\" name=\"worker_id\" value=\"{_esc(worker['worker_id'])}\">"
                + "<button class=\"secondary\" type=\"submit\">Revoke</button></form> "
                + "<form class=\"inline\" method=\"post\" action=\"/admin/workers/rotate\">"
                + f"<input type=\"hidden\" name=\"csrf\" value=\"{_esc(csrf)}\">"
                + f"<input type=\"hidden\" name=\"worker_id\" value=\"{_esc(worker['worker_id'])}\">"
                + "<button class=\"secondary\" type=\"submit\">Rotate credential</button></form>"
            )
            + "</td></tr>"
        )
    if not rows:
        rows = (
            "<tr><td colspan=\"4\" class=\"muted\">No workers configured; "
            + "server-direct operation only.</td></tr>"
        )
    body = (
        "<div class=\"notice\">Pairing: start it here, then enter the one-time "
        + "code on the worker device together with this server's worker-protocol "
        + "URL. The worker redeems the code inside the verified-TLS protocol "
        + "handshake and receives its per-device credential there; the code "
        + "expires and is shown once (D-044).</div>"
        + "<table><tr><th>Worker</th><th>Status</th><th>Last connection</th><th></th></tr>"
        + rows
        + "</table>"
        + "<h2>Start pairing</h2>"
        + "<fieldset><legend>New worker</legend>"
        + _form_open("/admin/workers/initiate", csrf)
        + "<label>Label (which machine is this?)</label>"
        + "<input type=\"text\" name=\"label\" required>"
        + "<button type=\"submit\">Generate one-time pairing code</button></form></fieldset>"
    )
    plane.send_html(handler, 200, _page(plane, handler, "Workers and pairing", body))


def _workers_initiate(
    plane: "ControlPlane", method: str, handler: "GatewayRequestHandler"
) -> None:
    _ = method
    result: dict[str, object] | None = None
    error = ""
    error_status = 200
    try:
        _ = plane.require_admin_session(handler, mutating=True)
        form = plane.read_form(handler)
        result = plane.service_initiate_pairing({"label": form.get("label")})
    except ControlHTTPError as exc:
        error = exc.message
        error_status = exc.status
    body = ""
    if result is not None:
        body = (
            "<div class=\"once\"><strong>One-time pairing code — shown "
            + "once, expires "
            + f"{_esc(result.get('expires_at'))}.</strong><br><pre>"
            + _esc(result.get("pairing_code"))
            + "</pre>On the worker device, run the worker's <code>pair</code> "
            + "command with this code and this server's worker-protocol URL; "
            + "the handshake over verified TLS creates the device identity "
            + "(the worker id is assigned at pairing time).</div>"
        )
    elif error:
        body = f"<div class=\"error\">{_esc(error)}</div>"
    body += "<p><a href=\"/admin/workers\">Back to workers</a></p>"
    plane.send_html(
        handler,
        error_status if error else 200,
        _page(plane, handler, "Pairing started", body),
    )


def _workers_revoke(
    plane: "ControlPlane", method: str, handler: "GatewayRequestHandler"
) -> None:
    _ = method
    try:
        _ = plane.require_admin_session(handler, mutating=True)
        form = plane.read_form(handler)
        plane.service_revoke_worker(form.get("worker_id") or "")
    except ControlHTTPError as exc:
        _render_banner_page(plane, handler, "Workers and pairing", "/admin/workers", exc.message, error=True, status=exc.status)
        return
    _redirect(plane, handler, "/admin/workers")


def _workers_rotate(
    plane: "ControlPlane", method: str, handler: "GatewayRequestHandler"
) -> None:
    _ = method
    result: dict[str, object] | None = None
    error = ""
    error_status = 200
    try:
        _ = plane.require_admin_session(handler, mutating=True)
        form = plane.read_form(handler)
        result = plane.service_rotate_worker_credential(form.get("worker_id") or "")
    except ControlHTTPError as exc:
        error = exc.message
        error_status = exc.status
    body = ""
    if result is not None:
        body = (
            "<div class=\"once\"><strong>New worker credential — shown "
            + "once.</strong><br><pre>"
            + _esc(result.get("worker_credential"))
            + "</pre>Deliver it to the device now; the old credential stopped "
            + "working immediately and only the salted hash is kept.</div>"
        )
    elif error:
        body = f"<div class=\"error\">{_esc(error)}</div>"
    body += "<p><a href=\"/admin/workers\">Back to workers</a></p>"
    plane.send_html(
        handler,
        error_status if error else 200,
        _page(plane, handler, "Credential rotated", body),
    )


def _diagnostics(
    plane: "ControlPlane", method: str, handler: "GatewayRequestHandler"
) -> None:
    _ = method
    if not _session_or_redirect(plane, handler):
        return
    report = plane.build_diagnostics()
    marker = {"ok": "ok", "warning": "warn", "error": "fail"}
    check_rows = ""
    for check in report.checks:
        state = check.state
        badge = (
            f"<span class=\"badge {marker.get(state, 'na')}\">"
            + ("pass" if state == "ok" else state)
            + "</span>"
        )
        check_rows += (
            "<tr><td>"
            + badge
            + f" {_esc(check.title)}</td><td>{_esc(check.detail)}</td><td>"
            + (_esc(check.remediation) if check.remediation else "")
            + "</td></tr>"
        )
    resource_rows = ""
    for resource in report.resources:
        passed = [
            stage
            for stage, value in (
                ("detected", resource.detected),
                ("authenticated", resource.authenticated),
                ("protocol_compatible", resource.protocol_compatible),
                ("available", resource.available),
                ("eligible", resource.eligible),
                ("promotion confirmed", resource.promotion_confirmed),
            )
            if value
        ]
        resource_rows += (
            "<tr><td><code>"
            + _esc(resource.resource_id)
            + "</code><br><span class=\"muted\">"
            + _esc(f"{resource.provider}/{resource.model} via {resource.channel}")
            + "</span></td><td>"
            + _esc(", ".join(passed) if passed else "none")
            + "</td><td>"
            + (
                _esc(resource.remediation)
                if resource.remediation
                else '<span class="badge on">eligible</span>'
            )
            + "</td></tr>"
        )
    if not resource_rows:
        resource_rows = (
            "<tr><td colspan=\"3\" class=\"muted\">No execution resources "
            + "configured.</td></tr>"
        )
    versions = ", ".join(f"{key} {value}" for key, value in report.versions.items())
    body = (
        "<div class=\"notice\">This report reads stored state only: no "
        + "collector call, no adapter dispatch and no inference request was "
        + "made.</div>"
        + f"<p class=\"muted\">Generated {_esc(report.generated_at)} ({_esc(report.mode)} mode); "
        + f"{_esc(versions)}</p>"
        + "<h2>Checks</h2><table><tr><th>Check</th><th>Detail</th>"
        + "<th>Remediation</th></tr>"
        + check_rows
        + "</table><h2>Resources</h2><table><tr><th>Resource</th>"
        + "<th>Ladder stages passed</th><th>Next step</th></tr>"
        + resource_rows
        + "</table>"
    )
    plane.send_html(handler, 200, _page(plane, handler, "Diagnostics", body))


def _client_config(
    plane: "ControlPlane", method: str, handler: "GatewayRequestHandler"
) -> None:
    _ = method
    if not _session_or_redirect(plane, handler):
        return
    scheme = "https" if plane.is_tls else "http"
    host = handler.headers.get("Host") or "127.0.0.1"
    base_url = f"{scheme}://{host}"
    aliases = plane.aliases_view()
    alias_names = ", ".join(f"`{alias['alias']}`" for alias in aliases) or "(none yet)"
    snippet = (
        "# Any OpenAI-compatible client\n"
        + f"base_url = \"{base_url}/v1\"\n"
        + "api_key  = <a client API key issued on the Client keys page>\n"
        + "model    = <a routing alias above, or sr-pin:<resource>/<provider>/<model>/<variant>>\n"
    )
    body = (
        "<div class=\"notice\">Point any OpenAI-compatible client at this "
        + "server. Keys are issued on the Client keys page and shown exactly "
        + "once; credentials never travel in URLs.</div>"
        + f"<p>Server URL: <code>{_esc(base_url)}</code></p>"
        + "<h2>Copyable client configuration</h2><pre>"
        + _esc(snippet)
        + "</pre>"
        + f"<p>Available routing aliases (the <code>model</code> field): "
        + f"<code>{_esc(alias_names)}</code></p>"
        + "<p class=\"muted\">Machine-interface parity: recommendation "
        + "clients may use the same origin with "
        + "<code>GET /v1/status</code>, <code>POST /v1/select</code> and "
        + "<code>POST /v1/simulate</code> under the same client key.</p>"
    )
    plane.send_html(handler, 200, _page(plane, handler, "Client configuration", body))


def _render_banner_page(
    plane: "ControlPlane",
    handler: "GatewayRequestHandler",
    title: str,
    back: str,
    message: str,
    *,
    error: bool,
    status: int = 200,
) -> None:
    css = "error" if error else "notice"
    body = (
        f"<div class=\"{css}\">{_esc(message)}</div>"
        + f"<p><a href=\"{_esc(back)}\">Back</a></p>"
    )
    plane.send_html(handler, status, _page(plane, handler, title, body))


__all__ = ["dispatch_ui"]
