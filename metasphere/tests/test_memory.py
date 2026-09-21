"""Tests for metasphere.memory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from metasphere.memory import (
    CamStrategy,
    HybridStrategy,
    MemoryHit,
    TokenOverlapStrategy,
    context_for,
    recall,
)
from metasphere.memory.fts import _tokenize
from metasphere.paths import Paths


def _seed_doc(paths: Paths, rel: str, body: str) -> Path:
    p = paths.project_root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


# ---------- tokenizer ----------


def test_tokenize_drops_stopwords_and_short():
    toks = _tokenize("The Quick brown fox in a metasphere")
    assert "the" not in toks
    assert "in" not in toks
    assert "quick" in toks
    assert "brown" in toks
    assert "metasphere" in toks


# ---------- TokenOverlapStrategy ----------


def test_token_overlap_returns_hit_for_matching_doc(tmp_paths: Paths):
    _seed_doc(tmp_paths, "docs/alpha.md", "the quokka loves alpine ferns\n")
    _seed_doc(tmp_paths, "docs/beta.md",  "irrelevant content about pandas\n")
    strat = TokenOverlapStrategy(tmp_paths)
    hits = strat.search("quokka alpine", limit=5)
    assert hits, "expected at least one hit"
    assert any("alpha.md" in h.source for h in hits)


def test_token_overlap_score_in_unit_range_and_top_first(tmp_paths: Paths):
    _seed_doc(tmp_paths, "docs/a.md", "quokka alpine fern\n")  # 3 distinct
    _seed_doc(tmp_paths, "docs/b.md", "quokka only here\n")    # 1 distinct
    strat = TokenOverlapStrategy(tmp_paths)
    hits = strat.search("quokka alpine fern", limit=5)
    assert len(hits) == 2
    for h in hits:
        assert 0.0 <= h.score <= 1.0
    assert hits[0].score >= hits[1].score
    assert "a.md" in hits[0].source


def test_token_overlap_empty_query_returns_nothing(tmp_paths: Paths):
    _seed_doc(tmp_paths, "docs/a.md", "anything\n")
    assert TokenOverlapStrategy(tmp_paths).search("the and for", limit=5) == []


# ---------- CamStrategy ----------


def test_cam_strategy_missing_binary_returns_empty(tmp_paths: Paths, monkeypatch):
    # Force shutil.which to return None.
    monkeypatch.setattr("metasphere.memory.cam.shutil.which", lambda _b: None)
    # Reset the once-warned flag so the test is independent.
    import metasphere.memory.cam as cam_mod
    cam_mod._CAM_MISSING_WARNED = False
    strat = CamStrategy()
    assert strat.search("anything", limit=3) == []


def test_cam_automatic_gate_keeps_recurse_and_rejects_no_overlap(monkeypatch):
    """Batch-top normalization cannot make an unanchored CAM hit relevant."""
    monkeypatch.setattr("metasphere.memory.cam.shutil.which", lambda _b: "/bin/cam")
    payload = [
        {
            "path": "claude/old-recurse-session.md",
            "title": "Recurse infrastructure decisions",
            "keywords": ["recurse", "migration"],
            "snippet": "The Recurse service migration retained the API.",
            "score": 48.4,
        },
        {
            "path": "claude/unrelated.md",
            "title": "Telegram maintenance",
            "keywords": ["telegram"],
            "snippet": "Routine polling notes.",
            "score": 70.0,
        },
    ]
    monkeypatch.setattr(
        "metasphere.memory.cam.subprocess.run",
        lambda *a, **k: type("R", (), {
            "returncode": 0, "stdout": json.dumps(payload)
        })(),
    )
    gated = CamStrategy(min_lexical_overlap=1, max_eligible_hits=2)
    recurse = gated.search("What about Recurse?", limit=5)
    assert [hit.source for hit in recurse] == ["claude/old-recurse-session.md"]
    assert recurse[0].metadata["lexical_overlap"] == 1
    assert gated.search("frobnicate qzxv harmonica", limit=5) == []


# ---------- HybridStrategy ----------


class _StubStrategy(TokenOverlapStrategy):
    def __init__(self, name: str, hits: list[MemoryHit]):
        self.name = name
        self._hits = hits

    def search(self, query: str, limit: int = 5) -> list[MemoryHit]:
        return list(self._hits[:limit])


def test_hybrid_default_weights_dont_ceiling_below_default_floor():
    """Regression: the merged score of a full-strength curated hit must be
    able to clear the default noise floor.

    Weights are applied as per-strategy *ceilings* (``max`` on dedupe, not a
    sum). Previously ``auto-memory`` was absent from ``DEFAULT_WEIGHTS`` and
    fell back to ``1/len``, capping its merged score below the default floor
    so the per-turn recall block rendered empty every turn. auto-memory now
    carries weight 1.0, so a perfect curated hit reaches the floor."""
    from metasphere.memory.api import _DEFAULT_MIN_SCORE
    from metasphere.memory.hybrid import DEFAULT_WEIGHTS, HybridStrategy

    assert DEFAULT_WEIGHTS["auto-memory"] == 1.0
    # CAM has a narrow lexically-gated path above the floor; FTS remains
    # strictly corroborative.
    assert DEFAULT_WEIGHTS["cam"] > _DEFAULT_MIN_SCORE
    assert DEFAULT_WEIGHTS["fts"] < _DEFAULT_MIN_SCORE

    auto = _StubStrategy(
        "auto-memory",
        [MemoryHit(source="auto-memory:x.md", score=1.0, excerpt="curated")],
    )
    cam = _StubStrategy(
        "cam", [MemoryHit(source="cam/y", score=1.0, excerpt="fuzzy")]
    )
    hyb = HybridStrategy([auto, cam])  # default weights
    hits = {h.source: h.score for h in hyb.search("q", limit=10)}
    # Curated hit keeps its full score; the cam corroborator is ceilinged.
    assert hits["auto-memory:x.md"] == 1.0
    assert hits["cam/y"] == DEFAULT_WEIGHTS["cam"]
    # A full-strength curated hit and an exceptionally strong CAM hit clear
    # the floor, restoring pre-cutover-history recall.
    assert hits["auto-memory:x.md"] >= _DEFAULT_MIN_SCORE
    assert hits["cam/y"] >= _DEFAULT_MIN_SCORE


def test_context_for_default_floor_renders_curated_and_strong_cam_only():
    """Strong CAM-only history can render while weaker CAM noise is cut."""
    auto = _StubStrategy(
        "auto-memory",
        [MemoryHit(source="auto-memory:keep.md", score=1.0, excerpt="kept")],
    )
    cam = _StubStrategy("cam", [
        MemoryHit(source="cam/keep", score=0.95, excerpt="historical match"),
        MemoryHit(source="cam/drop", score=0.70, excerpt="generic noise"),
    ])
    out = context_for("ignored", strategies=[HybridStrategy([auto, cam])])
    assert "auto-memory:keep.md" in out
    assert "cam/keep" in out
    assert "cam/drop" not in out


def test_hybrid_unions_and_dedupes():
    a = _StubStrategy("fts", [
        MemoryHit(source="docs/a.md", score=1.0, excerpt="quokka alpine"),
        MemoryHit(source="docs/b.md", score=0.5, excerpt="other"),
    ])
    b = _StubStrategy("cam", [
        MemoryHit(source="docs/a.md", score=1.0, excerpt="quokka alpine"),  # dupe
        MemoryHit(source="cam/x", score=0.8, excerpt="cam-only"),
    ])
    hyb = HybridStrategy([a, b], weights={"fts": 0.4, "cam": 0.6})
    hits = hyb.search("quokka", limit=10)
    sources = [h.source for h in hits]
    # dedupes a.md, keeps b.md and cam/x
    assert sources.count("docs/a.md") == 1
    assert "cam/x" in sources
    assert "docs/b.md" in sources


# ---------- recall + context_for ----------


def test_recall_returns_top_n(tmp_paths: Paths):
    _seed_doc(tmp_paths, "docs/a.md", "quokka alpine fern\n")
    _seed_doc(tmp_paths, "docs/b.md", "quokka only\n")
    _seed_doc(tmp_paths, "docs/c.md", "alpine peaks\n")
    hits = recall(
        "quokka alpine",
        limit=2,
        strategies=[TokenOverlapStrategy(tmp_paths)],
    )
    assert len(hits) == 2
    assert "a.md" in hits[0].source


def test_context_for_filters_below_min_score():
    """``min_score`` drops sub-threshold hits before rendering. The
    threshold compares against the merged hybrid score — what the
    ``(score: X.XXX)`` line displays — so cuts match operator intuition."""
    stub = _StubStrategy(
        "fts",
        [
            MemoryHit(source="docs/keep.md", score=0.8, excerpt="kept"),
            MemoryHit(source="docs/edge.md", score=0.4, excerpt="edge"),
            MemoryHit(source="docs/drop.md", score=0.2, excerpt="dropped"),
        ],
    )
    out = context_for("ignored", strategies=[stub], min_score=0.35)
    assert "docs/keep.md" in out
    assert "docs/edge.md" in out
    assert "docs/drop.md" not in out


def test_context_for_skips_oversize_top_block_keeps_smaller():
    """An oversized top-ranked excerpt must not suppress smaller lower-
    ranked hits that still fit the budget — skip it, don't abort."""
    stub = _StubStrategy(
        "fts",
        [
            MemoryHit(source="docs/huge.md", score=0.9, excerpt="x" * 500),
            MemoryHit(source="docs/small.md", score=0.5, excerpt="fits"),
        ],
    )
    out = context_for("ignored", strategies=[stub], budget_chars=120, min_score=0.0)
    assert "docs/small.md" in out
    assert "docs/huge.md" not in out
    assert out  # not empty despite the oversized first hit


def test_context_for_respects_budget(tmp_paths: Paths):
    for i in range(20):
        _seed_doc(tmp_paths, f"docs/d{i}.md", "quokka alpine\n")
    out = context_for(
        "quokka alpine",
        budget_chars=200,
        strategies=[TokenOverlapStrategy(tmp_paths)],
    )
    assert len(out) <= 250  # budget + final newline slack
    assert "quokka" in out or "###" in out
