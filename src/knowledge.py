"""Tiny keyword search (BM25) over the Markdown files in kb/.

Each file is split into sections at "## " headings. Edit or add .md files in kb/ and
restart the agent to update what Ava knows. No database or embeddings needed for a
small knowledge base.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

KB_DIR = Path(__file__).resolve().parent.parent / "kb"

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from",
    "how", "i", "if", "in", "is", "it", "me", "my", "of", "on", "or", "our", "so", "tell",
    "that", "the", "their", "there", "this", "to", "was", "we", "what", "when", "where",
    "which", "who", "why", "will", "with", "you", "your", "about", "any", "get",
}  # fmt: skip


def _tokenize(text: str) -> list[str]:
    tokens = []
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if word in _STOPWORDS:
            continue
        # crude plural folding so "expenses" matches "expense"
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        tokens.append(word)
    return tokens


@dataclass
class Section:
    source: str
    heading: str
    text: str

    def render(self) -> str:
        return f"[{self.source} / {self.heading}]\n{self.text.strip()}"


class KnowledgeBase:
    def __init__(self, sections: list[Section], k1: float = 1.5, b: float = 0.75):
        self.sections = sections
        self.k1, self.b = k1, b
        # Index heading + body; the document title is included so "policy" finds policies.md
        self._docs = [
            _tokenize(f"{s.source} {s.heading} {s.heading} {s.text}") for s in sections
        ]
        self._tf = [Counter(d) for d in self._docs]
        self._avgdl = sum(len(d) for d in self._docs) / max(len(self._docs), 1)
        df: Counter[str] = Counter()
        for d in self._docs:
            df.update(set(d))
        n = len(self._docs)
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    @classmethod
    def load(cls, kb_dir: Path = KB_DIR) -> KnowledgeBase:
        sections: list[Section] = []
        for path in sorted(kb_dir.glob("*.md")):
            title, heading, lines = path.stem, "Overview", []
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                elif line.startswith("## "):
                    if any(ln.strip() for ln in lines):
                        sections.append(Section(title, heading, "\n".join(lines)))
                    heading, lines = line[3:].strip(), []
                elif not line.startswith(">"):  # skip "sample data" notes
                    lines.append(line)
            if any(ln.strip() for ln in lines):
                sections.append(Section(title, heading, "\n".join(lines)))
        return cls(sections)

    def search(self, query: str, top_k: int = 3) -> list[Section]:
        terms = _tokenize(query)
        scored = []
        for i, tf in enumerate(self._tf):
            dl = len(self._docs[i])
            score = 0.0
            for t in terms:
                if t not in tf:
                    continue
                f = tf[t]
                score += (
                    self._idf[t]
                    * f
                    * (self.k1 + 1)
                    / (f + self.k1 * (1 - self.b + self.b * dl / self._avgdl))
                )
            if score > 0:
                scored.append((score, i))
        scored.sort(reverse=True)
        return [self.sections[i] for _, i in scored[:top_k]]


_kb: KnowledgeBase | None = None


def get_kb() -> KnowledgeBase:
    """Load the knowledge base once per process."""
    global _kb
    if _kb is None:
        _kb = KnowledgeBase.load()
    return _kb
