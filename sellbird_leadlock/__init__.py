"""sellbird-leadlock: a Sellbird extension that connects Sellbird to the Leadlock CRM hub.

It adds one send rule (asked before every email) and one tick step (run each heartbeat). It talks to the
Leadlock CRM only over HTTPS and holds no business logic: the decisions live in the CRM and in the
Leadlock Orchestrator. Licensed AGPL-3.0-or-later, like Sellbird.
"""
__version__ = "0.1.0"
