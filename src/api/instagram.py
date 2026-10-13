"""Instagram DM webhook.

The same bot as Messenger, reached through Instagram's inbox. Meta delivers Instagram DMs
through the same app as Messenger, signed with the same app secret and answered through the
same Send API call with the same page token; only the webhook object, the customer's id and
what a message can render differ. So this file only reads Instagram's webhook body and
sends to an IGSID. The queue, the turn, the typing indicator and the paced delivery are the
Messenger ones, used unchanged - see src/api/messenger.py for why each of them exists.

Two Instagram differences:

* **The customer is an IGSID, not a PSID.** It travels through Luna as ``"ig:<IGSID>"``
  (src/channel_identity.py), so an Instagram chat never lands in a Messenger session that
  happens to share the number. Meta is only ever sent the bare IGSID.
* **No cards.** A trailer goes as its title, its link and its bullets in plain bubbles,
  each inside Instagram's shorter text limit - src/domain/cards.channel_sends.
"""
from __future__ import annotations

import json
import logging
import threading

from fastapi import APIRouter, HTTPException, Request, Response

from src import channel_identity, inbound
from src.api import messenger
from src.config import settings
from src.domain import links

logger = logging.getLogger(__name__)

router = APIRouter()

CHANNEL = channel_identity.INSTAGRAM


def instagram_enabled() -> bool:
    """Off with INSTAGRAM_ENABLED=0, and never on when Messenger is not configured."""
    return bool(settings.instagram_enabled and messenger.messenger_enabled())


class InstagramTransport(messenger.MessengerTransport):
    """The Messenger Send API calls, addressed to the bare IGSID."""

    def send_text(self, recipient: str, text: str) -> bool:
        return super().send_text(channel_identity.raw_id(recipient), text)

    def send_card(self, recipient: str, element: dict) -> None:
        # channel_sends never renders a card for Instagram; this is the safety net if one
        # ever gets through - the trailer's title and link, as text.
        button = (element.get("buttons") or [{}])[0]
        text = "\n".join(part for part in (element.get("title"), button.get("url")) if part)
        self.send_text(recipient, text)

    def send_action(self, recipient: str, action: str) -> None:
        super().send_action(channel_identity.raw_id(recipient), action)


# ------------------------------------------------------------------- dispatch
def _enqueue(customer: str, text: str, mid: str, sent_at) -> None:
    """Record the message, then try to become this customer's drain worker."""
    if not messenger._claim_mid(mid):
        logger.info("INSTAGRAM duplicate suppressed in process: customer=%s mid=%s", customer, mid)
        return
    if not inbound.record_inbound_message(
        session_id=customer, external_id=mid, body=text, sent_at=sent_at, channel=CHANNEL
    ):
        logger.info("INSTAGRAM duplicate suppressed at the queue: customer=%s mid=%s", customer, mid)
        return
    threading.Thread(
        target=_drain, args=(customer,), daemon=True, name=f"instagram:{customer[:15]}"
    ).start()


def _drain(customer: str) -> None:
    """Answer this customer's queue, unless another instance already is."""
    try:
        while True:
            results = inbound.drain_inbound(customer, channel=CHANNEL, transport=InstagramTransport())
            if not results or not inbound.has_pending_inbound(customer, CHANNEL):
                return
    except Exception:  # noqa: BLE001 - a webhook thread has nobody to raise to
        logger.exception("INSTAGRAM drain failed: customer=%s", customer)


# --------------------------------------------------------------------- routes
@router.get("/webhooks/instagram")
def verify_webhook(request: Request) -> Response:
    """Meta's one-time subscription handshake: echo hub.challenge if the token matches."""
    if not instagram_enabled():
        raise HTTPException(status_code=404, detail="Not found")
    params = request.query_params
    if (params.get("hub.mode") == "subscribe"
            and params.get("hub.verify_token") == settings.messenger_verify_token):
        logger.info("INSTAGRAM webhook verified")
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    logger.warning("INSTAGRAM webhook verification rejected")
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/webhooks/instagram")
async def receive_webhook(request: Request) -> Response:
    """Acknowledge in milliseconds, answer in the background - as Messenger does."""
    if not instagram_enabled():
        raise HTTPException(status_code=404, detail="Not found")

    body = await request.body()
    if not messenger._signature_is_valid(body, request.headers.get("X-Hub-Signature-256")):
        logger.warning("INSTAGRAM webhook rejected: bad signature")
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        logger.warning("INSTAGRAM webhook rejected: the body was not JSON")
        return Response(status_code=200)

    for customer, text, mid, sent_at in extract_messages(payload):
        _enqueue(customer, text, mid, sent_at)
    return Response(status_code=200)


# ------------------------------------------------------- reading a webhook body
# A post or reel shared into the chat. Its payload url is all there is to go on, so it is
# passed on as an Instagram post the way a shared Facebook post is on Messenger.
_SHARE_TYPES = {"share", "ig_reel", "reel", "ig_post"}


def _shared_post_text(message: dict, text: str) -> str:
    lines: list[str] = []
    for attachment in message.get("attachments") or []:
        if (attachment.get("type") or "").lower() not in _SHARE_TYPES:
            continue  # their own photo, voice note, sticker or a story mention
        url = messenger._attachment_url(attachment)
        if url and url not in text:
            lines.append(f"Shared an Instagram post: {links.unwrap_redirect(url)}")
    return "\n".join(lines)


def extract_messages(payload: dict) -> list[tuple[str, str, str, object]]:
    """(customer, text, mid, sent_at) for everything in an Instagram webhook we can answer.

    ``customer`` is ``"ig:<IGSID>"``. Ignored: our own messages handed back (``is_echo``,
    or sent by our own account), deleted and unsupported messages, reactions, read receipts
    and attachment-only messages with nothing readable in them. A shared post becomes text,
    and a postback (an ice-breaker button) is answered like a message.
    """
    found: list[tuple[str, str, str, object]] = []
    if payload.get("object") != "instagram":
        return found
    for entry in payload.get("entry") or []:
        our_account = str(entry.get("id") or "")
        for event in entry.get("messaging") or []:
            igsid = str((event.get("sender") or {}).get("id") or "").strip()
            if not igsid or igsid == our_account:
                continue
            message = event.get("message") or {}
            if message.get("is_echo") or message.get("is_deleted") or message.get("is_unsupported"):
                continue
            customer = channel_identity.for_instagram(igsid)
            text = (message.get("text") or "").strip()
            mid = (message.get("mid") or "").strip()
            shared = _shared_post_text(message, text)
            if shared:
                text = f"{text}\n{shared}" if text else shared
            if not text:
                postback = event.get("postback") or {}
                text = (postback.get("payload") or postback.get("title") or "").strip()
                mid = mid or (postback.get("mid") or "").strip() or f"postback:{customer}:{event.get('timestamp')}"
            if not text or not mid:
                if message:
                    logger.info(
                        "INSTAGRAM ignored, nothing to answer: customer=%s attachments=%s",
                        customer, [a.get("type") for a in message.get("attachments") or []],
                    )
                continue
            found.append((customer, text, mid, messenger._sent_at(event)))
    return found
