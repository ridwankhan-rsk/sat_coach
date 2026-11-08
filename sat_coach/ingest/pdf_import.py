"""Import official practice material from PDFs the user downloaded themselves.

College Board practice tests and answer explanations are copyrighted: they are imported
into data/private/ (gitignored) for the user's own practice and as RAG exemplars, and are
never committed or redistributed.

Supported material
- Digital SAT (Bluebook) practice tests: short R&W texts (one per question) and math.
- Paper SAT practice tests: long reading passages, including paired passages, imported
  as articles together with their real question sets; Writing and Language passages,
  where each underlined portion becomes a fill-in-the-blank question; and math.
- Answer Explanations PDFs: the official answer and rationale for each question.

How files are matched
Put each test and its answer explanations in the same folder, e.g.
    sat-practice-test-1.pdf
    sat-practice-test-1-answers.pdf
Files whose names contain "answer", "explanation", "key" or "scoring" are read as answer
files. The remaining words of the name ("sat-practice-test-1") pair them, and questions are
matched to answers by section and question number. Questions with no answer are skipped.

Pipeline: PyMuPDF extracts text page by page → overlapping multi-page chunks → the LLM
structures each chunk into passages and questions → dedupe → store with origin="official".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import select

from ..db import Passage, Question, session
from ..llm import LLM, LLMError
from ..taxonomy import SKILL_BY_ID, SKILLS

SECTION_KEYS = {
    "rw1": "Digital SAT Reading and Writing, module 1",
    "rw2": "Digital SAT Reading and Writing, module 2",
    "math1": "Digital SAT Math, module 1",
    "math2": "Digital SAT Math, module 2",
    "reading": "Paper SAT Reading Test (section 1)",
    "writing": "Paper SAT Writing and Language Test (section 2)",
    "math_nc": "Paper SAT Math Test, no calculator (section 3)",
    "math_c": "Paper SAT Math Test, calculator (section 4)",
}
SECTION_LIST = "\n".join(f"- {k}: {v}" for k, v in SECTION_KEYS.items())
SKILL_LIST = "\n".join(f"- {s.id}: {s.name} ({s.section})" for s in SKILLS)
ANSWER_WORDS = ("answer", "explanation", "key", "scoring")
PRIVATE_LICENSE = "© College Board. Imported for personal practice; do not redistribute."

SYSTEM = """You convert text extracted from official SAT practice PDFs into structured JSON. \
Copy passage text, question text, choices and explanations faithfully; never invent or \
reword content. Skip anything incomplete."""

QUESTIONS_PROMPT = """Extract the reading passages and the questions from this practice-test text.

Section keys (use the one each question belongs to):
{sections}

Skills (choose the closest id):
{skills}

Rules:
1. PASSAGES (paper SAT Reading and Writing and Language): a long text that several questions
   share. Copy the full text without line numbers, page headers, or bracketed question numbers
   like [1] or (12). For paired passages, put both in one passage with the headings "Passage 1"
   and "Passage 2". Take the title, author and year from the introduction above the passage,
   if given. If the passage is cut off at the end of this text, set "complete": false.
2. Questions about a passage: set passage_ref to that passage's ref. If they belong to a
   passage that started before this text, set passage_ref to "previous".
3. Digital SAT questions each have their own short text: put it in "excerpt" and set
   passage_ref to null.
4. If a question refers to line numbers ("In lines 12-15"), quote those lines in "excerpt"
   so the question can be answered without line numbers.
5. Writing and Language questions about an underlined portion: set "excerpt" to the sentence
   containing it, with the underlined portion replaced by ______. Replace the choice
   "NO CHANGE" with the underlined text itself, so all four choices fill the blank. Add the
   stem "Which choice completes the text so that it conforms to the conventions of Standard
   English?" (for usage and punctuation) or "Which choice completes the text with the most
   logical transition?" (for transitions), or keep the printed question if there is one.
6. SKIP questions that ask for "the best evidence for the answer to the previous question",
   and questions that need a graph, table or figure that isn't reproduced as text.
7. "answer": the letter, or the value for student-produced responses, ONLY if it is printed
   with the question. Otherwise null.

