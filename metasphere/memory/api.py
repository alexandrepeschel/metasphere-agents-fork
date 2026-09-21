"""Top-level convenience surface for the memory subpackage.

Callers (notably ``metasphere.context``) should use ``recall`` and
``context_for`` rather than instantiating strategies directly. This
keeps the choice of default strategy stack a single edit away.
"""

from __future__ import annotations

import os

from .base import MemoryHit, MemoryStrategy
from .cam import CamStrategy
from .fts import TokenOverlapStrategy
from .hybrid import HybridStrategy

# Noise floor for the merged hybrid score. Sits just above the fuzzy
# FTS corroborator ceiling (0.4). CAM can cross it at weight 0.7 only after the
# automatic context path's independent lexical-anchor/noise gate. Curated
# auto-memory remains unweighted and normally scores ~0.8-0.93.
_DEFAULT_MIN_SCORE = 0.61


def _resolve_min_score(min_score: float | None) -> float:
    """Resolve the noise-floor threshold.

    Explicit ``min_score`` wins; otherwise read
    ``METASPHERE_MEMORY_MIN_SCORE`` from the env (float-parsed), and
    fall back to ``_DEFAULT_MIN_SCORE`` on any parse failure.
    """
    if min_score is not None:
        return min_score
    raw = os.environ.get("METASPHERE_MEMORY_MIN_SCORE")
    if raw is None:
        return _DEFAULT_MIN_SCORE
    try:
        return float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MIN_SCORE


def default_strategies() -> list[MemoryStrategy]:
    """The default stack: a single hybrid wrapping cam + fts.

    Returned as a list so callers can extend or replace it; the hybrid
    is the only entry by default because it already merges its inputs.
    """
    return [HybridStrategy([CamStrategy(), TokenOverlapStrategy()])]


def _resolve(strategies: list[MemoryStrategy] | None) -> list[MemoryStrategy]:
    return list(strategies) if strategies is not None else default_strategies()


def recall(
    query: str,
    limit: int = 5,
    strategies: list[MemoryStrategy] | None = None,
) -> list[MemoryHit]:
    """Return the top-``limit`` hits across all configured strategies.

    Multiple strategies are merged through a single :class:`HybridStrategy`
    so the dedupe + weighting policy is owned in exactly one place.
    Passing a single strategy short-circuits the wrap.
    """
    if not query.strip():
        return []
    resolved = _resolve(strategies)
    if not resolved:
        return []
    if len(resolved) == 1:
        merger = resolved[0]
    else:
        merger = HybridStrategy(resolved)
    try:
        return merger.search(query, limit=limit)
    except Exception:
        return []


def context_for(
    query: str,
    budget_chars: int = 2048,
    strategies: list[MemoryStrategy] | None = None,
    min_score: float | None = None,
) -> str:
    """Format recall results as a markdown block capped at ``budget_chars``.

    Hits below ``min_score`` are dropped before rendering. The threshold
    is the merged hybrid score — the same value the rendered
    ``(score: X.XXX)`` line displays — so cuts are intuitive for
    operators tuning the env var.
    """
    hits = recall(query, limit=10, strategies=strategies)
    threshold = _resolve_min_score(min_score)
    hits = [h for h in hits if h.score >= threshold]
    if not hits:
        return ""
    out: list[str] = []
    used = 0
    for h in hits:
        block = f"### {h.source}  (score: {h.score:.3f})\n    {h.excerpt}\n"
        if used + len(block) > budget_chars:
            # Skip this oversized block rather than aborting — a single
            # long top excerpt must not suppress smaller lower-ranked hits
            # that would still fit, which would otherwise return an empty
            # context block despite valid recall results.
            continue
        out.append(block)
        used += len(block)
    return "".join(out).rstrip() + "\n" if out else ""
