"""What the Leadlock Orchestrator reads back from Sellbird, with Sellbird's agent token (GET only).

Registered with the leadlock.orchestrator rule (plugin.install), so their absence tells the Orchestrator the rule
is not running:

- tick step `leadlock.orchestrator`: each heartbeat, in the process that sends email, records that the rule is
  registered there and whether the hold file loads now (a Sellbird setting; never an address or a hash).
- GET /api/leadlock/orchestrator: that record, or null when no heartbeat has written it.
- GET /api/leadlock/sent?after=<ISO>&after_id=<id>&limit=<n>: sent emails after a (time, outbox id) point,
  oldest first, so a reader can page past the dashboard's newest 25 (an empty after_id: from that time on).
  The address is returned (the Orchestrator matches it to its contact and logs only a hash); never the subject
  or body.
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


def _sql_ts(text: str) -> str | None:
    try:
        d = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if d.tzinfo is None:
        return None
    return d.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _iso_from_sql(ts) -> str:
    try:
        d = datetime.fromisoformat(str(ts).replace(" ", "T", 1))
    except ValueError:
        return ""
    return _iso(d if d.tzinfo else d.replace(tzinfo=timezone.utc))


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
    async def sent(after: str = "", after_id: str = "", limit: int = 100):
        since = _sql_ts(after)
        if since is None or not 1 <= limit <= MAX_SENT:
            return JSONResponse({"error": f"after must be an ISO time with a zone, limit 1 to {MAX_SENT}"},
                                status_code=400)
        rows = await _query("SELECT id, message_id, to_email, prospect_id, campaign_id, step, sent_at FROM outbox "
                            "WHERE status = 'sent' AND (sent_at > ? OR (sent_at = ? AND id > ?)) "
                            "ORDER BY sent_at, id LIMIT ?", (since, since, after_id, limit))
        return {"sent": [{**{k: r.get(k) for k in ("id", "message_id", "to_email", "prospect_id", "campaign_id",
                                                   "step")}, "sent_at": _iso_from_sql(r.get("sent_at"))}
                         for r in rows]}

    return r
