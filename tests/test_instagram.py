"""The Instagram DM webhook: the same bot as Messenger, reached through Instagram's inbox.

What differs from Messenger is all at the edge - the webhook body, the IGSID, no cards and a
shorter text limit - so that is what is tested here, plus that an Instagram customer never
shares a session or a lead with a Messenger one.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src import channel_identity, config, conversation_store, inbound
from src.api import instagram, messenger
from src.domain.cards import INSTAGRAM_TEXT_LIMIT, channel_sends

SECRET = "app-secret"
IGSID = "17841400000000001"
OUR_ACCOUNT = "17841499999999999"
CUSTOMER = f"ig:{IGSID}"
GALYEAN = "https://www.trailerplace.com/inventory/2026-galyean-32-cattle-trailer-015087/"


def _configure(monkeypatch, **overrides):
    values = dict(
        messenger_enabled=True,
        messenger_app_secret=SECRET,
        messenger_page_access_token="page-token",
        messenger_verify_token="verify-me",
        instagram_enabled=True,
    )
    settings = replace(config.settings, **{**values, **overrides})
    monkeypatch.setattr(config, "settings", settings)
    monkeypatch.setattr(messenger, "settings", settings)
    monkeypatch.setattr(instagram, "settings", settings)


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    _configure(monkeypatch)
    messenger.reset_seen_mids()
    yield
    messenger.reset_seen_mids()


@pytest.fixture
def client():
    import main

    return TestClient(main.app)


@pytest.fixture(autouse=True)
def no_threads(monkeypatch):
    drained = []
    # The module's own name only: patching threading.Thread itself would stop the typing
    # indicator's thread in the tests that run a whole exchange.
    fake = lambda **kwargs: type("T", (), {"start": lambda _self: drained.append(kwargs["args"][0])})()  # noqa: E731
    monkeypatch.setattr(instagram, "threading", SimpleNamespace(Thread=fake))
    return drained


def signed(payload: dict) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}


def dm(text: str = "got any dump trailers?", mid: str = "ig_m_1", sender: str = IGSID, **message) -> dict:
    return {
        "object": "instagram",
        "entry": [{"id": OUR_ACCOUNT, "time": 1758297600000, "messaging": [{
            "sender": {"id": sender},
            "recipient": {"id": OUR_ACCOUNT},
            "timestamp": 1758297600000,
            "message": {"mid": mid, "text": text, **message},
        }]}],
    }


# ------------------------------------------------------------------ the routes
def test_the_subscription_handshake_echoes_the_challenge(client):
    response = client.get("/webhooks/instagram", params={
        "hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42",
    })
    assert response.status_code == 200 and response.text == "42"


def test_a_wrong_verify_token_is_refused(client):
    response = client.get("/webhooks/instagram", params={
        "hub.mode": "subscribe", "hub.verify_token": "guess", "hub.challenge": "42",
    })
    assert response.status_code == 403


@pytest.mark.parametrize("overrides", [{"instagram_enabled": False}, {"messenger_enabled": False}])
def test_the_routes_are_invisible_when_switched_off(client, monkeypatch, overrides):
    _configure(monkeypatch, **overrides)
    assert client.get("/webhooks/instagram").status_code == 404
    body, headers = signed(dm())
    assert client.post("/webhooks/instagram", content=body, headers=headers).status_code == 404


def test_a_forged_signature_is_refused(client):
    body, _headers = signed(dm())
    response = client.post("/webhooks/instagram", content=body,
                           headers={"X-Hub-Signature-256": "sha256=" + "0" * 64})
    assert response.status_code == 403
    assert inbound.pending_inbound_batch(CUSTOMER, "instagram") == []


def test_a_dm_is_queued_as_an_instagram_customer(client, no_threads):
    body, headers = signed(dm())
    assert client.post("/webhooks/instagram", content=body, headers=headers).status_code == 200

    assert [row["body"] for row in inbound.pending_inbound_batch(CUSTOMER, "instagram")] == [
        "got any dump trailers?"
    ]
    assert inbound.pending_inbound_batch(IGSID, "messenger") == [], "never on the Messenger queue"
    assert no_threads == [CUSTOMER]


def test_a_retry_is_not_queued_twice(client):
    body, headers = signed(dm())
    client.post("/webhooks/instagram", content=body, headers=headers)
    messenger.reset_seen_mids()  # as if the retry reached another instance
    client.post("/webhooks/instagram", content=body, headers=headers)
    assert len(inbound.pending_inbound_batch(CUSTOMER, "instagram")) == 1


def test_a_messenger_body_is_not_read_as_instagram():
    assert instagram.extract_messages({**dm(), "object": "page"}) == []


# ----------------------------------------------------------- reading the body
@pytest.mark.parametrize("message", [
    {"is_echo": True},
    {"is_deleted": True},
    {"is_unsupported": True},
])
def test_what_is_not_the_customer_talking_is_ignored(message):
    assert instagram.extract_messages(dm(**message)) == []


def test_a_message_from_our_own_account_is_ignored():
    assert instagram.extract_messages(dm(sender=OUR_ACCOUNT)) == []


def test_reactions_and_read_receipts_are_ignored():
    payload = {"object": "instagram", "entry": [{"id": OUR_ACCOUNT, "messaging": [
        {"sender": {"id": IGSID}, "timestamp": 1, "reaction": {"mid": "x", "action": "react", "reaction": "love"}},
        {"sender": {"id": IGSID}, "timestamp": 2, "read": {"mid": "x"}},
    ]}]}
    assert instagram.extract_messages(payload) == []


def test_the_customer_is_their_prefixed_igsid():
    [(customer, text, mid, sent_at)] = instagram.extract_messages(dm())
    assert customer == CUSTOMER and text == "got any dump trailers?" and mid == "ig_m_1"
    assert sent_at == datetime(2025, 9, 19, 16, 0, tzinfo=timezone.utc)


def test_a_shared_reel_becomes_text_luna_can_read():
    payload = dm(text="", attachments=[{"type": "ig_reel", "payload": {"url": GALYEAN, "title": "Galyean"}}])
    [(_c, text, _m, _s)] = instagram.extract_messages(payload)
    assert text == f"Shared an Instagram post: {GALYEAN}"


def test_their_own_photo_or_a_story_mention_alone_is_ignored():
    for kind in ("image", "story_mention"):
        payload = dm(text="", attachments=[{"type": kind, "payload": {"url": "https://lookaside.fbsbx.com/x"}}])
        assert instagram.extract_messages(payload) == []


def test_an_ice_breaker_is_answered():
    payload = {"object": "instagram", "entry": [{"id": OUR_ACCOUNT, "messaging": [{
        "sender": {"id": IGSID}, "timestamp": 1758297600000,
        "postback": {"mid": "pb_1", "title": "What trailers do you have?", "payload": "What trailers do you have?"},
    }]}]}
    [(customer, text, mid, _s)] = instagram.extract_messages(payload)
    assert (customer, text, mid) == (CUSTOMER, "What trailers do you have?", "pb_1")


# --------------------------------------------------------------------- sending
@pytest.fixture
def posted(monkeypatch):
    calls = []

    class Response:
        status_code = 200
        text = ""

    monkeypatch.setattr(messenger.requests, "post",
                        lambda url, **kwargs: calls.append((url, kwargs)) or Response())
    return calls


def test_meta_is_sent_the_bare_igsid(posted):
    instagram.InstagramTransport().send_text(CUSTOMER, "Here are two that fit:")
    instagram.InstagramTransport().send_action(CUSTOMER, "typing_on")
    assert posted[0][1]["json"]["recipient"] == {"id": IGSID}
    assert posted[1][1]["json"] == {"recipient": {"id": IGSID}, "sender_action": "typing_on"}
    assert posted[0][1]["params"] == {"access_token": "page-token"}


def test_a_card_that_slips_through_goes_as_its_title_and_link(posted):
    element = {"title": "2026 Galyean 32'", "buttons": [{"type": "web_url", "url": GALYEAN, "title": "View Trailer"}]}
    instagram.InstagramTransport().send_card(CUSTOMER, element)
    assert posted[0][1]["json"]["message"] == {"text": f"2026 Galyean 32'\n{GALYEAN}"}


REPLY = f"""Here's one that fits:

