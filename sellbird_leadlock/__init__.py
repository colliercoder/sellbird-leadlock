"""sellbird-leadlock: a Sellbird extension that connects Sellbird to the Leadlock CRM hub.

It adds a send rule (asked before every email), a second one that reads the Leadlock Orchestrator's hold file
when SELLBIRD_LEADLOCK_HOLDS is set, and one tick step (run each heartbeat). It talks to the
Leadlock CRM only over HTTPS and holds no business logic: the decisions live in the CRM and in the
Leadlock Orchestrator. Licensed AGPL-3.0-or-later, like Sellbird.
"""
__version__ = "0.2.0"
