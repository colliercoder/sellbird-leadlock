"""sellbird-leadlock on a real Sellbird (0.4.0) state, on a throwaway database. No network: the hub is faked.

Test data uses example.com addresses."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sellbird.send_hooks import Cancel, Hold, SendCheck
from sellbird.state import StateManager
from sellbird_leadlock import hub, plugin

NOW = datetime.now(timezone.utc)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def state(tmp_path):
    s = StateManager(str(tmp_path / "sellbird.db"))
    run(s.init_db())
    return s


async def _exec(state, sql, params=()):
    import aiosqlite
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(sql, params)
        await db.commit()


def ts(d):
    return d.strftime("%Y-%m-%d %H:%M:%S")


def seed(state):
    run(_exec(state, "INSERT INTO prospects (id, email, first_name) VALUES ('p1', 'ann@example.com', 'Ann')"))
    run(_exec(state, "INSERT INTO outbox (id, prospect_id, kind, step, to_email, subject, body, status, sent_at, "
                     "message_id) VALUES ('o1', 'p1', 'sequence', 1, 'Ann@Example.com', 'secret subject', "
                     "'secret body', 'sent', ?, '<m1@mail.example.com>')", (ts(NOW - timedelta(minutes=30)),)))
    thread = [{"sender": "sellbird", "content": "secret body", "timestamp": (NOW - timedelta(minutes=30)).isoformat()},
              {"sender": "prospect", "content": "secret reply words", "timestamp": (NOW - timedelta(minutes=10)).isoformat()}]
    run(_exec(state, "INSERT INTO conversations (id, prospect_id, thread_json, intent, updated_at) "
                     "VALUES ('c1', 'p1', ?, 'interested', ?)", (json.dumps(thread), ts(NOW - timedelta(minutes=10)))))


def config(approval=True):
    return SimpleNamespace(channels=SimpleNamespace(email=SimpleNamespace(require_approval=approval)))


def check(kind="sequence", to="ann@example.com"):
    return SendCheck(item={"to_email": to, "kind": kind}, prospect=None, config=config(), state=None)


# the send rule

@pytest.mark.parametrize("last,kind,expect", [
    ({"outcome": "opted_out", "desk": "orchestrator"}, "sequence", Cancel),
    ({"outcome": "booked", "desk": "orchestrator", "occurred_at": "2026-01-01T00:00:00Z"}, "sequence", Cancel),
    ({"outcome": "booked", "desk": "orchestrator", "occurred_at": "2026-01-01T00:00:00Z"}, "reply", type(None)),
    ({"outcome": "pending", "desk": "orchestrator", "occurred_at": (NOW - timedelta(hours=2)).isoformat()}, "sequence", Hold),
    ({"outcome": "pending", "desk": "orchestrator", "occurred_at": (NOW - timedelta(days=2)).isoformat()}, "sequence", type(None)),
    ({"outcome": "pending", "desk": "sellbird", "occurred_at": (NOW - timedelta(hours=2)).isoformat()}, "sequence", type(None)),
    ({"outcome": "pending", "desk": "unipile"}, "sequence", Hold),  # no time: treated as recent
    (None, "sequence", type(None)),
])
def test_the_send_rule(monkeypatch, last, kind, expect):
    monkeypatch.setattr(hub, "last_touch", lambda email: last)
    assert isinstance(run(plugin.crosschannel(check(kind))), expect)


def test_the_send_rule_holds_when_the_crm_cannot_be_asked(monkeypatch):
    def boom(email):
        raise hub.HubError("leadlock: GET /last-touch could not reach the Leadlock CRM")
    monkeypatch.setattr(hub, "last_touch", boom)
    v = run(plugin.crosschannel(check()))
    assert isinstance(v, Hold) and "could not reach" in v.reason


def test_the_send_rule_holds_without_settings(monkeypatch):
    for k in ("LEADLOCK_HUB_URL", "LEADLOCK_HUB_WORKSPACE", "LEADLOCK_CRM_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert isinstance(run(plugin.crosschannel(check())), Hold)


def test_the_send_rule_holds_an_email_with_no_recipient(monkeypatch):
    monkeypatch.setattr(hub, "last_touch", lambda email: None)
    assert isinstance(run(plugin.crosschannel(check(to=""))), Hold)


# the tick step

def test_sent_emails_replies_and_opt_outs_become_touches(state):
    seed(state)
    run(_exec(state, "INSERT INTO suppressions (id, kind, value, reason, created_at) VALUES "
                     "('s1', 'address', 'ann@example.com', 'opted_out', ?)", (ts(NOW - timedelta(minutes=5)),)))
    touches, cursor = run(plugin.collect(state, config(approval=True), {}, NOW))
    sent = [t for t in touches if t["direction"] == "outbound" and "outcome" not in t]
    assert sent == [{"desk": "sellbird", "channel": "email", "direction": "outbound", "kind": "sent",
                     "occurred_at": sent[0]["occurred_at"], "external_id": "<m1@mail.example.com>",
                     "contact": {"email": "ann@example.com"}, "summary": "email sequence step 1",
                     "is_test": False, "autonomous": False}]
    (reply,) = [t for t in touches if t["direction"] == "inbound"]
    assert reply["reply_category"] == "interested" and reply["contact"] == {"email": "ann@example.com"}
    (opt,) = [t for t in touches if t.get("outcome")]
    assert opt["outcome"] == "opted_out" and opt["external_id"] == "<m1@mail.example.com>"
    text = json.dumps(touches)
    assert "secret" not in text  # no subject, body or reply words ever leave Sellbird
    assert set(cursor) == {"sent", "reply", "supp"}


def test_autopilot_marks_the_touch_autonomous(state):
    seed(state)
    touches, _ = run(plugin.collect(state, config(approval=False), {}, NOW))
    assert [t["autonomous"] for t in touches if t["direction"] == "outbound"] == [True]


def test_every_touch_fits_the_hubs_shape(state):
    seed(state)
    keys = {"channel", "desk", "direction", "kind", "occurred_at", "external_id", "summary", "contact", "outcome",
            "reply_category", "is_test", "autonomous"}
    touches, _ = run(plugin.collect(state, config(), {}, NOW))
    assert touches and all(set(t) <= keys and set(t["contact"]) == {"email"} for t in touches)


def test_the_tick_saves_the_cursor_only_after_the_hub_stored_everything(state, monkeypatch):
    seed(state)
    posted = []
    monkeypatch.setattr(hub, "post_touches", lambda ts_: posted.append(ts_) or {"inserted": len(ts_)})
    run(plugin.push_events(SimpleNamespace(state=state, config=config())))
    assert posted and json.loads(run(state.get_setting(plugin.CURSOR_KEY)))["sent"]


@pytest.mark.parametrize("err", [hub.HubBusy("busy"), hub.HubError("down")])
def test_a_busy_or_down_hub_keeps_the_cursor(state, monkeypatch, err):
    seed(state)
    def boom(ts_):
        raise err
    monkeypatch.setattr(hub, "post_touches", boom)
    run(plugin.push_events(SimpleNamespace(state=state, config=config())))
    assert run(state.get_setting(plugin.CURSOR_KEY, "")) == ""


def test_the_next_read_overlaps_and_resends_harmlessly(state):
    seed(state)
    _, cursor = run(plugin.collect(state, config(), {}, NOW))
    again, _ = run(plugin.collect(state, config(), cursor, NOW))
    assert any(t["external_id"] == "<m1@mail.example.com>" for t in again)  # resent; the hub drops duplicates


def test_install_registers_one_rule_and_one_step(monkeypatch):
    from sellbird.extensions import Registry
    monkeypatch.delenv("SELLBIRD_LEADLOCK_HOLDS", raising=False)
    r = Registry()
    plugin.install(r)
    assert list(r.send_rules) == ["leadlock.crosschannel"] and list(r.tick_steps) == ["leadlock.events"]


# the hub client

ENV = {"LEADLOCK_HUB_URL": "https://crm.example.com", "LEADLOCK_HUB_WORKSPACE": "leadlock",
       "LEADLOCK_CRM_TOKEN": "tok-fake-12345678"}


def test_the_client_sends_the_token_to_the_workspace():
    seen = []
    def opener(req, timeout):
        seen.append((req.get_method(), req.full_url, dict(req.header_items())))
        return 200, b'{"last_touch": null}'
    assert hub.last_touch("ann@example.com", env=ENV, opener=opener) is None
    method, url, headers = seen[0]
    assert url == "https://crm.example.com/api/agency/workspaces/leadlock/last-touch?contact=ann%40example.com"
    assert headers["Authorization"] == "Bearer tok-fake-12345678"


@pytest.mark.parametrize("env", [{**ENV, "LEADLOCK_HUB_URL": "http://crm.example.com"},
                                 {**ENV, "LEADLOCK_HUB_URL": "https://crm.example.com/x"},
                                 {**ENV, "LEADLOCK_HUB_WORKSPACE": "Bad Slug"},
                                 {**ENV, "LEADLOCK_CRM_TOKEN": ""}])
def test_bad_settings_are_refused_before_any_request(env):
    with pytest.raises(hub.HubError):
        hub.last_touch("a@example.com", env=env, opener=lambda *a: pytest.fail("no request"))


def test_503_is_busy_and_errors_never_carry_the_token():
    with pytest.raises(hub.HubBusy):
        hub.post_touches([{}], env=ENV, opener=lambda req, t: (503, b""))
    with pytest.raises(hub.HubError) as e:
        hub.post_touches([{}], env=ENV, opener=lambda req, t: (401, b"tok-fake-12345678"))
    assert "tok-fake" not in str(e.value)
