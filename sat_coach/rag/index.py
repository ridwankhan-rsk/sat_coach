"""A small, dependency-free BM25 retriever over the question bank.

Retrieval first filters by tags (section/skill/difficulty), then ranks by text similarity,
then picks a diverse set (maximal marginal relevance) so the generator sees varied
exemplars instead of near-duplicates. The interface (`retrieve_exemplars`) is what the
generators use, so this can be swapped for a vector store later without other changes.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from sqlmodel import select

from ..db import Question, session

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = set("the a an of to and in is are be for on that this with as by it its or at from which what".split())


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.docs = [tokenize(d) for d in docs]
        self.k1, self.b = k1, b
        self.avgdl = (sum(len(d) for d in self.docs) / len(self.docs)) if self.docs else 0.0
        df: Counter[str] = Counter()
        for d in self.docs:
            df.update(set(d))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.tf = [Counter(d) for d in self.docs]

    def scores(self, query: str) -> list[float]:
        q = tokenize(query)
        out = []
        for tf, d in zip(self.tf, self.docs):
            s = 0.0
            for t in q:
                if t not in tf:
                    continue
                f = tf[t]
                s += self.idf.get(t, 0.0) * f * (self.k1 + 1) / (
                    f + self.k1 * (1 - self.b + self.b * len(d) / (self.avgdl or 1))
                )
            out.append(s)
        return out


def _jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def mmr(candidates: list[Question], relevance: list[float], k: int, lam: float = 0.7) -> list[Question]:
    toks = [tokenize(q.stem) for q in candidates]
    chosen: list[int] = []
    remaining = list(range(len(candidates)))
    top = max(relevance) if relevance and max(relevance) > 0 else 1.0
    while remaining and len(chosen) < k:
        def score(i: int) -> float:
            div = max((_jaccard(toks[i], toks[j]) for j in chosen), default=0.0)
            return lam * (relevance[i] / top) - (1 - lam) * div

        best = max(remaining, key=score)
        chosen.append(best)
        remaining.remove(best)
    return [candidates[i] for i in chosen]


def retrieve_exemplars(
    section: str,
    skill: str,
    difficulty: str | None = None,
    query: str = "",
    k: int = 4,
    exclude_ids: set[int] | None = None,
) -> list[Question]:
    """Exemplars for few-shot generation. Prefers official (user-imported) items, then seeds,
    then previously generated items; same skill first, matching difficulty preferred."""
    exclude_ids = exclude_ids or set()
    with session() as s:
        pool = list(s.exec(select(Question).where(Question.section == section, Question.skill == skill)).all())
        if len(pool) < k:
            # Fall back to the same domain so rare skills still get context.
            from ..taxonomy import SKILL_BY_ID

            dom = SKILL_BY_ID[skill].domain
            pool += [q for q in s.exec(select(Question).where(Question.domain == dom)).all() if q not in pool]
    pool = [q for q in pool if q.id not in exclude_ids]
    if not pool:
        return []
    origin_rank = {"official": 2.0, "seed": 1.0, "generated": 0.0}
    rel = BM25([q.stem + " " + " ".join(q.choices) for q in pool]).scores(query) if query else [0.0] * len(pool)
    rel = [
        r + origin_rank.get(q.origin, 0) + (1.5 if q.skill == skill else 0) + (1.0 if q.difficulty == difficulty else 0)
        for r, q in zip(rel, pool)
    ]
    return mmr(pool, rel, k)
