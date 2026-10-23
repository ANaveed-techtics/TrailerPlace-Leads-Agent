"""The chatbot leads report, for the accountability agent (src/chatbot_report.py).

Same guard as the rules API: ``ADMIN_API_TOKEN`` sent as ``X-Admin-Token``, and main.py
mounts this only when that token is configured. Read-only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from src import chatbot_report
from src.api.admin_rules import _require_token
from src.tools.email_sender import chat_session_url

router = APIRouter(prefix="/admin/reports", tags=["admin"], dependencies=[Depends(_require_token)])

MAX_WINDOW = timedelta(days=366)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _lead(lead: chatbot_report.ReportLead) -> dict[str, Any]:
    return {
        "name": lead.name,
        "phone": lead.phone,
        "email": lead.email,
        "platform": lead.platform,
        "sessions": [{"session_id": sid, "chat_url": chat_session_url(sid)} for sid in lead.sessions],
        "descriptions": lead.descriptions,
    }


@router.get("/chatbot-leads")
def chatbot_leads(since: datetime = Query(...), until: datetime = Query(...)) -> dict[str, Any]:
    """Hot and cold chatbot leads for ``since <= t < until``. A naive time is read as UTC."""
    since, until = _aware(since), _aware(until)
    if until <= since:
        raise HTTPException(status_code=422, detail="until must be after since")
    if until - since > MAX_WINDOW:
        raise HTTPException(status_code=422, detail="the window is at most 366 days")
    report = chatbot_report.fetch_report(since, until)
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "conversations": report["conversations"],
        "active_sessions": report["active_sessions"],
        "hot": [_lead(lead) for lead in report["hot"]],
        "cold": [_lead(lead) for lead in report["cold"]],
    }