1. [2026 Galyean 32' Cattle Trailer]({GALYEAN})
   - Price: $41,995
   - Built for big herds.

Want me to check financing?"""
LISTING = {"url": GALYEAN, "title": "2026 Galyean 32' Cattle Trailer", "price": "$41,995",
           "image_url": "https://img.example/1.jpg"}


def test_instagram_gets_plain_bubbles_where_messenger_gets_a_card():
    assert any(kind == "card" for kind, _ in channel_sends("messenger", REPLY, [LISTING]))
    sends = channel_sends("instagram", REPLY, [LISTING])
    assert all(kind == "text" for kind, _ in sends)
    assert GALYEAN in [payload for _, payload in sends], "the trailer's link is its own bubble"


def test_every_instagram_bubble_fits_its_limit():
    long_reply = "\n".join(f"Line {n}: " + "x" * 80 for n in range(40))
    sends = channel_sends("instagram", long_reply, [])
    assert len(sends) > 1 and all(len(payload) <= INSTAGRAM_TEXT_LIMIT for _, payload in sends)


# --------------------------------------------------------------- one customer, one platform
def test_an_instagram_customer_never_shares_a_session_with_a_messenger_one():
    assert conversation_store.session_uuid_for(CUSTOMER) != conversation_store.session_uuid_for(IGSID)


def test_the_lead_stores_the_bare_igsid_and_its_platform(fake_llm):
    from src.graph.build import run_turn
    from tests.factories import turn_output

    session = conversation_store.session_uuid_for(CUSTOMER)
    fake_llm.push(turn_output())
    run_turn(session, "hi", channel_id=CUSTOMER)

    lead = conversation_store.load_lead(session)
    assert (lead["psid"], lead["platform"]) == (IGSID, "instagram")


def test_a_messenger_lead_is_still_a_messenger_one(fake_llm):
    from src.graph.build import run_turn
    from tests.factories import turn_output

    session = conversation_store.session_uuid_for("8675309")
    fake_llm.push(turn_output())
    run_turn(session, "hi", channel_id="8675309")
    lead = conversation_store.load_lead(session)
    assert (lead["psid"], lead["platform"]) == ("8675309", "messenger")


def test_identity_helpers_round_trip():
    assert channel_identity.platform_of(CUSTOMER) == "instagram"
    assert channel_identity.platform_of("8675309") == "messenger"
    assert channel_identity.raw_id(CUSTOMER) == IGSID
    assert channel_identity.raw_id("8675309") == "8675309"


# ------------------------------------------------------------------- a whole exchange
class Recorder:
    def __init__(self):
        self.sent = []

    def send_text(self, recipient, text):
        self.sent.append(("text", recipient, text))

    def send_card(self, recipient, element):
        self.sent.append(("card", recipient, element))

    def send_action(self, recipient, action):
        pass


def test_a_queued_dm_is_answered_on_instagram():
    inbound.record_inbound_message(session_id=CUSTOMER, external_id="ig_m_9", body="cattle trailer?",
                                   sent_at=datetime.now(timezone.utc), channel="instagram")
    seen = {}

    def answer(session, message, **kwargs):
        seen.update(session=session, message=message, channel_id=kwargs["channel_id"])
        return {"assistant_text": REPLY, "listings": [LISTING]}

    transport = Recorder()
    [result] = inbound.drain_inbound(CUSTOMER, channel="instagram", answer=answer, transport=transport)

    assert seen == {"session": conversation_store.session_uuid_for(CUSTOMER),
                    "message": "cattle trailer?", "channel_id": CUSTOMER}
    assert result["delivered"] and all(kind == "text" for kind, _, _ in transport.sent)
    assert {recipient for _, recipient, _ in transport.sent} == {CUSTOMER}


def test_the_idle_timer_pushes_to_instagram(fake_llm, monkeypatch):
    from src import idle_sweep
    from src.graph.build import run_turn
    from src.graph.nodes import compose
    from src.llm import client as llm_client
    from tests.factories import turn_output

    monkeypatch.setattr(config, "settings", replace(config.settings, llm_writes_reply=True, idle_results_minutes=5))
    monkeypatch.setattr(compose, "settings", config.settings)
    monkeypatch.setattr(llm_client, "rewrite_reply", lambda *args: None)

    session = conversation_store.session_uuid_for(CUSTOMER)
    fake_llm.push(turn_output(intent="category_selection", category_mentioned="Dump"))
    run_turn(session, "I need a dump trailer", channel_id=CUSTOMER)
    assert conversation_store.armed_timer(session)["channel"] == "instagram"

    transport = Recorder()
    results = idle_sweep.sweep(now=datetime.now(timezone.utc) + timedelta(minutes=6), transport=transport)
    assert results[0]["status"] == "fired" and results[0]["delivered"]
    assert {recipient for _, recipient, _ in transport.sent} == {CUSTOMER}
    assert all(kind == "text" for kind, _, _ in transport.sent), "no cards on Instagram"
