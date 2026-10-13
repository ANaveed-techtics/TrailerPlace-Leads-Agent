"""Who a push-channel customer is, and on which platform.

A Messenger customer is known by their PSID, an Instagram one by their IGSID. Both are bare
numbers scoped to our Page or our Instagram account, and nothing stops the two from being
equal - so an Instagram customer travels through Luna as ``"ig:<IGSID>"``. That one string
is the inbound queue's session, the idle timer's channel_id and what ``session_uuid_for``
hashes, which keeps an Instagram chat from ever landing in a Messenger one. A PSID is used
bare, exactly as before, so every existing Messenger conversation keeps its session.

The prefix stops at the edges: Meta is sent the bare IGSID, and chatbot_leads stores it
bare in ``psid`` with ``platform`` saying which platform the number belongs to.
"""
from __future__ import annotations

MESSENGER = "messenger"
INSTAGRAM = "instagram"

_INSTAGRAM_PREFIX = "ig:"


def for_instagram(igsid: str) -> str:
    """The identity an Instagram customer is known by inside Luna."""
    return f"{_INSTAGRAM_PREFIX}{igsid}"


def platform_of(channel_id: str | None) -> str:
    """Which platform a channel identity belongs to. A bare id is a Messenger PSID."""
    return INSTAGRAM if str(channel_id or "").startswith(_INSTAGRAM_PREFIX) else MESSENGER


def raw_id(channel_id: str | None) -> str:
    """The id Meta knows the customer by: the PSID, or the IGSID without its prefix."""
    value = str(channel_id or "")
    return value[len(_INSTAGRAM_PREFIX):] if value.startswith(_INSTAGRAM_PREFIX) else value
