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

## Install

Into Sellbird's own Python environment:

```
pip install git+https://github.com/colliercoder/sellbird-leadlock@v0.1.0
```

Then set three environment variables where Sellbird runs. The token is a secret: keep it in your secret store,
never in a file you commit.

```
LEADLOCK_HUB_URL=https://<your Leadlock CRM host>
LEADLOCK_HUB_WORKSPACE=<your workspace slug>
LEADLOCK_CRM_TOKEN=<your workspace's agency token>
```

Until all three are set, the send rule holds every email (fail closed) and the tick step sends nothing.

## Tests

```
pip install pytest
python -m pytest -q
```

The tests run against an installed Sellbird (0.4.0) on a throwaway database, with no network.

## License

AGPL-3.0-or-later. See `LICENSE`. If you run Sellbird with this extension for other people over a network,
offer them the source of both.
