"""MatrixAdapter — :class:`SurfaceAdapter` wrapping the Matrix poller + api."""

from __future__ import annotations

from ...matrix import api as _api
from ...matrix import poller as _poller
from ...matrix import routing as _routing
from ..adapter import SurfaceAdapter


class MatrixAdapter:
    """One Matrix bot identity per ``surface_id`` (``matrix`` by default;
    config ``~/.metasphere/config/<surface_id>.env``)."""

    surface_type: str = "matrix"

    def __init__(self, surface_id: str = "matrix",
                 target_agent_id: str | None = None) -> None:
        self.surface_id = surface_id
        self._target = target_agent_id or _routing.derive_target_agent(surface_id)

    def receive(self, timeout: int = 1) -> int:
        return _poller.run_poll_iteration(self.surface_id, self._target, timeout=timeout)

    def send(self, chat_id: int | str, text: str) -> None:
        _api.send_message(str(chat_id), text, cfg=_api.load_config(self.surface_id))


__all__ = ["MatrixAdapter", "SurfaceAdapter"]
