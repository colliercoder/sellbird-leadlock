# sellbird-leadlock

A small [Sellbird](https://github.com/ethanplusai/sellbird) extension that connects Sellbird to the Leadlock CRM,
so cold email sits on the same timeline as calls, texts and LinkedIn, and one person is never worked by two
channels at once.

It is AGPL-3.0-or-later, like Sellbird, and uses only Sellbird's public extension API (`docs/extensions.md`).
Sellbird runs unmodified; nothing here patches it. It holds no business logic: the decisions live in the
Leadlock CRM, which it asks over HTTPS.

## What it adds

**A send rule, `leadlock.crosschannel`**, asked before every email after Sellbird's own checks. Like every
Sellbird send rule it can only stop an email, never let one through, and it holds the email when anything goes
wrong:

| The Leadlock CRM's newest touch for the recipient | The email |
|---|---|
| opted out | cancelled |
| booked or paid (cold emails only) | cancelled |
| on another channel in the last 24 hours | held, tried again next cycle |
| none, or an earlier email | goes |
| the CRM cannot be asked, or the settings are missing | held |

**A tick step, `leadlock.events`**, run each heartbeat. It posts what Sellbird did to the CRM's hub as touches
(desk `sellbird`):

- every sent email, keyed by its Message-ID;
- every reply, with a category taken from Sellbird's reply intent, never the words;
- every opt-out and bounce, as the outcome of that address's newest email.

It reads Sellbird's outbox, conversations, prospects and suppressions tables read-only, the way Sellbird's own
dashboard does. Each read starts an hour before the last one, and the hub drops anything it already has. If the
CRM is busy or down, nothing is lost: the next heartbeat sends it again.

**A second send rule, `leadlock.orchestrator`** (since 0.2.0), only where `SELLBIRD_LEADLOCK_HOLDS` or
`SELLBIRD_LEADLOCK_HOLDS_REQUIRED=1` is set. The
Leadlock Orchestrator writes a small hold file on the same machine each run; this rule reads that file before
every email, with no network:

| In the hold file | The email |
|---|---|
| the file is missing, unreadable, malformed, or more than 2 hours old | held |
| `paused` | held |
| the recipient's hash in `cancel_email_sha256` | cancelled |
| `test_only`, and the recipient's hash not in `allow_email_sha256` | held |
| the recipient's hash in `hold_email_sha256` | held, tried again next cycle |
| the recipient's domain, or a domain above it, in `hold_domains` | held |
| none of these | goes on to Sellbird's own checks |

A hash is the sha256 hex of the address, stripped and lower-cased, with its domain's trailing dot removed and an
international domain in punycode (`xn--`). A plus-alias (`ann+news@`) is a different address. Listed domains are
compared the same way. The file looks like this (made-up values):

```json
{"version": 1, "generated_at": "2026-01-01T12:00:00Z", "paused": false, "test_only": false,
 "allow_email_sha256": [], "cancel_email_sha256": ["<64 hex characters>"], "hold_email_sha256": [],
 "hold_domains": ["example.com"]}
```

A hash of an email address can be reversed by guessing common addresses, so treat the file as private: readable
only by the user Sellbird runs as, never committed or copied anywhere else.

With the rule come two things the Orchestrator reads back with Sellbird's agent token (GET only), so it notices
when the rule is not running:

- a tick step, `leadlock.orchestrator`, that records each heartbeat that the rule is registered and whether the
  file loads (a Sellbird setting; no address or hash), shown at `GET /api/leadlock/orchestrator`;
- `GET /api/leadlock/sent?after=<ISO time>&after_id=<outbox id>&limit=<1-200>`, sent emails oldest first, so the
  Orchestrator can log every one, not only the dashboard's newest 25. No subject or body.

## Install

Into Sellbird's own Python environment:

```
pip install git+https://github.com/colliercoder/sellbird-leadlock@v0.2.0
```

Then set three environment variables where Sellbird runs. The token is a secret: keep it in your secret store,
never in a file you commit.

```
LEADLOCK_HUB_URL=https://<your Leadlock CRM host>
LEADLOCK_HUB_WORKSPACE=<your workspace slug>
LEADLOCK_CRM_TOKEN=<your workspace's agency token>
```

Until all three are set, the send rule holds every email (fail closed) and the tick step sends nothing.

To use the Orchestrator's hold file as well, set its full path, and on an Orchestrator install also the
required switch, so that a restart that loses the path holds every email instead of dropping the rule:

```
SELLBIRD_LEADLOCK_HOLDS=/path/to/the/orchestrator/hold-file.json
SELLBIRD_LEADLOCK_HOLDS_REQUIRED=1
```

## Tests

```
pip install pytest
python -m pytest -q
```

The tests run against an installed Sellbird (0.4.0) on a throwaway database, with no network.

## License

AGPL-3.0-or-later. See `LICENSE`. If you run Sellbird with this extension for other people over a network,
offer them the source of both.
