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


async def _send(state, oid, to, when=None, raw=None):
    """An outbox row marked sent exactly as Sellbird's sender marks it (sellbird/agents/sender.py): through
    StateManager.update_outbox_item with sent_at = datetime.now(utc).replace(tzinfo=None).isoformat(). `raw`
    writes another stored form instead (an older row)."""
    import aiosqlite
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("INSERT INTO outbox (id, prospect_id, kind, step, to_email, subject, body, status) "
                         "VALUES (?, 'p1', 'sequence', 1, ?, 'secret subject', 'secret body', 'approved')", (oid, to))
        await db.commit()
    sent_at = raw if raw is not None else (when or datetime.now(timezone.utc)).replace(tzinfo=None).isoformat()
    await state.update_outbox_item(oid, status="sent", sent_at=sent_at, message_id=f"<{oid}@mail.example.com>")


def pages(client, after, limit):
    got, cursor = [], ""
    for _ in range(20):
        params = {"cursor": cursor, "limit": limit} if cursor else {"after": after, "limit": limit}
        page = client.get(status.SENT_PATH, params=params).json()["sent"]
        if not page:
            return got
        got += page
        cursor = page[-1]["cursor"]
    raise AssertionError("the pages never ended")


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


def test_sent_pages_through_sellbirds_real_format_without_repeating(client, state):
    """The reviewer's repro: six rows as the sender writes them (microseconds, a T), page size 2."""
    base = NOW.replace(hour=10, minute=0, second=0) - timedelta(days=1)
    for i in range(6):
        run(_send(state, f"o{i}", f"p{i}@example.com", base + timedelta(seconds=i, microseconds=123456)))
    got = pages(client, (base - timedelta(minutes=1)).isoformat(), 2)
    assert [r["id"] for r in got] == [f"o{i}" for i in range(6)]


def test_sent_orders_mixed_stored_forms_as_times(client, state):
    day = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
    run(_send(state, "o_space", "a@example.com", raw=f"{day} 10:00:01"))           # an older row
    run(_send(state, "o_whole", "b@example.com", raw=f"{day}T10:00:00"))           # isoformat at .000000
    run(_send(state, "o_micro", "c@example.com", raw=f"{day}T10:00:00.500000"))
    run(_send(state, "o_late", "d@example.com", raw=f"{day}T10:00:02.000001"))
    got = pages(client, f"{day}T09:00:00Z", 1)
    assert [r["id"] for r in got] == ["o_whole", "o_micro", "o_space", "o_late"]
    assert [r["sent_at"] for r in got] == [f"{day}T10:00:00.000Z", f"{day}T10:00:00.500Z", f"{day}T10:00:01.000Z",
                                           f"{day}T10:00:02.000Z"]


def test_sent_pages_past_more_rows_in_one_instant_than_a_page(client, state):
    day = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
    for i in range(5):
        run(_send(state, f"o{i}", f"p{i}@example.com", raw=f"{day}T10:00:00.250000"))
    assert [r["id"] for r in pages(client, f"{day}T09:00:00Z", 2)] == [f"o{i}" for i in range(5)]


def test_after_is_inclusive_and_a_cursor_is_strict(client, state):
    day = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
    run(_send(state, "o1", "a@example.com", raw=f"{day}T10:00:00"))
    first = client.get(status.SENT_PATH, params={"after": f"{day}T10:00:00Z"}).json()["sent"]
    assert [r["id"] for r in first] == ["o1"]
    assert client.get(status.SENT_PATH, params={"cursor": first[0]["cursor"]}).json()["sent"] == []


def test_sent_hands_out_a_hash_never_the_address_subject_or_body(client, state):
    run(_send(state, "o1", "Ann@Example.com", NOW - timedelta(minutes=5)))
    (row,) = client.get(status.SENT_PATH, params={"after": (NOW - timedelta(hours=1)).isoformat()}).json()["sent"]
    assert row["email_sha256"] == holds.email_hash("ann@example.com") and "to_email" not in row
    text = json.dumps(row).lower()
    assert "ann" not in text and "example.com>" in text and "secret" not in text  # the Message-ID is the mail host's


def test_a_row_without_an_address_keeps_its_place(client, state):
    day = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
    run(_send(state, "o1", "a@example.com", raw=f"{day}T10:00:00"))
    run(_send(state, "o2", "", raw=f"{day}T10:00:01"))
    run(_send(state, "o3", "c@example.com", raw=f"{day}T10:00:02"))
    got = pages(client, f"{day}T09:00:00Z", 2)
    assert [(r["id"], bool(r["email_sha256"])) for r in got] == [("o1", True), ("o2", False), ("o3", True)]


@pytest.mark.parametrize("params", [{}, {"after": "yesterday"}, {"after": "2026-01-01T00:00:00"},
                                    {"after": "2026-01-01T00:00:00Z", "limit": 0},
                                    {"after": "2026-01-01T00:00:00Z", "limit": 201},
                                    {"cursor": "nonsense"}, {"cursor": "2461362.5|"}, {"cursor": "nan|o1"}])
def test_a_bad_query_is_refused(client, params):
    assert client.get(status.SENT_PATH, params=params).status_code == 400
