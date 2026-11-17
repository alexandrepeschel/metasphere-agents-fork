"""Matrix client-server API — outbound + sync + media.

Config resolution (``load_config``):

1. ``~/.metasphere/config/<surface_id>.env`` (falls back to ``matrix.env``):
   ``MATRIX_HOMESERVER``, ``MATRIX_USER``, ``MATRIX_ROOM_ALEX``,
   ``MATRIX_ALEX`` (+ optional ``MATRIX_ALLOWED`` comma list).
2. Access token: env ``MATRIX_ACCESS_TOKEN`` or the JSON token file
   ``~/.metasphere/secrets/matrix-clay.token`` (``access_token`` key).

The token is never logged or put in an exception message.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

CONFIG_DIR = os.path.expanduser("~/.metasphere/config")
TOKEN_PATH = os.path.expanduser("~/.metasphere/secrets/matrix-clay.token")

# Matrix events cap at ~65 KB; chunk well below that.
CHUNK_MAX = 16000
DEFAULT_TIMEOUT = 30.0


class MatrixAPIError(RuntimeError):
    """Non-2xx Matrix response (or transport failure with ``status=0``)."""

    def __init__(self, status: int, errcode: str = "", error: str = "") -> None:
        self.status = status
        self.errcode = errcode
        self.error = error
        super().__init__(f"matrix HTTP {status} {errcode}: {error}".strip())


@dataclass
class Config:
    homeserver: str
    access_token: str = field(repr=False)
    user_id: str = ""
    room_alex: str = ""
    alex: str = ""
    allowed: list[str] = field(default_factory=list)


def _read_env_file(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_config(surface_id: str = "matrix") -> Config:
    """Resolve the Matrix config for ``surface_id``.

    Raises ``FileNotFoundError`` (path listed, never a secret) when the
    homeserver or token cannot be resolved.
    """
    per = os.path.join(CONFIG_DIR, f"{surface_id}.env")
    env = _read_env_file(per) or _read_env_file(os.path.join(CONFIG_DIR, "matrix.env"))
    homeserver = (
        os.environ.get("MATRIX_HOMESERVER") or env.get("MATRIX_HOMESERVER") or ""
    ).rstrip("/")
    token = os.environ.get("MATRIX_ACCESS_TOKEN") or ""
    user_id = os.environ.get("MATRIX_USER") or env.get("MATRIX_USER") or ""
    if not token and os.path.exists(TOKEN_PATH):
        try:
            with open(TOKEN_PATH, encoding="utf-8") as f:
                data = json.load(f)
            token = str(data.get("access_token") or "")
            homeserver = homeserver or str(data.get("homeserver") or "").rstrip("/")
            user_id = user_id or str(data.get("user_id") or "")
        except (OSError, ValueError):
            token = ""
    if not homeserver or not token:
        raise FileNotFoundError(
            f"No Matrix homeserver/token for surface {surface_id!r}: need "
            f"{per} (MATRIX_HOMESERVER) and {TOKEN_PATH} (access_token)."
        )
    alex = env.get("MATRIX_ALEX", "")
    allowed = [s.strip() for s in env.get("MATRIX_ALLOWED", "").split(",") if s.strip()]
    if alex and alex not in allowed:
        allowed.insert(0, alex)
    return Config(
        homeserver=homeserver,
        access_token=token,
        user_id=user_id,
        room_alex=env.get("MATRIX_ROOM_ALEX", ""),
        alex=alex,
        allowed=allowed,
    )


# ---------------------------------------------------------------- transport

def _session():
    import requests

    return requests.Session()


_SESSION = None


def _get_session():
    global _SESSION
    if _SESSION is None:
        _SESSION = _session()
    return _SESSION


def call(
    method: str,
    path: str,
    *,
    cfg: Optional[Config] = None,
    surface_id: str = "matrix",
    params: Optional[dict] = None,
    json_body: Optional[dict] = None,
    data: Any = None,
    headers: Optional[dict] = None,
    timeout: float = DEFAULT_TIMEOUT,
    raw: bool = False,
    retries: int = 2,
):
    """One authenticated client-server call; returns parsed JSON (or the
    ``requests.Response`` when ``raw``). Retries 429 (``retry_after_ms``)
    and 5xx / transport errors up to ``retries`` times. Raises
    :class:`MatrixAPIError`."""
    import requests

    cfg = cfg or load_config(surface_id)
    url = f"{cfg.homeserver}{path}"
    hdrs = {"Authorization": f"Bearer {cfg.access_token}"}
    hdrs.update(headers or {})
    attempt = 0
    while True:
        try:
            resp = _get_session().request(
                method, url, params=params, json=json_body, data=data,
                headers=hdrs, timeout=timeout,
            )
        except requests.RequestException as e:
            if attempt < retries:
                attempt += 1
                time.sleep(min(2 ** attempt, 8))
                continue
            raise MatrixAPIError(0, "M_TRANSPORT", type(e).__name__) from None
        if resp.status_code == 429 and attempt < retries:
            attempt += 1
            try:
                wait = float(resp.json().get("retry_after_ms", 1000)) / 1000.0
            except Exception:  # noqa: BLE001
                wait = 1.0
            time.sleep(min(max(wait, 0.1), 30.0))
            continue
        if resp.status_code >= 500 and attempt < retries:
            attempt += 1
            time.sleep(min(2 ** attempt, 8))
            continue
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = {}
            raise MatrixAPIError(
                resp.status_code, str(body.get("errcode", "")), str(body.get("error", ""))[:200],
            )
        if raw:
            return resp
        try:
            return resp.json()
        except ValueError:
            return {}


# ---------------------------------------------------------------- rendering

_MD = None


def markdown_to_html(text: str) -> str:
    """Render markdown to HTML for ``formatted_body``.

    Raw HTML in the source is escaped (``html=False``). Single newlines
    become ``<br>`` so prose written for chat keeps its line breaks. Falls
    back to escaped text + ``<br>`` when markdown-it is unavailable.
    """
    global _MD
    try:
        if _MD is None:
            from markdown_it import MarkdownIt

            _MD = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable("table")
        return _MD.render(text).strip()
    except Exception:  # noqa: BLE001
        import html as _html

        return _html.escape(text).replace("\n", "<br>")


def html_to_plain(html_text: str) -> str:
    import html as _html

    t = re.sub(r"<br\s*/?>", "\n", html_text)
    t = re.sub(r"</(p|li|div|h\d)>", "\n", t)
    return _html.unescape(re.sub(r"<[^>]+>", "", t)).strip()


def _split_chunks(text: str, max_len: int = CHUNK_MAX) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= max_len:
            chunks.append(rest)
            break
        window = rest[:max_len]
        cut = window.rfind("\n\n")
        if cut < max_len // 2:
            cut = window.rfind("\n")
        if cut < max_len // 2:
            cut = max_len
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    return chunks


# ------------------------------------------------------------------- calls

def whoami(cfg: Optional[Config] = None) -> dict:
    return call("GET", "/_matrix/client/v3/account/whoami", cfg=cfg)


SYNC_FILTER = json.dumps({
    "presence": {"types": []},
    "account_data": {"types": []},
    "room": {
        "timeline": {"limit": 50},
        "ephemeral": {"types": []},
        "account_data": {"types": []},
        "state": {"types": ["m.room.member"], "lazy_load_members": True},
    },
})

INITIAL_FILTER = json.dumps({
    "presence": {"types": []},
    "account_data": {"types": []},
    "room": {
        "timeline": {"limit": 0},
        "ephemeral": {"types": []},
        "account_data": {"types": []},
        "state": {"types": []},
    },
})


def sync(
    since: Optional[str],
    *,
    timeout_ms: int = 25000,
    cfg: Optional[Config] = None,
    initial: bool = False,
) -> dict:
    """One ``/sync``. ``initial`` (no ``since``) asks for zero timeline
    events: we only want the ``next_batch`` cursor, never history."""
    params: dict[str, Any] = {
        "timeout": timeout_ms,
        "filter": INITIAL_FILTER if (initial or not since) else SYNC_FILTER,
    }
    if since:
        params["since"] = since
    return call(
        "GET", "/_matrix/client/v3/sync", cfg=cfg, params=params,
        timeout=timeout_ms / 1000.0 + 20.0, retries=0,
    )


def send_event(
    room_id: str,
    content: dict,
    *,
    event_type: str = "m.room.message",
    cfg: Optional[Config] = None,
    txn_id: Optional[str] = None,
) -> str:
    """PUT an event; returns its ``event_id``."""
    txn = txn_id or uuid.uuid4().hex
    path = (
        f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}"
        f"/send/{quote(event_type, safe='')}/{txn}"
    )
    return str(call("PUT", path, cfg=cfg, json_body=content).get("event_id", ""))


def send_message(
    room_id: str,
    text: str,
    *,
    html: Optional[str] = None,
    reply_to: Optional[str] = None,
    msgtype: str = "m.text",
    cfg: Optional[Config] = None,
) -> list[str]:
    """Send ``text`` (markdown) to ``room_id``; returns event ids.

    ``body`` carries the markdown source (what plain clients show);
    ``formatted_body`` the rendered HTML. Pass ``html`` to supply HTML
    directly (``body`` is then its plain-text form). Long text is chunked.
    """
    if not text and not html:
        raise ValueError("send_message: text must be non-empty")
    ids: list[str] = []
    if html is not None:
        pieces = [(html_to_plain(html), html)]
    else:
        pieces = [(c, markdown_to_html(c)) for c in _split_chunks(text)]
    total = len(pieces)
    for i, (body, formatted) in enumerate(pieces, start=1):
        if total > 1:
            body = f"[{i}/{total}] {body}"
            formatted = f"[{i}/{total}] {formatted}"
        content: dict[str, Any] = {
            "msgtype": msgtype,
            "body": body,
            "format": "org.matrix.custom.html",
            "formatted_body": formatted,
        }
        if reply_to and i == 1:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
        ids.append(send_event(room_id, content, cfg=cfg))
    return ids


def react(room_id: str, event_id: str, key: str, *, cfg: Optional[Config] = None) -> str:
    return send_event(
        room_id,
        {"m.relates_to": {"rel_type": "m.annotation", "event_id": event_id, "key": key}},
        event_type="m.reaction", cfg=cfg,
    )


def get_event(room_id: str, event_id: str, *, cfg: Optional[Config] = None) -> dict:
    path = (
        f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}"
        f"/event/{quote(event_id, safe='')}"
    )
    return call("GET", path, cfg=cfg, timeout=15.0, retries=1)


def upload_media(
    path: str, *, mime: Optional[str] = None, filename: Optional[str] = None,
    cfg: Optional[Config] = None,
) -> str:
    """Upload a file; returns the ``mxc://`` URI."""
    name = filename or os.path.basename(path)
    mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
    with open(path, "rb") as f:
        payload = f.read()
    resp = call(
        "POST", "/_matrix/media/v3/upload", cfg=cfg,
        params={"filename": name}, data=payload,
        headers={"Content-Type": mime}, timeout=120.0,
    )
    return str(resp.get("content_uri", ""))


