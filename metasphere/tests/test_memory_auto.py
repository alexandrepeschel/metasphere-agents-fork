"""Tests for AutoMemoryStrategy — MEMORY.md + linked files recall."""

from __future__ import annotations

import re
from pathlib import Path

from metasphere.memory.auto import AutoMemoryStrategy, _default_memory_root


def _setup_memory(tmp_path: Path) -> Path:
    """Create a minimal MEMORY.md + linked files under tmp_path."""
    root = tmp_path / "memory"
    root.mkdir()

    (root / "MEMORY.md").write_text(
        "- [Tmux injection fix](tmux_fix.md) — fixed the interleaving bug\n"
        "- [Telegram auth notes](telegram_auth.md) — token exchange design\n"
        "- [Noise entry](noise.md) — irrelevant content about cooking\n"
        "- [Missing file](does_not_exist.md) — broken link\n"
        "- [Escape attempt](../../../etc/passwd) — path traversal\n",
        encoding="utf-8",
    )

    (root / "tmux_fix.md").write_text(
        "---\nname: tmux injection fix\ntype: project\n---\n"
        "Fixed the tmux send-keys interleaving bug where concurrent "
        "writers braided characters into the orchestrator pane. "
        "The fix uses a per-pane fcntl lock around submit_to_tmux.\n",
        encoding="utf-8",
    )

    (root / "telegram_auth.md").write_text(
        "---\nname: Telegram auth\ntype: reference\n---\n"
        "Token exchange for telegram user authorization. "
        "Each new contact must present a token to register. "
        "Address book maps chat_id to authorized user.\n",
        encoding="utf-8",
    )

    (root / "noise.md").write_text(
        "---\nname: Noise\ntype: user\n---\n"
        "My favorite recipe for banana bread involves flour eggs "
        "and sugar. Nothing about software engineering here.\n",
        encoding="utf-8",
    )

    return root


def test_search_ranks_tmux_file_first_for_tmux_query(tmp_path):
    root = _setup_memory(tmp_path)
    strat = AutoMemoryStrategy(root=root)
    hits = strat.search("tmux injection interleaving submit")
    assert len(hits) >= 1
    assert hits[0].source == "auto-memory:tmux_fix.md"
    assert hits[0].score > 0


def test_search_ranks_telegram_for_telegram_query(tmp_path):
    root = _setup_memory(tmp_path)
    strat = AutoMemoryStrategy(root=root)
    hits = strat.search("telegram authorization token exchange")
    assert len(hits) >= 1
    assert hits[0].source == "auto-memory:telegram_auth.md"


def test_search_skips_broken_links(tmp_path):
    root = _setup_memory(tmp_path)
    strat = AutoMemoryStrategy(root=root)
    hits = strat.search("does not exist missing file")
    sources = {h.source for h in hits}
    assert "auto-memory:does_not_exist.md" not in sources


def test_search_blocks_path_traversal(tmp_path):
    root = _setup_memory(tmp_path)
    strat = AutoMemoryStrategy(root=root)
    hits = strat.search("etc passwd escape")
    sources = {h.source for h in hits}
    assert not any("passwd" in s for s in sources)


def test_search_empty_query_returns_empty(tmp_path):
    root = _setup_memory(tmp_path)
    strat = AutoMemoryStrategy(root=root)
    assert strat.search("") == []
    assert strat.search("   ") == []


def test_search_strips_frontmatter_from_excerpts(tmp_path):
    root = _setup_memory(tmp_path)
    strat = AutoMemoryStrategy(root=root)
    hits = strat.search("tmux injection fix")
    assert hits
    # Excerpt should NOT contain frontmatter delimiters
    assert "---" not in hits[0].excerpt
    assert "name:" not in hits[0].excerpt
    # But should contain the actual body
    assert "interleaving" in hits[0].excerpt


def test_search_respects_limit(tmp_path):
    root = _setup_memory(tmp_path)
    strat = AutoMemoryStrategy(root=root)
    hits = strat.search("the", limit=1)
    assert len(hits) <= 1


def test_missing_memory_md_returns_empty(tmp_path):
    strat = AutoMemoryStrategy(root=tmp_path)
    assert strat.search("anything") == []


