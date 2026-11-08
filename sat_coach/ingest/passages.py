"""Fetch real, redistributable reading passages.

Sources (the same genres the SAT draws on):
- Project Gutenberg (public domain): literature, plus historical speeches and essays.
- PLOS open-access journals (CC BY 4.0): science abstracts, which are DSAT-length.

Each text is cleaned and cut into 250-700 word excerpts at paragraph boundaries.
"""

from __future__ import annotations

import re

import httpx
from sqlmodel import select

from ..db import Passage, session

# Curated Gutenberg ids: (id, title, author, year, genre)
GUTENBERG = [
    (1342, "Pride and Prejudice", "Jane Austen", "1813", "literature"),
    (158, "Emma", "Jane Austen", "1815", "literature"),
    (1260, "Jane Eyre", "Charlotte Brontë", "1847", "literature"),
    (145, "Middlemarch", "George Eliot", "1871", "literature"),
    (2701, "Moby-Dick", "Herman Melville", "1851", "literature"),
    (514, "Little Women", "Louisa May Alcott", "1868", "literature"),
    (23, "Narrative of the Life of Frederick Douglass", "Frederick Douglass", "1845", "history"),
    (71, "On the Duty of Civil Disobedience", "Henry David Thoreau", "1849", "history"),
    (3420, "A Vindication of the Rights of Woman", "Mary Wollstonecraft", "1792", "history"),
    (1404, "The Federalist Papers", "Alexander Hamilton, James Madison, John Jay", "1788", "history"),
    (16643, "Essays: First Series", "Ralph Waldo Emerson", "1841", "humanities"),
    (205, "Walden", "Henry David Thoreau", "1854", "humanities"),
    (1228, "On the Origin of Species", "Charles Darwin", "1859", "science"),
]

PLOS_QUERIES = ["ecology", "astronomy", "neuroscience", "archaeology", "climate", "animal behavior", "materials", "linguistics"]

UA = {"User-Agent": "sat-coach/0.1 (educational, open source)"}


def strip_gutenberg(text: str) -> str:
    start = re.search(r"\*\*\* ?START OF (THE|THIS) PROJECT GUTENBERG.*?\*\*\*", text, re.I)
    end = re.search(r"\*\*\* ?END OF (THE|THIS) PROJECT GUTENBERG", text, re.I)
    return text[start.end() if start else 0 : end.start() if end else len(text)]


def paragraphs(text: str) -> list[str]:
    paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text.replace("\r", ""))]
    return [p for p in paras if p]


def is_prose(p: str) -> bool:
    """Skip headings, tables of contents, and illustration notes."""
    words = p.split()
    if len(words) < 25:
        return False
    if p.isupper() or re.match(r"^(chapter|book|letter|section|contents|illustration)\b", p, re.I):
        return False
    return sum(c.isalpha() for c in p) / max(1, len(p)) > 0.7


def excerpts(text: str, min_words: int = 250, max_words: int = 700, limit: int = 3, skip_frac: float = 0.05) -> list[str]:
    """Contiguous runs of prose paragraphs between min_words and max_words."""
    paras = paragraphs(text)
    paras = paras[int(len(paras) * skip_frac):]  # skip front matter
    out, cur, n = [], [], 0
    for p in paras:
        if not is_prose(p):
            cur, n = [], 0
            continue
        w = len(p.split())
        if w > max_words:
            cur, n = [], 0
            continue
        if n + w > max_words:
            if n >= min_words:
                out.append("\n\n".join(cur))
            cur, n = [], 0
        cur.append(p)
        n += w
        if n >= min_words and n >= (min_words + max_words) // 2:
            out.append("\n\n".join(cur))
            cur, n = [], 0
        if len(out) >= limit * 8:
            break
    if not out:
        return []
    step = max(1, len(out) // limit)  # spread picks across the book
    return out[::step][:limit]


def fetch_gutenberg(client: httpx.Client, per_book: int = 2) -> list[Passage]:
    found = []
    for gid, title, author, year, genre in GUTENBERG:
        url = f"https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.txt"
        try:
            r = client.get(url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            print(f"  skip {title}: {e}")
            continue
        for i, ex in enumerate(excerpts(strip_gutenberg(r.text), limit=per_book)):
            found.append(Passage(title=f"{title} (excerpt {i + 1})", author=author, year=year, genre=genre, text=ex,
                                 source_url=f"https://www.gutenberg.org/ebooks/{gid}", license="Public domain",
                                 origin="fetched"))
    return found


def fetch_plos(client: httpx.Client, per_query: int = 2) -> list[Passage]:
    found = []
    for q in PLOS_QUERIES:
        try:
            r = client.get("https://api.plos.org/search", params={
                "q": f'abstract:"{q}"', "fl": "id,title_display,author_display,publication_date,abstract",
                "fq": "doc_type:full", "rows": per_query * 3, "wt": "json"})
            r.raise_for_status()
            docs = r.json()["response"]["docs"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            print(f"  skip PLOS '{q}': {e}")
            continue
        kept = 0
        for d in docs:
            abstract = " ".join(d.get("abstract") or []).strip()
            words = len(abstract.split())
            if not 150 <= words <= 450:
                continue
            authors = d.get("author_display") or ["et al."]
            found.append(Passage(
                title=re.sub(r"<[^>]+>", "", d.get("title_display", "Untitled")), author=authors[0] + (" et al." if len(authors) > 1 else ""),
                year=(d.get("publication_date") or "")[:4], genre="science", text=abstract,
                source_url=f"https://doi.org/{d['id']}", license="CC BY 4.0", origin="fetched"))
            kept += 1
            if kept >= per_query:
                break
    return found


def fetch_all(gutenberg: bool = True, plos: bool = True) -> int:
    with httpx.Client(headers=UA, timeout=30, follow_redirects=True) as client:
        new = (fetch_gutenberg(client) if gutenberg else []) + (fetch_plos(client) if plos else [])
    added = 0
    with session() as s:
        for p in new:
            if s.exec(select(Passage).where(Passage.title == p.title, Passage.author == p.author)).first():
                continue
            s.add(p)
            added += 1
        s.commit()
    return added