def send_file(
    room_id: str,
    path: str,
    *,
    caption: Optional[str] = None,
    filename: Optional[str] = None,
    voice: bool = False,
    mime: Optional[str] = None,
    duration_ms: Optional[int] = None,
    cfg: Optional[Config] = None,
) -> str:
    """Upload ``path`` and post it. ``voice`` posts ``m.audio`` flagged as a
    voice message (Element renders the waveform player)."""
    name = filename or os.path.basename(path)
    mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
    mxc = upload_media(path, mime=mime, filename=name, cfg=cfg)
    size = os.path.getsize(path)
    info: dict[str, Any] = {"mimetype": mime, "size": size}
    if duration_ms:
        info["duration"] = duration_ms
    if voice or mime.startswith("audio/"):
        msgtype = "m.audio"
    elif mime.startswith("image/"):
        msgtype = "m.image"
    elif mime.startswith("video/"):
        msgtype = "m.video"
    else:
        msgtype = "m.file"
    content: dict[str, Any] = {
        "msgtype": msgtype, "body": caption or name, "url": mxc, "info": info,
    }
    if caption:
        content["filename"] = name
    if voice:
        content["org.matrix.msc3245.voice"] = {}
        content["org.matrix.msc1767.audio"] = {"duration": duration_ms or 0}
    return send_event(room_id, content, cfg=cfg)


