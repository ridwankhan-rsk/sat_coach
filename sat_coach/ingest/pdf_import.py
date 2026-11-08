"""Import official practice material from PDFs the user downloaded themselves.

College Board practice tests and question-bank exports are copyrighted: they are imported
into data/private/ (gitignored) for the user's own practice and as RAG exemplars, and are
never committed or redistributed.

Pipeline: PyMuPDF extracts text page by page → the LLM structures chunks into question
JSON (stem, choices, answer, official rationale, skill, difficulty) → stored with
origin="official".
"""

from __future__ import annotations

import json
from pathlib import Path

from ..db import Question, session
from ..llm import LLM, LLMError
from ..taxonomy import SKILL_BY_ID, SKILLS

SYSTEM = """You convert text extracted from official Digital SAT practice PDFs into structured \
JSON. Copy question text, choices, and explanations faithfully; do not invent content. Skip \
anything that is not a complete question."""

SKILL_LIST = "\n".join(f"- {s.id}: {s.name} ({s.section})" for s in SKILLS)


def extract_pages(pdf_path: Path) -> list[str]:
    try:
        import pymupdf
    except ImportError as e:  # pragma: no cover
        raise SystemExit("PDF import needs PyMuPDF: pip install 'sat-coach[pdf]'") from e
    with pymupdf.open(pdf_path) as doc:
        return [page.get_text() for page in doc]


def chunk(pages: list[str], max_chars: int = 12000) -> list[str]:
    out, cur = [], ""
    for p in pages:
        if len(cur) + len(p) > max_chars and cur:
            out.append(cur)
            cur = ""
        cur += "\n\n" + p
    if cur.strip():
        out.append(cur)
    return out


def structure_chunk(llm: LLM, text: str) -> list[dict]:
    prompt = f"""Extract every complete question from this text. If the text contains answer
explanations, include them as the rationale (keep the official wording).

Skills (choose the closest id):
{SKILL_LIST}

Return {{"items": [{{"skill": "<id>", "difficulty": "easy|medium|hard", "qtype": "mcq|spr",
"excerpt": "<R&W passage text, or null>", "stem": "...", "choices": ["...", "...", "...", "..."],
"answer": "<letter, or value for spr>", "rationale": {{"A": "...", ...}} or {{"correct": "..."}}}}]}}.
Use "medium" if difficulty is not stated.

TEXT:
{text}"""
    data = llm.generate_json(SYSTEM, prompt, check=False)
    return data.get("items", []) if isinstance(data, dict) else []


def import_pdf(llm: LLM, pdf_path: Path) -> int:
    added = 0
    for ch in chunk(extract_pages(pdf_path)):
        try:
            items = structure_chunk(llm, ch)
        except LLMError as e:
            print(f"  chunk skipped: {e}")
            continue
        with session() as s:
            for it in items:
                sk = SKILL_BY_ID.get(str(it.get("skill")))
                if not sk or not it.get("stem") or not it.get("answer"):
                    continue
                qtype = "spr" if it.get("qtype") == "spr" else "mcq"
                choices = [str(c) for c in (it.get("choices") or [])] if qtype == "mcq" else []
                if qtype == "mcq" and len(choices) != 4:
                    continue
                s.add(Question(
                    section=sk.section, domain=sk.domain, skill=sk.id,
                    difficulty=it.get("difficulty") if it.get("difficulty") in ("easy", "medium", "hard") else "medium",
                    qtype=qtype, stem=str(it["stem"]), choices=choices,
                    answer=str(it["answer"]).strip().upper()[:1] if qtype == "mcq" else str(it["answer"]),
                    rationale=it.get("rationale") or {}, excerpt=it.get("excerpt") or None, origin="official",
                ))
                added += 1
            s.commit()
    return added


def dump_debug(pdf_path: Path, out: Path) -> None:
    """Write extracted text to a file, to check what PyMuPDF sees in a given PDF."""
    out.write_text(json.dumps(extract_pages(pdf_path), indent=1), encoding="utf-8")
