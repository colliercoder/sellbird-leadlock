"""Send rule `leadlock.orchestrator`: the Leadlock Orchestrator's hold file, read before every email.

The Orchestrator writes a small JSON file each run. This rule reads it, with no network, and only ever stops an
email:
    - the file is missing, unreadable, malformed, or older than MAX_AGE      -> Hold (everything waits)
    - "paused" is set                                                         -> Hold
    - the recipient's hash is in "cancel_email_sha256"                        -> Cancel
    - "test_only" is set and the recipient's hash is not in "allow_email_sha256" -> Hold
    - the recipient's hash is in "hold_email_sha256"                          -> Hold (tried again next cycle)
    - the recipient's domain, or a parent domain, is in "hold_domains"        -> Hold
    - otherwise                                                               -> None: Sellbird's own checks decide

A hash is the sha256 hex of the address, stripped and lower-cased. The path comes from the environment variable
SELLBIRD_LEADLOCK_HOLDS; the rule is registered only when it is set (plugin.install).

The file looks like:
    {"version": 1, "generated_at": "2026-01-01T12:00:00Z", "paused": false, "test_only": false,
     "allow_email_sha256": [], "cancel_email_sha256": ["<64 hex>"], "hold_email_sha256": [],
     "hold_domains": ["example.com"]}
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone

ENV = "SELLBIRD_LEADLOCK_HOLDS"
MAX_AGE = timedelta(hours=2)
MAX_BYTES = 8 * 1024 * 1024
VERSION = 1
HEX = re.compile(r"[0-9a-f]{64}")
DOMAIN = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")  # as the Orchestrator's do-not-contact list reads them
LISTS = ("allow_email_sha256", "cancel_email_sha256", "hold_email_sha256")


class Unusable(Exception):
    """The file cannot be trusted; every email holds."""


def email_hash(address: str) -> str:
    return hashlib.sha256(address.strip().lower().encode("utf-8")).hexdigest()


def _when(text) -> datetime:
    if not isinstance(text, str):
        raise Unusable("generated_at is missing")
    try:
        d = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        raise Unusable("generated_at is not a time") from None
    if d.tzinfo is None:
        raise Unusable("generated_at has no time zone")
    return d


def load(path: str, now: datetime) -> dict:
    """The parsed file, with its lists as sets. Raises Unusable for anything that is not exactly right."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_BYTES + 1)
    except OSError:
        raise Unusable("the hold file cannot be read") from None
    if len(raw) > MAX_BYTES:
        raise Unusable("the hold file is too big")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Unusable("the hold file is not JSON") from None
    if not isinstance(doc, dict) or doc.get("version") != VERSION:
        raise Unusable("the hold file has an unknown version")
    made = _when(doc.get("generated_at"))
    age = now - made
    if age > MAX_AGE or age < -timedelta(minutes=5):
        raise Unusable("the hold file is stale")
    for key in ("paused", "test_only"):
        if not isinstance(doc.get(key), bool):
            raise Unusable(f"{key} is not true or false")
    out = {"paused": doc["paused"], "test_only": doc["test_only"]}
    for key in LISTS:
        items = doc.get(key)
        if not isinstance(items, list) or not all(isinstance(h, str) and HEX.fullmatch(h) for h in items):
            raise Unusable(f"{key} is not a list of sha256 hashes")
        out[key] = set(items)
    domains = doc.get("hold_domains")
    if not isinstance(domains, list) or not all(isinstance(d, str) and DOMAIN.fullmatch(d) for d in domains):
        raise Unusable("hold_domains is not a list of domains")
    out["hold_domains"] = set(domains)
    return out


def decide(holds: dict, email: str):
    """("cancel" | "hold" | None, reason) for one recipient, given a loaded file."""
    if holds["paused"]:
        return "hold", "the Orchestrator has paused email"
    h = email_hash(email)
    if h in holds["cancel_email_sha256"]:
        return "cancel", "the Orchestrator says never email this person"
    if holds["test_only"] and h not in holds["allow_email_sha256"]:
        return "hold", "the Orchestrator is in test phase and this is not a test contact"
    if h in holds["hold_email_sha256"]:
        return "hold", "touched on another channel today (Orchestrator)"
    domain = email.rsplit("@", 1)[-1]
    if any(domain == d or domain.endswith("." + d) for d in holds["hold_domains"]):
        return "hold", "the recipient's domain is on the do-not-contact list (Orchestrator)"
    return None, ""


async def orchestrator(check):
    from sellbird.send_hooks import Cancel, Hold
    email = str((check.item or {}).get("to_email") or "").strip().lower()
    if "@" not in email:
        return Hold("leadlock: the email has no recipient address")
    path = os.environ.get(ENV, "")
    if not path:
        return Hold(f"leadlock: {ENV} is not set")
    try:
        holds = load(path, datetime.now(timezone.utc))
    except Unusable as e:
        return Hold(f"leadlock: {e}; every email waits for a fresh file from the Orchestrator")
    verdict, reason = decide(holds, email)
    if verdict == "cancel":
        return Cancel(f"leadlock: {reason}")
    if verdict == "hold":
        return Hold(f"leadlock: {reason}")
    return None
