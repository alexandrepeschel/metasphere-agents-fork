"""Concrete :class:`SurfaceAdapter` implementations.

Each module here adapts one transport (telegram, web chat, email, …) to
the :class:`metasphere.gateway.adapter.SurfaceAdapter` Protocol so the
daemon can drive it through a uniform interface.
"""

from __future__ import annotations

from .matrix import MatrixAdapter
from .slack import SlackAdapter
from .telegram import TelegramAdapter

__all__ = ["MatrixAdapter", "SlackAdapter", "TelegramAdapter"]
