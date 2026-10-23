"""The chatbot leads report: who the bot emailed to Transax, and who left contact details
without being emailed, over one window.

The accountability agent builds its "chatbot" PDF sections from this (through
src/api/admin_reports.py), so the agentic system never reads the chatbot_* tables itself.

Two lists:

* **hot** - every lead the deployed bot emailed to the team in the window. A row queued by
  a local run (origin ``local-...``) never reached Transax for real, so it is left out; a
  row with no origin predates the column and is the deployed bot's.
* **cold** - a lead who talked to the bot in the window and gave a phone number or email,
  but was not emailed in it.

Activity means a customer turn. The follow-up job writes turns too ("(follow-up 1: ...)",
"(The customer has not replied ...)"); those are the bot talking, so they do not make a
conversation active. Leads are merged by phone number, then email, so one person chatting
on two sessions is one row.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

_HEADER_PREFIXES = ("Full Name:", "Email:", "Phone Number:", "Chat:")


@dataclass
class LeadRow:
    """One session's lead, as the queries return it."""

    session_id: str
    lead_id: str | None
    name: str | None
    phone: str | None
    email: str | None
    platform: str | None


@dataclass
class EmailRow(LeadRow):
    body: str = ""
    created_at: datetime | None = None


@dataclass
class ReportLead:
    name: str | None
    phone: str | None
    email: str | None
    platform: str | None
    sessions: list[str] = field(default_factory=list)
    descriptions: list[str] = field(default_factory=list)


def description_from_body(body: str) -> str:
    """The one-line description out of a team email body (email_sender.render_email_body).

    Older bodies put a blank line before it and the chat link on its own line, so this
    skips the header lines rather than counting them.
    """
    for line in (body or "").splitlines():
        line = line.strip()
        if not line or line.startswith(_HEADER_PREFIXES):
            continue
        return " ".join(line.split(" | Chat:")[0].split())
    return ""


_UNKNOWN = {"unknown user", "unknown email", "unknown phone number", "not provided"}


def _from_body(row: EmailRow) -> EmailRow:
    """Fill what the lead row lacks from the email's own header lines (no lead row means the
    conversation was deleted; the email still says who it was about)."""
    found = {}
    for line in (row.body or "").splitlines():
        label, _, value = line.partition(":")
        value = value.strip()
        if value and value.casefold() not in _UNKNOWN:
            found[label.strip()] = value
    row.name = row.name or found.get("Full Name")
    row.phone = row.phone or found.get("Phone Number")
    row.email = row.email or found.get("Email")
    return row


def _key(row: LeadRow) -> str:
    digits = re.sub(r"\D", "", row.phone or "")
    if len(digits) >= 7:
        return "phone:" + digits[-10:]
    if row.email and row.email.strip():
        return "email:" + row.email.strip().casefold()
    return "lead:" + (row.lead_id or row.session_id)


def _merge(into: dict[str, ReportLead], row: LeadRow, description: str | None = None) -> None:
    lead = into.get(_key(row))
    if lead is None:
        lead = into[_key(row)] = ReportLead(row.name, row.phone, row.email, row.platform)
    lead.name = lead.name or row.name
    lead.phone = lead.phone or row.phone
    lead.email = lead.email or row.email
    if row.session_id not in lead.sessions:
        lead.sessions.append(row.session_id)
    if description and description not in lead.descriptions:
        lead.descriptions.append(description)


def build_report(active: Iterable[LeadRow], emails: Iterable[EmailRow]) -> dict[str, Any]:
    """Hot and cold leads from the window's active sessions and team emails."""
    active = list(active)
    hot: dict[str, ReportLead] = {}
    for row in sorted(emails, key=lambda e: e.created_at or datetime.min):
        _merge(hot, _from_body(row), description_from_body(row.body))

    cold: dict[str, ReportLead] = {}
    for row in active:
        if not ((row.phone or "").strip() or (row.email or "").strip()):
            continue
        if _key(row) in hot:
            continue
        _merge(cold, row)

    sessions = sorted({row.session_id for row in active})
    return {
        "conversations": len(sessions),
        # So a caller that filters leads out (the team's test chats) can recount.
        "active_sessions": sessions,
        "hot": list(hot.values()),
        "cold": list(cold.values()),
    }


# ---------------------------------------------------------------- database

_ACTIVE_SQL = """
select distinct t.session_id::text as session_id, l.lead_id::text as lead_id, l.name,
       l.phone_number as phone, l.email, l.platform
from chatbot_turns t
join chatbot_conversations c on c.session_id = t.session_id
join chatbot_leads l on l.lead_id = c.lead_id
where t.created_at >= :since and t.created_at < :until
  and t.request_message not like '(follow-up%'
  and t.request_message not like '(The customer has not replied%'
"""

_EMAILS_SQL = """
select o.session_id::text as session_id, l.lead_id::text as lead_id, l.name,
       l.phone_number as phone, l.email, l.platform, o.payload->>'body' as body, o.created_at
from chatbot_outbox o
left join chatbot_conversations c on c.session_id = o.session_id
left join chatbot_leads l on l.lead_id = c.lead_id
where o.created_at >= :since and o.created_at < :until
  and o.status <> 'failed'
  and (o.origin is null or o.origin not like 'local-%')
"""


def fetch_report(since: datetime, until: datetime) -> dict[str, Any]:
    """Read the window from Postgres and build the report. Read-only."""
    from sqlalchemy import text

    from src import db

    window = {"since": since, "until": until}
    with db.get_engine().connect() as conn:
        active = [LeadRow(**r._mapping) for r in conn.execute(text(_ACTIVE_SQL), window)]
        emails = [EmailRow(**r._mapping) for r in conn.execute(text(_EMAILS_SQL), window)]
    return build_report(active, emails)
