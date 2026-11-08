from pathlib import Path

import pymupdf

from sat_coach.db import Passage, Question, select, session
from sat_coach.ingest.pdf_import import chunk, group_key, import_folder

from .conftest import FakeLLM

PASSAGE = " ".join(["The river carried silt from the mountains to the delta each spring."] * 12)


def make_pdf(path: Path, pages: list[str]) -> None:
    doc = pymupdf.open()
    for text in pages:
        doc.new_page().insert_text((50, 72), text)
    doc.save(path)


def q(number, skill="words_in_context", ref="p1", **kw):
    return {"section_key": "reading", "number": number, "passage_ref": ref, "skill": skill, "difficulty": "medium",
            "qtype": "mcq", "excerpt": None, "stem": f"Question {number}?", "choices": ["w", "x", "y", "z"], "answer": None, **kw}


def writer(prompt):
    if "answer explanations document" in prompt:
        return {"answers": [{"section_key": "reading", "number": n, "answer": "B",
                             "rationale": {L: f"Choice {L} ..." for L in "ABCD"}} for n in range(1, 8)]}
    if "page two" in prompt:
        # A later chunk: questions continue the passage from the previous chunk; one passage is cut off.
        return {"passages": [{"ref": "p9", "title": "Cut", "author": "X", "text": PASSAGE + " more", "complete": False}],
                "items": [q(6, ref="previous"), q(7, ref="p9")]}
    return {"passages": [{"ref": "p1", "title": "Delta Life", "author": "A. Author", "year": "2015", "text": PASSAGE}],
            "items": [q(1), q(2, "central_ideas"), q(3, "inferences"), q(4, "text_structure_purpose"), q(5, "transitions"),
                      q(99, stem="")]}


def test_group_key_pairs_tests_with_answer_files():
    assert group_key(Path("SAT Practice Test 1.pdf")) == ("satpracticetest1", False)
    assert group_key(Path("sat-practice-test-1-answers.pdf")) == ("satpracticetest1", True)
    assert group_key(Path("SAT Practice Test 1 Answer Explanations.pdf"))[0] == "satpracticetest1"
    assert group_key(Path("sat-practice-test-10-scoring.pdf")) == ("satpracticetest10", True)


def test_chunks_overlap_and_cover_everything():
    pages = [f"page{i} " + "x" * 5000 for i in range(12)]
    chunks = chunk(pages, max_chars=24000, overlap_pages=3)
    assert len(chunks) > 1 and "page0" in chunks[0] and "page11" in chunks[-1]
    assert "page3 " in chunks[0] and "page3 " in chunks[1]  # overlap


def test_import_matches_answers_and_builds_article_sets(tmp_path):
    folder = tmp_path / "pdfs"
    folder.mkdir()
    make_pdf(folder / "sat-practice-test-1.pdf", ["page one", "page two"])
    make_pdf(folder / "sat-practice-test-1-answers.pdf", ["answers"])
    llm = FakeLLM(writer, lambda p: {})
    # chunk() would put both tiny pages in one chunk; force one chunk per page for this test
    import sat_coach.ingest.pdf_import as imp
    orig = imp.chunk
    imp.chunk = lambda pages, **kw: list(pages)
    try:
        res = import_folder(llm, folder)
    finally:
        imp.chunk = orig
    st = res["sat-practice-test-1"]
    assert st["questions"] == 6  # 1-5 plus 6 ("previous" passage); 7 is on an incomplete passage; 99 has no stem
    assert st["passages"] == 1
    with session() as s:
        p = s.exec(select(Passage).where(Passage.origin == "official")).one()
        assert p.title == "Delta Life" and "do not redistribute" in p.license
        qs = s.exec(select(Question).where(Question.origin == "official")).all()
        assert {x.passage_id for x in qs} == {p.id}
        assert all(x.answer == "B" and x.rationale["B"] for x in qs)
    # Re-importing doesn't duplicate anything
    imp.chunk = lambda pages, **kw: list(pages)
    try:
        assert import_folder(llm, folder)["sat-practice-test-1"]["questions"] == 0
    finally:
        imp.chunk = orig


def test_questions_without_answers_are_skipped(tmp_path):
    folder = tmp_path / "pdfs"
    folder.mkdir()
    make_pdf(folder / "test-2.pdf", ["page one"])
    res = import_folder(FakeLLM(writer, lambda p: {}), folder)
    assert res["test-2"]["questions"] == 0 and res["test-2"]["no_answer"] == 6