Return {{"passages": [{{"ref": "p1", "title": "...", "author": "...", "year": "...", "text": "...", "complete": true}}],
"items": [{{"section_key": "reading", "number": 12, "passage_ref": "p1", "skill": "<id>",
"difficulty": "easy|medium|hard", "qtype": "mcq|spr", "excerpt": null, "stem": "...",
"choices": ["...", "...", "...", "..."], "answer": null}}]}}.
Use difficulty "medium" when it isn't stated.

TEXT:
{text}"""

ANSWERS_PROMPT = """This text is from an official SAT answer explanations document. Extract
the answer and the official explanation for every question.

Section keys:
{sections}

Return {{"answers": [{{"section_key": "reading", "number": 12, "answer": "B",
"rationale": {{"A": "Choice A is incorrect because ...", "B": "Choice B is the best answer because ...", "C": "...", "D": "..."}}}}]}}.
Split the explanation into one entry per choice, copying the official wording. For
student-produced response questions, give the answer value(s) separated by "|" and
"rationale": {{"correct": "..."}}.

TEXT:
{text}"""


def extract_pages(pdf_path: Path) -> list[str]:
    try:
        import pymupdf
    except ImportError as e:  # pragma: no cover
        raise SystemExit("PDF import needs PyMuPDF: pip install -e '.[pdf]'") from e
    with pymupdf.open(pdf_path) as doc:
        return [page.get_text() for page in doc]


def chunk(pages: list[str], max_chars: int = 24000, overlap_pages: int = 3) -> list[str]:
    """Multi-page chunks that overlap, so a passage and its questions are never split
    between two chunks without also appearing whole in one of them."""
    out: list[str] = []
    i = 0
    while i < len(pages):
        j, size = i, 0
        while j < len(pages) and (size + len(pages[j]) <= max_chars or j == i):
            size += len(pages[j])
            j += 1
        out.append("\n\n".join(pages[i:j]))
        if j >= len(pages):
            break
        i = max(i + 1, j - overlap_pages)
    return out


def group_key(path: Path) -> tuple[str, bool]:
    """('satpracticetest1', is_answer_file) from 'sat-practice-test-1-answers.pdf'."""
    words = re.findall(r"[a-z]+|\d+", path.stem.lower())
    is_answer = any(w.startswith(a) for w in words for a in ANSWER_WORDS)
    kept = [w for w in words if not any(w.startswith(a) for a in ANSWER_WORDS) and w not in ("and", "for")]
    return "".join(kept), is_answer


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()[:120]


@dataclass
class _Import:
    passages: dict[str, dict] = field(default_factory=dict)  # normalized prefix -> passage
    questions: dict[tuple[str, int], dict] = field(default_factory=dict)  # (section_key, number) -> item
    answers: dict[tuple[str, int], dict] = field(default_factory=dict)


def _key(it: dict) -> tuple[str, int] | None:
    try:
        return str(it.get("section_key", "")).strip().lower(), int(it.get("number"))
    except (TypeError, ValueError):
        return None


def read_questions(llm: LLM, pages: list[str], acc: _Import) -> None:
    last_passage: str | None = None
    for ch in chunk(pages):
        try:
            data = llm.generate_json(SYSTEM, QUESTIONS_PROMPT.format(sections=SECTION_LIST, skills=SKILL_LIST, text=ch))
        except LLMError as e:
            print(f"  chunk skipped: {e}")
            continue
        if not isinstance(data, dict):
            continue
        refs: dict[str, str | None] = {}
        for p in data.get("passages") or []:
            text = str(p.get("text") or "").strip()
            if len(text.split()) < 80 or p.get("complete") is False:
                refs[str(p.get("ref"))] = None  # questions on an incomplete passage wait for the next chunk
                continue
            k = _norm(text)
            acc.passages.setdefault(k, {**p, "text": text})
            refs[str(p.get("ref"))] = k
            last_passage = k
        for it in data.get("items") or []:
            key = _key(it)
            if key is None or key in acc.questions:
                continue
            ref = it.get("passage_ref")
            if ref == "previous":
                it["_passage"] = last_passage
            elif ref:
                if refs.get(str(ref)) is None:
                    continue
                it["_passage"] = refs[str(ref)]
            if ref and it.get("_passage") is None:
                continue
            acc.questions[key] = it


def read_answers(llm: LLM, pages: list[str], acc: _Import) -> None:
    for ch in chunk(pages):
        try:
            data = llm.generate_json(SYSTEM, ANSWERS_PROMPT.format(sections=SECTION_LIST, text=ch))
        except LLMError as e:
            print(f"  chunk skipped: {e}")
            continue
        for a in (data.get("answers") if isinstance(data, dict) else None) or []:
            key = _key(a)
            if key is not None and str(a.get("answer") or "").strip():
                acc.answers.setdefault(key, a)


def save(acc: _Import, source_name: str) -> dict[str, int]:
    """Write the matched questions (and the passages they use) to the database."""
    stats = {"questions": 0, "passages": 0, "no_answer": 0, "skipped": 0}
    with session() as s:
        passage_ids: dict[str, int] = {}
        existing = {(q.stem, q.excerpt or "", q.passage_id) for q in s.exec(select(Question)).all()}
        for key, it in sorted(acc.questions.items()):
            sk = SKILL_BY_ID.get(str(it.get("skill")))
            ans = acc.answers.get(key) or {}
            answer = str(ans.get("answer") or it.get("answer") or "").strip()
            qtype = "spr" if it.get("qtype") == "spr" else "mcq"
            choices = [str(c) for c in (it.get("choices") or [])] if qtype == "mcq" else []
            if not answer:
                stats["no_answer"] += 1
                continue
            if not sk or not it.get("stem") or (qtype == "mcq" and (len(choices) != 4 or answer.upper()[:1] not in "ABCD")):
                stats["skipped"] += 1
                continue
            pid = None
            if it.get("_passage"):
                pk = it["_passage"]
                if pk not in passage_ids:
                    p = acc.passages[pk]
                    title = str(p.get("title") or f"{source_name} passage")
                    row = s.exec(select(Passage).where(Passage.title == title, Passage.origin == "official")).first()
                    if row is None or _norm(row.text) != pk:
                        row = Passage(title=title, author=str(p.get("author") or "Unknown"), year=str(p.get("year") or "") or None,
                                      genre="official", text=p["text"], source_url="", license=PRIVATE_LICENSE, origin="official")
                        s.add(row)
                        s.commit()
                        s.refresh(row)
                        stats["passages"] += 1
                    passage_ids[pk] = row.id
                pid = passage_ids[pk]
            stem = str(it["stem"]).strip()
            excerpt = it.get("excerpt") or None
            if (stem, excerpt or "", pid) in existing:
                continue
            s.add(Question(
                section=sk.section, domain=sk.domain, skill=sk.id,
                difficulty=it.get("difficulty") if it.get("difficulty") in ("easy", "medium", "hard") else "medium",
                qtype=qtype, stem=stem, choices=choices,
                answer=answer.upper()[:1] if qtype == "mcq" else answer,
                rationale=ans.get("rationale") or {}, excerpt=excerpt, passage_id=pid, origin="official",
            ))
            existing.add((stem, excerpt or "", pid))
            stats["questions"] += 1
        s.commit()
    return stats


def import_folder(llm: LLM, folder: Path) -> dict[str, dict[str, int]]:
    """Import every PDF in a folder, pairing tests with their answer files by name."""
    groups: dict[str, dict[str, list[Path]]] = {}
    for pdf in sorted(folder.glob("*.pdf")):
        key, is_answer = group_key(pdf)
        groups.setdefault(key, {"q": [], "a": []})["a" if is_answer else "q"].append(pdf)
    results = {}
    for key, files in groups.items():
        acc = _Import()
        for pdf in files["q"]:
            print(f"  reading questions: {pdf.name}")
            read_questions(llm, extract_pages(pdf), acc)
        for pdf in files["a"]:
            print(f"  reading answers:   {pdf.name}")
            read_answers(llm, extract_pages(pdf), acc)
        name = (files["q"] or files["a"])[0].stem
        results[name] = save(acc, name)
    return results


def dump_debug(pdf_path: Path, out: Path) -> None:
    """Write extracted text to a file, to check what PyMuPDF sees in a given PDF."""
    out.write_text(json.dumps(extract_pages(pdf_path), indent=1), encoding="utf-8")
