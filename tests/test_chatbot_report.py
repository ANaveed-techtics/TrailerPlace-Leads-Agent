"""The chatbot leads report the accountability agent reads. No database: the SQL is thin,
and the rules live in build_report."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import chatbot_report
from src.api import admin_reports, admin_rules
from src.chatbot_report import EmailRow, LeadRow, build_report, description_from_body

TOKEN = {"X-Admin-Token": "s3cret"}


def body(desc, name="Gerald A. Brown", phone="(210) 387-3115", email="Unknown email"):
    return (f"Full Name: {name}\nEmail: {email}\nPhone Number: {phone}\n"
            f"[{desc[0]}] {desc[1]} | Chat: https://panel/?chat_session=abc")


def lead(sid, name=None, phone=None, email=None, lead_id=None):
    return LeadRow(sid, lead_id or sid, name, phone, email, "messenger")


def email(sid, desc, name="Gerald A. Brown", phone="(210) 387-3115", at=1, **lead_fields):
    return EmailRow(sid, sid, name, phone, lead_fields.get("email"), "messenger",
                    body=body(desc, name=name or "Unknown user", phone=phone or "Unknown phone number"),
                    created_at=datetime(2026, 10, 22, at, tzinfo=timezone.utc))


def test_the_description_is_the_line_under_the_phone_without_the_chat_link():
    assert description_from_body(body(("Results Shown to User", "Showed 6 Dump trailers"))) == \
        "[Results Shown to User] Showed 6 Dump trailers"


def test_an_older_body_with_a_blank_line_and_the_link_on_its_own_line():
    old = "Full Name: A\nEmail: b\nPhone Number: c\n\n[FAQ - financing] Asked about financing\n\nChat: https://x"
    assert description_from_body(old) == "[FAQ - financing] Asked about financing"


def test_emails_to_one_person_merge_into_one_hot_lead():
    report = build_report([], [
        email("s1", ("Results Shown to User", "Showed 6 Dump trailers"), at=1),
        email("s2", ("Team Request", "Wants a callback"), phone="210-387-3115", at=2),
    ])
    assert len(report["hot"]) == 1
    hot = report["hot"][0]
    assert hot.sessions == ["s1", "s2"]
    assert hot.descriptions == ["[Results Shown to User] Showed 6 Dump trailers", "[Team Request] Wants a callback"]


def test_a_contact_without_an_email_is_cold_and_one_with_an_email_is_not():
    report = build_report(
        [lead("s1", "Gerald", "(210) 387-3115"), lead("s2", "Rocky Davis", "(214) 277-4017"), lead("s3")],
        [email("s1", ("Results Shown to User", "Showed 6 Dump trailers"))],
    )
    assert [c.name for c in report["cold"]] == ["Rocky Davis"]
    assert report["conversations"] == 3


def test_an_email_alone_is_contact_enough_for_the_cold_list():
    report = build_report([lead("s1", email="max@example.com")], [])
    assert report["cold"][0].email == "max@example.com"


def test_a_missing_lead_row_falls_back_to_the_email_header_but_not_its_placeholders():
    row = EmailRow("s9", None, None, None, None, None,
                   body=body(("Team Request", "Callback"), name="Dennis W", phone="Unknown phone number"))
    hot = build_report([], [row])["hot"][0]
    assert hot.name == "Dennis W" and hot.phone is None and hot.email is None


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(admin_rules, "settings", replace(admin_rules.settings, admin_api_token="s3cret"))
    app = FastAPI()
    app.include_router(admin_reports.router)
    return TestClient(app)


def test_the_route_needs_the_admin_token(api):
    assert api.get("/admin/reports/chatbot-leads?since=2026-10-22T00:00:00Z&until=2026-10-23T00:00:00Z").status_code == 401


def test_the_route_refuses_a_backwards_window(api):
    r = api.get("/admin/reports/chatbot-leads?since=2026-10-23T00:00:00Z&until=2026-10-22T00:00:00Z", headers=TOKEN)
    assert r.status_code == 422


def test_the_route_returns_both_lists_with_chat_links(api, monkeypatch):
    seen = {}

    def fake(since, until):
        seen.update(since=since, until=until)
        return build_report([lead("s2", "Rocky", "(214) 277-4017")],
                            [email("s1", ("Results Shown to User", "Showed 6 Dump trailers"))])

    monkeypatch.setattr(chatbot_report, "fetch_report", fake)
    monkeypatch.setattr(admin_reports, "chat_session_url", lambda sid: f"https://panel/?chat_session={sid}")
    r = api.get("/admin/reports/chatbot-leads?since=2026-10-22T00:00:00&until=2026-10-23T00:00:00Z", headers=TOKEN)
    assert r.status_code == 200
    data = r.json()
    assert seen["since"].tzinfo is not None  # a naive time is read as UTC
    assert data["conversations"] == 1 and data["active_sessions"] == ["s2"]
    assert data["hot"][0]["sessions"] == [{"session_id": "s1", "chat_url": "https://panel/?chat_session=s1"}]
    assert data["cold"][0]["name"] == "Rocky"
