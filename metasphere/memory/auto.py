"""AutoMemoryStrategy — search the orchestrator's hand-curated MEMORY.md index.

Reads ``MEMORY.md``, follows each ``[title](file.md)`` link to its target,
token-overlaps the combined body against the query, and returns ranked
:class:`MemoryHit` objects. Highest-signal recall source for the
orchestrator: each entry is a distilled feedback/project/user/reference
memo from a past incident.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

from .base import MemoryHit, MemoryStrategy

# Vocabulary size at which log-length damping starts biting. Docs with a
# token set well above this shrink toward this fraction so a 100 KB+ memo
# stops crowding the top-N on sheer vocabulary overlap; small memos (the
# median linked file is a couple KB) are damped by roughly nothing.
_DAMP_VOCAB = 2000.0

_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+\.md)\)")


def _default_memory_root(project_root: Path | str | None = None) -> Path:
    """Return the Claude auto-memory tree for an explicit project root.

    The old resolver used daemon ``PWD`` and then selected the first memory
    tree it found.  Both are process-global accidents: a gateway launched
    from ``$HOME`` could recall a different project's memories.  Callers that
    know the active project pass it explicitly; the environment fallback is
    the canonical root exported by every managed Metasphere launch path.
    There is deliberately no ambient-cwd or first-directory fallback.
    """
    from ..paths import home as metasphere_home

    if project_root is None:
        import os
        project_root = (
            os.environ.get("METASPHERE_PROJECT_ROOT")
            or os.environ.get("METASPHERE_REPO_ROOT")
        )
    if project_root:
        root = Path(project_root).expanduser().resolve()
        slug = re.sub(r"[/.]", "-", str(root))
        return Path.home() / ".claude" / "projects" / slug / "memory"

    # Managed installs without a project override are rooted at the
    # Metasphere runtime, never at the daemon's cwd.
    root = metasphere_home().expanduser().resolve()
    slug = re.sub(r"[/.]", "-", str(root))
    return Path.home() / ".claude" / "projects" / slug / "memory"


def _tokenize(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{3,}", s.lower())}


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + 5:]
    return text


class AutoMemoryStrategy(MemoryStrategy):
    """Reads MEMORY.md + each linked ``*.md``, ranks by token overlap."""

    name = "auto-memory"

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or _default_memory_root()

    def search(self, query: str, limit: int = 5) -> list[MemoryHit]:
        index = self.root / "MEMORY.md"
        if not index.is_file():
            return []
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []
        try:
            index_text = index.read_text(encoding="utf-8")
        except OSError:
            return []

        # First pass: load every linked memo once, keeping its token set and
        # body. IDF is a corpus statistic, so all docs must be in hand before
        # any can be scored.
        root_resolved = self.root.resolve()
        # rel_path, title, body, tokens, absolute path
        docs: list[tuple[str, str, str, set[str], str]] = []
        seen: set[str] = set()
        for match in _LINK_RE.finditer(index_text):
            title, rel_path = match.group(1), match.group(2)
            mem_file = (self.root / rel_path).resolve()
            try:
                mem_file.relative_to(root_resolved)
            except ValueError:
                continue
            key = str(mem_file)
            if key in seen or not mem_file.is_file():
                continue
            seen.add(key)
            try:
                body = mem_file.read_text(encoding="utf-8")
            except OSError:
                continue
            d_tokens = _tokenize(body)
            if not d_tokens:
                continue
            docs.append((rel_path, title, body, d_tokens, str(mem_file)))

        if not docs:
            return []

        # Document frequency → IDF. Rare, distinctive tokens carry more
        # signal than corpus-wide ones; smoothed so a term appearing in
        # every doc still has a small positive weight.
        n_docs = len(docs)
        df: Counter[str] = Counter()
        for _rel, _title, _body, d_tokens, _abs in docs:
            df.update(d_tokens)
        idf = {t: math.log((n_docs + 1) / (c + 0.5)) for t, c in df.items()}

        # Query IDF mass — the denominator the matched mass is measured
        # against. Terms unseen in the corpus contribute 0 and can't inflate.
        query_mass = sum(idf.get(t, 0.0) for t in q_tokens)

        hits: list[MemoryHit] = []
        for rel_path, title, body, d_tokens, abs_path in docs:
            inter = q_tokens & d_tokens
            if not inter:
                continue
            matched_mass = sum(idf.get(t, 0.0) for t in inter)
            base = matched_mass / query_mass if query_mass > 0 else 0.0
            # Mild log-length damping: ~1.0 for small memos, <1 for huge
            # ones, so vocabulary-rich files stop winning on size alone.
            damp = 1.0 / (1.0 + math.log(1.0 + len(d_tokens) / _DAMP_VOCAB))
            score = base * damp
            if score <= 0.0:
                continue
            excerpt = _strip_frontmatter(body).strip()[:400]
            hits.append(MemoryHit(
                source=f"auto-memory:{rel_path}",
                score=score,
                excerpt=excerpt,
                metadata={"title": title, "path": abs_path},
            ))

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]
