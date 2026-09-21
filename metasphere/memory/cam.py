"""CAM-backed recall strategy.

Shells out to the ``cam`` CLI (external to metasphere). If the binary
is missing, ``search`` returns an empty list and logs a single warning
event per process.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

from ..events import log_event
from .base import MemoryHit, MemoryStrategy

_CAM_MISSING_WARNED = False

# Low-information query words cannot establish that a CAM transcript is about
# the current turn. The automatic context path requires overlap after this
# filter; explicit ``metasphere memory search --strategy cam`` keeps the raw
# CAM behavior by leaving ``min_lexical_overlap`` at zero.
_ANCHOR_STOPWORDS = frozenset({
    "about", "agent", "current", "decide", "from", "have", "memory",
    "please", "project", "status", "task", "that", "the", "this", "turn",
    "update", "what", "when", "where", "which", "with", "would",
})


def _anchor_tokens(text: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9][a-z0-9._-]{2,}", text.lower())
        if token not in _ANCHOR_STOPWORDS
    }


def _hit_anchor_text(result: dict) -> str:
    values = [
        result.get("path", ""), result.get("title", ""), result.get("snippet", "")
    ]
    for field in ("keywords", "entities"):
        value = result.get(field, [])
        if isinstance(value, list):
            values.extend(str(item) for item in value)
    return " ".join(str(value) for value in values if value)


def _warn_missing_once() -> None:
    global _CAM_MISSING_WARNED
    if _CAM_MISSING_WARNED:
        return
    _CAM_MISSING_WARNED = True
    try:
        log_event("memory.cam.missing", "cam binary not found on PATH")
    except Exception:
        pass


class CamStrategy(MemoryStrategy):
    """Wraps ``cam search --json`` as a memory backend."""

    name = "cam"

    def __init__(
        self,
        binary: str = "cam",
        timeout: float = 5.0,
        fast: bool = True,
        *,
        min_lexical_overlap: int = 0,
        max_eligible_hits: int | None = None,
    ) -> None:
        self._binary = binary
        self._timeout = timeout
        self._fast = fast
        self._min_lexical_overlap = max(min_lexical_overlap, 0)
        self._max_eligible_hits = max_eligible_hits

    def search(self, query: str, limit: int = 5) -> list[MemoryHit]:
        if not query.strip():
            return []
        if shutil.which(self._binary) is None:
            _warn_missing_once()
            return []
        cmd = [self._binary, "search", query, "--limit", str(limit), "--json"]
        if self._fast:
            cmd.append("--fast")
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except (subprocess.SubprocessError, OSError):
            return []
        if res.returncode != 0 or not res.stdout.strip():
            return []
        try:
            raw = json.loads(res.stdout)
        except json.JSONDecodeError:
            return []
        if not isinstance(raw, list):
            return []

        query_tokens = _anchor_tokens(query)
        eligible: list[tuple[dict, int]] = []
        seen: set[tuple[str, str]] = set()
        for result in raw:
            if not isinstance(result, dict):
                continue
            overlap = len(query_tokens & _anchor_tokens(_hit_anchor_text(result)))
            if overlap < self._min_lexical_overlap:
                continue
            key = (str(result.get("path", "cam")), str(result.get("snippet", ""))[:80])
            if key in seen:
                continue
            seen.add(key)
            eligible.append((result, overlap))
            if self._max_eligible_hits is not None and len(eligible) >= self._max_eligible_hits:
                break

        # CAM's numeric scale is corpus-relative and can score nonsense above
        # relevant terms. Normalize only after the independent lexical gate;
        # normalization ranks eligible results but never establishes relevance.
        max_score = max((float(r.get("score", 0.0)) for r, _ in eligible), default=0.0)
        if max_score <= 0:
            max_score = 1.0

        out: list[MemoryHit] = []
        for r, overlap in eligible:
            raw_score = float(r.get("score", 0.0))
            out.append(
                MemoryHit(
                    source=r.get("path", "cam"),
                    score=raw_score / max_score,
                    excerpt=(r.get("snippet") or r.get("title") or "")[:200],
                    metadata={
                        "agent": r.get("agent"),
                        "machine": r.get("machine"),
                        "date": r.get("date"),
                        "raw_score": raw_score,
                        "lexical_overlap": overlap,
                        "strategy": "cam",
                    },
                )
            )
        return out[:limit]
