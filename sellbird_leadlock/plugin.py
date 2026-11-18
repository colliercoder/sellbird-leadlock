"""The extension: send rules and one tick step (Sellbird's extension API, docs/extensions.md). The second send
rule, `leadlock.orchestrator`, is in holds.py and is registered only when SELLBIRD_LEADLOCK_HOLDS is set.

Send rule `leadlock.crosschannel`, asked before every email after Sellbird's own checks. It can only stop an
email, never let one through, and anything that goes wrong holds the email:
    - the Leadlock CRM says the recipient opted out                       -> Cancel
    - a cold email to someone who booked or paid                          -> Cancel
    - the recipient was touched on another channel in the last 24 hours  -> Hold (tried again next cycle)
    - the CRM cannot be asked, or the settings are missing                -> Hold

Tick step `leadlock.events`, run each heartbeat: what Sellbird did since the last run goes to the Leadlock CRM
hub as touches (desk "sellbird"), so email counts next to calls, texts and LinkedIn:
    - every sent email: an outbound touch, external_id its Message-ID
    - every reply: an inbound touch with a category from Sellbird's intent (never the words). It names no
      touch it answers: the hub refuses a whole batch whose reply links to a touch it does not have (an email
      sent before this extension), and it already settles a reply on the newest outbound touch itself
    - every opt-out and bounce: the address's newest outbound touch again, with outcome opted_out or bounced
It reads Sellbird's outbox, conversations, prospects and suppressions tables read-only, with Sellbird's own
sellbird.storage.connect, the way Sellbird's dashboard reads them (Sellbird v0.4.0's schema; the tests run on
it). Each read starts OVERLAP before the last one; the hub drops what it already has. A hub that is busy or
down leaves the cursor (a Sellbird setting) where it was.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from . import hub

logger = logging.getLogger("sellbird_leadlock")

CURSOR_KEY = "leadlock.hub.cursor"
OVERLAP = timedelta(hours=1)
FIRST_LOOKBACK = timedelta(days=1)
RECENT = timedelta(hours=24)
MAX_ROWS = 1000
STOP_COLD = ("booked", "paid")
# Sellbird's conversation intent -> the hub's reply_category
INTENT = {"interested": "interested", "objection": "question", "not_interested": "not_interested",
          "ooo": "other", "wrong_person": "other", "meeting": "booked", "booked": "booked"}


def _utc(ts) -> datetime | None:
    """Sellbird's TIMESTAMP text ("YYYY-MM-DD HH:MM:SS", UTC) or an ISO time, as an aware datetime."""
    if not ts:
        return None
    s = str(ts).strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s.replace(" ", "T", 1) if "T" not in s else s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _sql_ts(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# the send rule

async def crosschannel(check):
    from sellbird.send_hooks import Cancel, Hold
    email = str((check.item or {}).get("to_email") or "").strip().lower()
    if not email:
        return Hold("leadlock: the email has no recipient address")
    try:
        last = await asyncio.to_thread(hub.last_touch, email)
    except hub.HubError as e:
        return Hold(f"{e}; held until the Leadlock CRM answers")
    if not last:
        return None
    outcome, desk = last.get("outcome"), last.get("desk")
    if outcome == "opted_out":
        return Cancel("leadlock: this person opted out (Leadlock CRM)")
    if check.cold and outcome in STOP_COLD:
        return Cancel(f"leadlock: this person already {outcome} (Leadlock CRM)")
    when = _utc(last.get("occurred_at"))
    if desk != "sellbird" and (when is None or datetime.now(timezone.utc) - when < RECENT):
        return Hold("leadlock: touched on another channel in the last 24 hours (Leadlock CRM)")
    return None


# the tick step

async def _rows(db_path: str, sql: str, params: tuple) -> list[dict]:
    import aiosqlite
    from sellbird import storage
    async with storage.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


def _outbound(row: dict, autonomous: bool) -> dict:
    when = _utc(row.get("sent_at"))
    t = {"desk": "sellbird", "channel": "email", "direction": "outbound",
         "kind": "sent" if row.get("kind") == "sequence" else "reply_sent",
         "occurred_at": _iso(when) if when else None,
         "external_id": (row.get("message_id") or f"outbox:{row['id']}")[:300],
         "contact": {"email": str(row.get("to_email") or "").strip().lower()},
         "summary": f"email {row.get('kind') or 'sequence'} step {row.get('step') or 1}"[:200],
         "is_test": False, "autonomous": bool(autonomous)}
    return t


async def collect(state, config, cursor: dict, now: datetime) -> tuple:
    """(touches, new cursor) for everything since the cursor, each read OVERLAP early."""
    db = state.db_path
    autonomous = not bool(getattr(getattr(getattr(config, "channels", None), "email", None), "require_approval", True))
    since = {k: (_utc(cursor.get(k)) or now - FIRST_LOOKBACK) - OVERLAP for k in ("sent", "reply", "supp")}
    touches, newest = [], dict(cursor)

    sent = await _rows(db, "SELECT id, kind, step, to_email, message_id, in_reply_to, sent_at FROM outbox "
                           "WHERE status = 'sent' AND sent_at >= ? ORDER BY sent_at LIMIT ?",
                       (_sql_ts(since["sent"]), MAX_ROWS))
    for r in sent:
        if r.get("to_email") and _utc(r.get("sent_at")):
            touches.append(_outbound(r, autonomous))
            newest["sent"] = max(newest.get("sent") or "", _iso(_utc(r["sent_at"])))

    convos = await _rows(db, "SELECT c.id, c.intent, c.thread_json, c.updated_at, p.email FROM conversations c "
                             "JOIN prospects p ON p.id = c.prospect_id WHERE c.updated_at >= ? "
                             "ORDER BY c.updated_at LIMIT ?", (_sql_ts(since["reply"]), MAX_ROWS))
    for c in convos:
        email = str(c.get("email") or "").strip().lower()
        try:
            thread = json.loads(c.get("thread_json") or "[]")
        except ValueError:
            thread = []
        for m in thread if isinstance(thread, list) else []:
            when = _utc(m.get("timestamp")) if isinstance(m, dict) else None
            if not email or not when or m.get("sender") != "prospect" or when < since["reply"]:
                continue
            t = {"desk": "sellbird", "channel": "email", "direction": "inbound", "kind": "reply",
                 "occurred_at": _iso(when), "external_id": f"reply:{c['id']}:{_iso(when)}"[:300],
                 "contact": {"email": email}, "summary": "email reply", "is_test": False, "autonomous": False,
                 "reply_category": INTENT.get(str(c.get("intent") or ""), "unknown")}
            touches.append(t)
        upd = _utc(c.get("updated_at"))
        if upd:
            newest["reply"] = max(newest.get("reply") or "", _iso(upd))

    supp = await _rows(db, "SELECT value, reason, created_at FROM suppressions WHERE kind = 'address' "
                           "AND reason IN ('opted_out', 'bounced') AND created_at >= ? ORDER BY created_at LIMIT ?",
                       (_sql_ts(since["supp"]), MAX_ROWS))
    for s in supp:
        email = str(s.get("value") or "").strip().lower()
        last = await _rows(db, "SELECT id, kind, step, to_email, message_id, in_reply_to, sent_at FROM outbox "
                               "WHERE status = 'sent' AND lower(to_email) = ? ORDER BY sent_at DESC LIMIT 1", (email,))
        if last:
            touches.append({**_outbound(last[0], autonomous), "outcome": s["reason"]})
        made = _utc(s.get("created_at"))
        if made:
            newest["supp"] = max(newest.get("supp") or "", _iso(made))
    return [t for t in touches if t["occurred_at"]], newest


async def push_events(ctx) -> None:
    try:
        cursor = json.loads(await ctx.state.get_setting(CURSOR_KEY, "") or "{}")
    except ValueError:
        cursor = {}  # a cursor we cannot read only means resending: the hub drops what it has
    touches, newest = await collect(ctx.state, ctx.config, cursor if isinstance(cursor, dict) else {},
                                    datetime.now(timezone.utc))
    for i in range(0, len(touches), hub.MAX_BATCH):
        try:
            await asyncio.to_thread(hub.post_touches, touches[i:i + hub.MAX_BATCH])
        except (hub.HubError, ValueError) as e:  # the cursor stays: the next cycle sends it all again
            logger.warning("%s; the Leadlock CRM gets these touches next cycle", e)
            return
    await ctx.state.set_setting(CURSOR_KEY, json.dumps(newest, sort_keys=True))


def install(registry) -> None:
    import os
    from . import holds
    registry.add_send_rule("leadlock.crosschannel", crosschannel)
    registry.add_tick_step("leadlock.events", push_events)
    if os.environ.get(holds.ENV):  # the Leadlock Orchestrator's hold file (holds.py), only where it is set up
        registry.add_send_rule("leadlock.orchestrator", holds.orchestrator)