def download_media(mxc: str, dest: Path, *, cfg: Optional[Config] = None) -> int:
    """Download ``mxc://server/id`` to ``dest``; returns bytes written.
    Tries the authenticated (v1) endpoint first, then the legacy v3 one."""
    m = re.match(r"^mxc://([^/]+)/(.+)$", mxc or "")
    if not m:
        raise ValueError(f"not an mxc uri: {mxc!r}")
    server, media_id = m.group(1), m.group(2)
    last: Optional[Exception] = None
    for path in (
        f"/_matrix/client/v1/media/download/{quote(server)}/{quote(media_id)}",
        f"/_matrix/media/v3/download/{quote(server)}/{quote(media_id)}",
    ):
        try:
            resp = call("GET", path, cfg=cfg, raw=True, timeout=60.0, retries=1)
        except MatrixAPIError as e:
            last = e
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return len(resp.content)
    raise last or MatrixAPIError(0, "M_UNKNOWN", "download failed")


def _cc_outbound_to_orchestrator(body: str) -> None:
    """Mirror a non-orchestrator agent's Matrix send into the orchestrator
    inbox (same contract as the Telegram CC). Never raises."""
    sender = (os.environ.get("METASPHERE_AGENT_ID") or "").strip()
    if not sender or sender == "@orchestrator":
        return
    try:
        from metasphere import messages as _messages

        _messages.send_message(
            "@orchestrator", "!info", f"[matrix-cc from {sender}]\n{body}",
            from_agent=sender, wake=False,
        )
    except Exception:  # noqa: BLE001
        pass


def send_with_cc(
    room_id: str,
    text: Optional[str] = None,
    *,
    html: Optional[str] = None,
    document_path: Optional[str] = None,
    caption: Optional[str] = None,
    filename: Optional[str] = None,
    voice: bool = False,
    reply_to: Optional[str] = None,
    cfg: Optional[Config] = None,
):
    """Send text or a file, then CC the orchestrator when the sender is
    another agent (mirrors ``telegram.api.send_with_cc``)."""
    if document_path is not None:
        if text is not None:
            raise ValueError("send_with_cc: pass text OR document_path, not both")
        eid = send_file(
            room_id, document_path, caption=caption, filename=filename,
            voice=voice, cfg=cfg,
        )
        _cc_outbound_to_orchestrator(
            caption or f"[sent {'voice note' if voice else 'document'}: {document_path}]"
        )
        return eid
    if text is None and html is None:
        raise ValueError("send_with_cc: text or document_path required")
    ids = send_message(room_id, text or "", html=html, reply_to=reply_to, cfg=cfg)
    _cc_outbound_to_orchestrator(text if text is not None else html_to_plain(html or ""))
    return ids
