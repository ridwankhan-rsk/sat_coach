"""Load the committed seed bank (original questions + public-domain passages) into the DB."""

from __future__ import annotations

import json
from pathlib import Path

from sqlmodel import select

from .config import SEED_DIR
from .db import Passage, Question, session
from .taxonomy import SKILL_BY_ID


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_seed(seed_dir: Path = SEED_DIR) -> dict[str, int]:
    added = {"passages": 0, "questions": 0}
    with session() as s:
        existing = {(q.stem, q.excerpt or "", q.passage_id) for q in s.exec(select(Question)).all()}
        keys: dict[str, int] = {}
        for p in _jsonl(seed_dir / "passages.jsonl"):
            row = s.exec(select(Passage).where(Passage.title == p["title"], Passage.author == p["author"])).first()
            if row is None:
                row = Passage(**{k: v for k, v in p.items() if k != "key"})
                s.add(row)
                s.commit()
                s.refresh(row)
                added["passages"] += 1
            keys[p["key"]] = row.id
        for fname in ("math.jsonl", "rw.jsonl"):
            for q in _jsonl(seed_dir / fname):
                sk = SKILL_BY_ID[q["skill"]]
                passage_id = keys.get(q.pop("passage", None) or "", None)
                key = (q["stem"], q.get("excerpt") or "", passage_id)
                if key in existing:
                    continue
                s.add(Question(section=sk.section, domain=sk.domain, origin=q.pop("origin", "seed"),
                               passage_id=passage_id, **q))
                existing.add(key)
                added["questions"] += 1
        s.commit()
    return added
