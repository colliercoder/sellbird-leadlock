"""The Leadlock CRM hub over HTTPS: the only place this extension talks to.

Settings come from the environment Sellbird runs in (never from a file in this package):

    LEADLOCK_HUB_URL        https://<the Leadlock CRM host>, no path
    LEADLOCK_HUB_WORKSPACE  the workspace slug
    LEADLOCK_CRM_TOKEN      the workspace's agency token (a secret: never logged)

    GET  {url}/api/agency/workspaces/{ws}/last-touch?contact=<email>   -> {"last_touch": {...} | null}
    POST {url}/api/agency/workspaces/{ws}/touches   {"touches": [...]}  1 to 500, idempotent

Every failure raises HubError with a fixed message (never the token or a body). A 503 is HubBusy: the hub is
still storing an earlier batch, so the caller keeps its cursor and resends later.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT_S = 10.0
MAX_BATCH = 500
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


class HubError(Exception):
    """The hub could not be asked, or answered something unusable."""


class HubBusy(HubError):
    """503: the hub is still storing an earlier batch."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def settings(env=None) -> tuple:
    env = os.environ if env is None else env
    url = (env.get("LEADLOCK_HUB_URL") or "").strip().rstrip("/")
    ws = (env.get("LEADLOCK_HUB_WORKSPACE") or "").strip()
    token = (env.get("LEADLOCK_CRM_TOKEN") or "").strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.path or parts.query or "@" in parts.netloc:
        raise HubError("leadlock: LEADLOCK_HUB_URL must be https://<host> with no path")
    if not SLUG_RE.fullmatch(ws):
        raise HubError("leadlock: LEADLOCK_HUB_WORKSPACE is not a workspace slug")
    if not token:
        raise HubError("leadlock: LEADLOCK_CRM_TOKEN is not set")
    return url, ws, token


def _request(method: str, path: str, *, query=None, body=None, env=None, opener=None):
    url, ws, token = settings(env)
    full = f"{url}/api/agency/workspaces/{ws}{path}" + ("?" + urllib.parse.urlencode(query) if query else "")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(full, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json",
        **({"Content-Type": "application/json"} if data is not None else {})})
    try:
        if opener is not None:
            status, raw = opener(req, TIMEOUT_S)
        else:
            try:
                with _OPENER.open(req, timeout=TIMEOUT_S) as resp:
                    status, raw = resp.status, resp.read()
            except urllib.error.HTTPError as e:
                status, raw = e.code, e.read()
    except (urllib.error.URLError, OSError, TimeoutError):
        raise HubError(f"leadlock: {method} {path} could not reach the Leadlock CRM") from None
    if status == 503:
        raise HubBusy("leadlock: the Leadlock CRM is still storing an earlier batch")
    if not 200 <= int(status) < 300:
        raise HubError(f"leadlock: {method} {path} answered HTTP {int(status)}")
    try:
        out = json.loads((raw or b"").decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise HubError(f"leadlock: {method} {path} answered something that is not JSON") from None
    if not isinstance(out, dict):
        raise HubError(f"leadlock: {method} {path} answer is not a JSON object")
    return out


def last_touch(email: str, *, env=None, opener=None) -> dict | None:
    out = _request("GET", "/last-touch", query={"contact": email}, env=env, opener=opener)
    lt = out.get("last_touch")
    if lt is not None and not isinstance(lt, dict):
        raise HubError("leadlock: last-touch answer is not an object")
    return lt


def post_touches(touches: list, *, env=None, opener=None) -> dict:
    if not 1 <= len(touches) <= MAX_BATCH:
        raise ValueError(f"a batch is 1 to {MAX_BATCH} touches")
    return _request("POST", "/touches", body={"touches": touches}, env=env, opener=opener)
