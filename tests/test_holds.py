"""The leadlock.orchestrator send rule, run through Sellbird's own first_verdict (Sellbird 0.4.0). No network.

Every address here is a made-up example.com address; the hashes are computed from them in the test."""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sellbird.send_hooks import SendCheck, first_verdict
from sellbird_leadlock import holds, plugin

NOW = datetime.now(timezone.utc)
ANN, BOB, CAT = "ann@example.com", "bob@example.com", "cat@example.com"


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def doc(**over):
    base = {"version": 1, "generated_at": iso(NOW), "paused": False, "test_only": False,
            "allow_email_sha256": [], "cancel_email_sha256": [], "hold_email_sha256": [], "hold_domains": []}
    base.update(over)
    return base


@pytest.fixture
def holds_file(tmp_path, monkeypatch):
    path = tmp_path / "holds.json"
    monkeypatch.setenv(holds.ENV, str(path))

    def write(content):
        path.write_text(content if isinstance(content, str) else json.dumps(content))
        return path
    return write


def verdict(to=ANN):
    check = SendCheck(item={"to_email": to, "kind": "sequence"}, prospect=None,
                      config=SimpleNamespace(), state=None)
    return asyncio.run(first_verdict({"leadlock.orchestrator": holds.orchestrator}, check))


def status(to=ANN):
    """Sellbird's outcome: "held" (its status stays approved, the reason starts "held:"), "cancelled" or None."""
    v = verdict(to)
    if v is None:
        return None
    if v[0] == "approved" and v[1].startswith("held: leadlock.orchestrator: "):
        return "held"
    return v[0]


# no file we can trust: every email holds


def test_a_missing_file_holds(holds_file, tmp_path):
    assert status() == "held"


def test_the_variable_unset_holds(monkeypatch):
    monkeypatch.delenv(holds.ENV, raising=False)
    assert status() == "held" and holds.ENV in verdict()[1]


@pytest.mark.parametrize("content", [
    "not json", "[]", json.dumps(doc(version=2)), json.dumps(doc(generated_at="yesterday")),
    json.dumps(doc(paused="no")),
    json.dumps(doc(test_only=None)), json.dumps(doc(cancel_email_sha256="x")),
    json.dumps(doc(hold_email_sha256=["NOT-A-HASH"])), json.dumps(doc(hold_domains=["bad domain"])),
    json.dumps({k: v for k, v in doc().items() if k != "allow_email_sha256"}),
])
def test_a_malformed_file_holds(holds_file, content):
    holds_file(content)
    assert status() == "held"


def test_a_time_without_a_zone_holds(holds_file):
    holds_file(doc(generated_at=NOW.strftime("%Y-%m-%dT%H:%M:%S")))
    assert status() == "held" and "no time zone" in verdict()[1]


def test_a_stale_file_holds(holds_file):
    holds_file(doc(generated_at=iso(NOW - timedelta(hours=2, minutes=1))))
    assert status() == "held"
    holds_file(doc(generated_at=iso(NOW - timedelta(hours=1, minutes=50))))
    assert status() is None


def test_a_file_from_the_future_holds(holds_file):
    holds_file(doc(generated_at=iso(NOW + timedelta(hours=1))))
    assert status() == "held"


def test_a_file_too_big_holds(holds_file, monkeypatch):
    monkeypatch.setattr(holds, "MAX_BYTES", 10)
    holds_file(doc())
    assert status() == "held" and "too big" in verdict()[1]


def test_paused_holds_everyone(holds_file):
    holds_file(doc(paused=True, allow_email_sha256=[holds.email_hash(ANN)]))
    assert status() == "held"


# a good file


def test_a_clean_file_lets_sellbird_decide(holds_file):
    holds_file(doc())
    assert verdict() is None


def test_a_cancelled_address_is_cancelled_whatever_its_case(holds_file):
    holds_file(doc(cancel_email_sha256=[holds.email_hash(ANN)]))
    assert status(" Ann@Example.COM ") == "cancelled"
    assert status(BOB) is None


def test_a_held_address_waits(holds_file):
    holds_file(doc(hold_email_sha256=[holds.email_hash(BOB)]))
    assert status(BOB) == "held" and status(ANN) is None


def test_cancel_wins_over_allow_and_hold(holds_file):
    h = holds.email_hash(ANN)
    holds_file(doc(test_only=True, allow_email_sha256=[h], cancel_email_sha256=[h], hold_email_sha256=[h]))
    assert status() == "cancelled"


def test_test_only_lets_through_only_the_allowed(holds_file):
    holds_file(doc(test_only=True, allow_email_sha256=[holds.email_hash(CAT)]))
    assert status(CAT) is None and status(ANN) == "held"


def test_a_listed_domain_holds_it_and_its_subdomains_only(holds_file):
    holds_file(doc(hold_domains=["blocked.example.com"]))
    assert status("x@blocked.example.com") == "held"
    assert status("x@mail.blocked.example.com") == "held"
    assert status("x@notblocked.example.com") is None and status(ANN) is None


def test_no_recipient_holds(holds_file):
    holds_file(doc())
    assert status("") == "held" and status("not-an-address") == "held"


def test_the_reason_never_carries_the_address(holds_file):
    holds_file(doc(cancel_email_sha256=[holds.email_hash(ANN)]))
    assert "ann" not in verdict()[1].lower()


def test_the_rule_reads_only_its_file(holds_file, monkeypatch):
    """No network: the rule opens nothing but the hold file."""
    import socket
    holds_file(doc())

    def no(*a, **k):
        raise AssertionError("the rule tried the network")
    monkeypatch.setattr(socket, "create_connection", no)
    monkeypatch.setattr(socket, "getaddrinfo", no)
    monkeypatch.setattr(socket.socket, "connect", no)
    assert verdict() is None


# install


def test_install_adds_the_rule_only_when_the_file_is_set(monkeypatch, tmp_path):
    from sellbird.extensions import Registry
    monkeypatch.delenv(holds.ENV, raising=False)
    r = Registry()
    plugin.install(r)
    assert list(r.send_rules) == ["leadlock.crosschannel"]
    monkeypatch.setenv(holds.ENV, str(tmp_path / "holds.json"))
    r = Registry()
    plugin.install(r)
    assert list(r.send_rules) == ["leadlock.crosschannel", "leadlock.orchestrator"]
    assert list(r.tick_steps) == ["leadlock.events"]


def test_the_hash_is_sha256_of_the_stripped_lower_cased_address():
    """What the Orchestrator writes (its email_holds.email_hash)."""
    import hashlib
    assert holds.email_hash(" Ann@Example.com ") == hashlib.sha256(b"ann@example.com").hexdigest()
