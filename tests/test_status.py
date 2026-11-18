"""The beacon and the two routes the Orchestrator reads with Sellbird's agent token. Throwaway database, no
network; example.com addresses only."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sellbird.state import StateManager
from sellbird_leadlock import holds, status

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def state(tmp_path, monkeypatch):
    s = StateManager(str(tmp_path / "sellbird.db"))
    run(s.init_db())

    async def query(sql, params=()):
        import aiosqlite
        async with aiosqlite.connect(s.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                return [dict(r) for r in await cur.fetchall()]
    monkeypatch.setattr(status, "_query", query)
    return s


@pytest.fixture
def client(state):
    app = FastAPI()
    app.include_router(status.router())
    return TestClient(app)


def ts(d):
    return d.strftime("%Y-%m-%d %H:%M:%S")


async def _send(state, oid, to, when):
    import aiosqlite
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("INSERT INTO outbox (id, prospect_id, kind, step, to_email, subject, body, status, sent_at, "
                         "message_id) VALUES (?, 'p1', 'sequence', 1, ?, 'secret subject', 'secret body', 'sent', ?, ?)",
                         (oid, to, ts(when), f"<{oid}@mail.example.com>"))
        await db.commit()


def test_no_heartbeat_yet_is_null(client):
    assert client.get(status.STATUS_PATH).json()["rule"] is None


def test_the_beacon_records_the_rule_and_the_files_state(client, state, tmp_path, monkeypatch):
    path = tmp_path / "holds.json"
    monkeypatch.setenv(holds.ENV, str(path))
    run(status.beacon(SimpleNamespace(state=state)))
    rule = client.get(status.STATUS_PATH).json()["rule"]
    assert rule["file"] == "the hold file cannot be read" and rule["version"] == "0.2.0"
    assert abs(datetime.fromisoformat(rule["active_at"].replace("Z", "+00:00")) - NOW) < timedelta(minutes=1)
    path.write_text(json.dumps({"version": 1, "generated_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "paused": False,
                                "test_only": False, "allow_email_sha256": [], "cancel_email_sha256": [],
                                "hold_email_sha256": [], "hold_domains": []}))
    run(status.beacon(SimpleNamespace(state=state)))
    assert client.get(status.STATUS_PATH).json()["rule"]["file"] == "ok"


def test_the_beacon_says_when_the_path_is_unset(client, state, monkeypatch):
    monkeypatch.delenv(holds.ENV, raising=False)
    run(status.beacon(SimpleNamespace(state=state)))
    assert holds.ENV in client.get(status.STATUS_PATH).json()["rule"]["file"]


def test_sent_pages_oldest_first_past_any_number_in_one_second(client, state):
    t = NOW - timedelta(hours=1)
    for i in range(5):
        run(_send(state, f"o{i}", f"p{i}@example.com", t))  # five in the same second
    run(_send(state, "o9", "late@example.com", t + timedelta(minutes=5)))
    got, after, after_id = [], (t - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), ""
    for _ in range(10):
        page = client.get(status.SENT_PATH, params={"after": after, "after_id": after_id, "limit": 2}).json()["sent"]
        if not page:
            break
        got += [r["id"] for r in page]
        after, after_id = page[-1]["sent_at"], page[-1]["id"]
    assert got == ["o0", "o1", "o2", "o3", "o4", "o9"]


def test_sent_carries_no_subject_or_body(client, state):
    run(_send(state, "o1", "ann@example.com", NOW - timedelta(minutes=5)))
    rows = client.get(status.SENT_PATH, params={"after": (NOW - timedelta(hours=1)).isoformat()}).json()["sent"]
    assert rows == [{"id": "o1", "message_id": "<o1@mail.example.com>", "to_email": "ann@example.com",
                     "prospect_id": "p1", "campaign_id": "", "step": 1,
                     "sent_at": (NOW - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")}]
    assert "secret" not in json.dumps(rows)


@pytest.mark.parametrize("params", [{}, {"after": "yesterday"}, {"after": "2026-01-01T00:00:00"},
                                    {"after": "2026-01-01T00:00:00Z", "limit": 0},
                                    {"after": "2026-01-01T00:00:00Z", "limit": 201}])
def test_a_bad_query_is_refused(client, params):
    assert client.get(status.SENT_PATH, params=params).status_code == 400
