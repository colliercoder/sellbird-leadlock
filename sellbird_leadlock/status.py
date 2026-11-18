"""What the Leadlock Orchestrator reads back from Sellbird, with Sellbird's agent token (GET only).

Registered with the leadlock.orchestrator rule (plugin.install), so their absence tells the Orchestrator the rule
is not running:

- tick step `leadlock.orchestrator`: each heartbeat, in the process that sends email, records that the rule is
  registered there and whether the hold file loads now (a Sellbird setting; never an address or a hash).
- GET /api/leadlock/orchestrator: that record, or null when no heartbeat has written it.
- GET /api/leadlock/sent?after=<ISO>&cursor=<cursor>&limit=<n>: sent emails oldest first, so a reader can page
  past the dashboard's newest 25. Without a cursor, from `after` on; with one, strictly after it (each row carries
  the cursor of its own position). Times are compared as times, never as text: Sellbird writes sent_at as
  "2026-11-18T10:00:00.123456" and older rows may read "2026-11-18 10:00:00". Each row carries
  email_sha256, the hash the hold file uses (holds.email_hash), never the address, subject or body.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from . import __version__, holds

SETTING = "leadlock.orchestrator.rule"
MAX_SENT = 200
STATUS_PATH = "/api/leadlock/orchestrator"
SENT_PATH = "/api/leadlock/sent"


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def beacon(ctx) -> None:
    now = datetime.now(timezone.utc)
    path = os.environ.get(holds.ENV, "")
    try:
        if not path:
            raise holds.Unusable(f"{holds.ENV} is not set")
        holds.load(path, now)
        file = "ok"
    except holds.Unusable as e:
        file = str(e)
    await ctx.state.set_setting(SETTING, json.dumps({"active_at": _iso(now), "version": __version__,
                                                     "file": file}, sort_keys=True))


def _sql_time(text: str) -> str | None:
    """An ISO time with a zone, as UTC text SQLite's julianday() reads."""
    try:
        d = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if d.tzinfo is None:
        return None
    return d.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _cursor(text: str):
    """(julian day, outbox id) from "<julian day>|<id>", or None."""
    day, bar, oid = text.partition("|")
    try:
        value = float(day)
    except ValueError:
        return None
    return (value, oid) if bar and oid and value == value else None


async def _query(sql: str, params: tuple) -> list:
    from sellbird import dashboard  # the running dashboard: its database, in its home
    return await dashboard.query_db(sql, params)


def router():
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse
    r = APIRouter()

    @r.get(STATUS_PATH)
    async def orchestrator_status():
        rows = await _query("SELECT value FROM settings WHERE key = ?", (SETTING,))
        try:
            rule = json.loads(rows[0]["value"]) if rows else None
        except (KeyError, TypeError, ValueError):
            rule = None
        return {"rule": rule if isinstance(rule, dict) else None, "version": __version__}

    @r.get(SENT_PATH)
    async def sent(after: str = "", cursor: str = "", limit: int = 100):
        bad = JSONResponse({"error": f"after must be an ISO time with a zone, cursor one this route gave, limit 1 "
                                     f"to {MAX_SENT}"}, status_code=400)
        if not 1 <= limit <= MAX_SENT:
            return bad
        if cursor:
            point = _cursor(cursor)
            if point is None:
                return bad
        else:
            since = _sql_time(after)
            if since is None:
                return bad
            rows = await _query("SELECT julianday(?) AS jd", (since,))
            point = (rows[0]["jd"], "") if rows and rows[0].get("jd") is not None else None
            if point is None:
                return bad
        rows = await _query(
            "SELECT id, message_id, to_email, prospect_id, campaign_id, step, julianday(sent_at) AS jd, "
            "strftime('%Y-%m-%dT%H:%M:%fZ', sent_at) AS at FROM outbox "
            "WHERE status = 'sent' AND julianday(sent_at) IS NOT NULL "
            "AND (julianday(sent_at) > ? OR (julianday(sent_at) = ? AND id > ?)) "
            "ORDER BY julianday(sent_at), id LIMIT ?", (point[0], point[0], point[1], limit))
        return {"sent": [{"id": r.get("id"), "message_id": r.get("message_id"),
                          "email_sha256": holds.email_hash(r["to_email"]) if r.get("to_email") else "",
                          "prospect_id": r.get("prospect_id"), "campaign_id": r.get("campaign_id"),
                          "step": r.get("step"), "sent_at": r.get("at"), "cursor": f"{r['jd']!r}|{r['id']}"}
                         for r in rows]}

    return r
