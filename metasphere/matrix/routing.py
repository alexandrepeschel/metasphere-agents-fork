"""Access + reply-path policy for inbound Matrix events.

Only senders on the allow list (``MATRIX_ALEX`` + ``MATRIX_ALLOWED`` in
``matrix.env``) reach the agent; everyone else in any room the bot is in
is dropped before anything is archived or injected. With no allow list
configured NOTHING is accepted (fail closed — unlike Telegram's
fail-open default, a Matrix bot is reachable by any account that can
invite it).
"""

from __future__ import annotations

import shlex


def is_allowed(sender: str, allowed: list[str]) -> bool:
    return bool(sender) and sender in (allowed or [])


def derive_target_agent(surface_id: str) -> str:
    """``matrix`` -> ``@orchestrator``; ``matrix-foo`` -> ``@foo``."""
    body = surface_id
    if body.startswith("matrix-"):
        body = body[len("matrix-"):]
    if body == "matrix" or not body:
        return "@orchestrator"
    return body if body.startswith("@") else "@" + body


def reply_command(surface_id: str, room_id: str) -> str:
    """Copy-verbatim reply command baked into the envelope (independent of
    the active-conversation pin)."""
    parts = ["metasphere", "message", "send", "--surface", surface_id,
             "--chat-id", room_id]
    return " ".join(shlex.quote(p) for p in parts) + ' "<reply>"'


def short_name(user_id: str) -> str:
    """``@alexandre_peschel:matrix.org`` -> ``alexandre_peschel``."""
    return (user_id or "").lstrip("@").partition(":")[0] or "user"