def test_scoring_favors_rare_distinctive_terms(tmp_path):
    """IDF weighting ranks a doc matching a rare, distinctive query term
    above one matching only a corpus-common term.

    ``common`` appears in every doc (low IDF); ``zebracorn`` appears in one
    (high IDF). A query carrying both must rank the distinctive doc first,
    which a plain overlap/len score could not distinguish."""
    root = tmp_path / "memory"
    root.mkdir()
    (root / "MEMORY.md").write_text(
        "- [rare](rare.md) — distinctive\n"
        "- [filler one](f1.md) — common only\n"
        "- [filler two](f2.md) — common only\n"
        "- [filler three](f3.md) — common only\n",
        encoding="utf-8",
    )
    (root / "rare.md").write_text("common zebracorn payload\n", encoding="utf-8")
    (root / "f1.md").write_text("common alpha bravo\n", encoding="utf-8")
    (root / "f2.md").write_text("common charlie delta\n", encoding="utf-8")
    (root / "f3.md").write_text("common echo foxtrot\n", encoding="utf-8")

    hits = AutoMemoryStrategy(root=root).search("common zebracorn")
    assert hits[0].source == "auto-memory:rare.md"
    # The distinctive match must out-score any common-only match.
    rare = next(h.score for h in hits if h.source == "auto-memory:rare.md")
    others = [h.score for h in hits if h.source != "auto-memory:rare.md"]
    assert all(rare > o for o in others)


def test_scoring_length_damps_huge_vocab_files(tmp_path):
    """A small, precisely-matching memo must out-rank a huge grab-bag file
    that merely happens to contain the query terms among a vast vocabulary.

    Without length damping the huge file's incidental overlap crowds the
    top slot; the damping term pushes the focused memo above it."""
    root = tmp_path / "memory"
    root.mkdir()
    (root / "MEMORY.md").write_text(
        "- [focused](focused.md) — small precise memo\n"
        "- [grabbag](grabbag.md) — huge mixed file\n",
        encoding="utf-8",
    )
    (root / "focused.md").write_text("widget calibration procedure\n", encoding="utf-8")
    # Huge vocabulary that also contains the query terms.
    filler = " ".join(f"tok{i}" for i in range(6000))
    (root / "grabbag.md").write_text(
        "widget calibration procedure " + filler + "\n", encoding="utf-8"
    )

    hits = AutoMemoryStrategy(root=root).search("widget calibration procedure")
    assert hits[0].source == "auto-memory:focused.md"


def test_default_memory_root_uses_explicit_root_not_pwd(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PWD", "/daemon/wrong")
    monkeypatch.delenv("METASPHERE_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("METASPHERE_REPO_ROOT", raising=False)
    expected = tmp_path / ".claude" / "projects" / "-home-op-projects-myproj" / "memory"
    assert _default_memory_root("/home/op/projects/myproj") == expected


def test_default_memory_root_env_maps_dotted_segments(tmp_path, monkeypatch):
    # A dotted cwd segment maps each '.' to '-' as well, so
    # /home/op/.ms/p -> -home-op--ms-p (double dash from the '/.' pair).
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PWD", "/daemon/wrong")
    monkeypatch.setenv("METASPHERE_PROJECT_ROOT", "/home/op/.ms/p")
    correct = tmp_path / ".claude" / "projects" / "-home-op--ms-p" / "memory"
    correct.mkdir(parents=True)
    assert _default_memory_root() == correct


def test_default_memory_root_fallback_is_metasphere_root(tmp_path, monkeypatch):
    # No canonical project env falls back to METASPHERE_DIR, never PWD or a
    # first-found unrelated memory directory.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PWD", "/daemon/wrong")
    monkeypatch.setenv("METASPHERE_DIR", str(tmp_path / ".metasphere"))
    monkeypatch.delenv("METASPHERE_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("METASPHERE_REPO_ROOT", raising=False)
    fallback = _default_memory_root()
    suffix = fallback.relative_to(tmp_path).as_posix()
    slug = re.sub(r"[/.]", "-", str(tmp_path / ".metasphere"))
    assert suffix == f".claude/projects/{slug}/memory"
