from sat_coach.coach import sessions as svc
from sat_coach.db import Passage, Question, ReviewItem, SkillState, select, session
from sat_coach.gen.generate import generate_math
from sat_coach.llm import set_llm

from .conftest import FakeLLM, make_user


def wrong_answers(data):
    out = {}
    for q in data["questions"]:
        with session() as s:
            key = s.get(Question, q["id"]).answer
        out[q["id"]] = {"response": "A" if key != "A" else "B", "seconds": 30} if q["qtype"] == "mcq" else {"response": "-999"}
    return out


def right_answers(data):
    out = {}
    with session() as s:
        for q in data["questions"]:
            out[q["id"]] = {"response": s.get(Question, q["id"]).answer.split("|")[0]}
    return out


def test_offline_baseline_session_shape_and_no_answer_leak():
    uid = make_user()
    sid = svc.start_session(uid)
    data = svc.load_session(sid, uid, reveal=False)
    math = [q for q in data["questions"] if q["section"] == "math"]
    rw = [q for q in data["questions"] if q["section"] == "rw"]
    assert data["kind"] == "baseline"
    assert len(math) == 8 and len(rw) == 5
    assert len({q["passage_id"] for q in rw}) == 1  # one article
    assert len({q["domain"] for q in math}) == 4
    for q in data["questions"]:
        assert "answer" not in q and "rationale" not in q
    # Resuming returns the same open session
    assert svc.start_session(uid) == sid


def test_blank_items_mask_the_article():
    text = "It is fitting. But, in a larger sense, we can not dedicate this ground. More text."
    excerpt = "It is fitting. ______ in a larger sense, we can not dedicate this ground."
    masked = svc.mask_passage(text, excerpt)
    assert "But," not in masked and "______" in masked and masked.endswith("More text.")
    assert svc.mask_passage(text, "no blank here") is None


def test_submit_updates_model_and_queues_review():
    uid = make_user()
    sid = svc.start_session(uid)
    data = svc.load_session(sid, uid, reveal=False)
    res = svc.submit_session(sid, uid, wrong_answers(data))
    assert res["completed"] and all(not q["correct"] for q in res["questions"])
    assert all("rationale" in q for q in res["questions"])
    assert res["projection"]["total"] < 1000
    with session() as s:
        queue = s.exec(select(ReviewItem).where(ReviewItem.user_id == uid)).all()
        assert len(queue) == len(res["questions"])
    # Next session is adaptive and pulls in due review items.
    sid2 = svc.start_session(uid)
    d2 = svc.load_session(sid2, uid, reveal=False)
    assert d2["kind"] == "adaptive"
    assert any(q["reason"] == "review" for q in d2["questions"])
    assert any(q["reason"] == "weak" for q in d2["questions"])
    seen_before = {q["id"] for q in data["questions"] if q["reason"] != "review"}
    fresh = [q for q in d2["questions"] if q["reason"] not in ("review",) and q["section"] == "math"]
    assert not ({q["id"] for q in fresh} & seen_before)


def test_users_are_isolated():
    a, b = make_user("A"), make_user("B")
    sid = svc.start_session(a)
    svc.submit_session(sid, a, right_answers(svc.load_session(sid, a, reveal=False)))
    with session() as s:
        assert s.exec(select(SkillState).where(SkillState.user_id == b, SkillState.attempts > 0)).all() == []
    assert svc.progress(b)["history"] == []
    assert svc.progress(a)["projection"]["total"] > svc.progress(b)["projection"]["total"]
    try:
        svc.load_session(sid, b, reveal=True)
        raise AssertionError("user B could read user A's session")
    except KeyError:
        pass


def _item(answer_letter, x):
    choices = [str(x - 1), str(x), str(x + 1), str(2 * x)]
    return {"qtype": "mcq", "stem": f"If 2x = {2 * x}, what is x?", "choices": choices, "answer": answer_letter,
            "rationale": {L: "Choice ..." for L in "ABCD"}, "check": {"equations": [f"2*x = {2 * x}"], "solve_for": "x"}}


def test_generation_accepts_valid_and_rejects_wrong_key():
    # Slot 1: correct key (B = x). Slot 2: wrong key (C), which the sympy check rejects.
    writer = lambda p: {"items": [_item("B", 5), _item("C", 8)]}
    solver = lambda p: {"answers": ["B"]}
    llm = FakeLLM(writer, solver)
    out = generate_math(llm, [("linear_eq_one_var", "easy"), ("linear_eq_one_var", "medium")], rounds=1)
    assert out[0] is not None and out[0].origin == "generated"
    assert out[1] is None


def test_generation_rejects_when_blind_solver_disagrees():
    llm = FakeLLM(lambda p: {"items": [_item("B", 5)]}, lambda p: {"answers": ["D"]})
    assert generate_math(llm, [("linear_eq_one_var", "easy")], rounds=1) == [None]


def test_session_generates_when_bank_runs_out():
    uid = make_user()
    with session() as s:  # empty the math bank so every slot must be generated
        for q in s.exec(select(Question).where(Question.section == "math")).all():
            s.delete(q)
        s.commit()
    llm = FakeLLM(lambda p: {"items": [_item("B", n) for n in range(2, 10)]}, lambda p: {"answers": ["B"] * 8})
    set_llm(llm)
    data = svc.load_session(svc.start_session(uid), uid, reveal=False)
    assert len([q for q in data["questions"] if q["section"] == "math"]) == 8
    assert len([c for c in llm.calls if c[0] == "write"]) == 1  # batched: one writer call for 8 slots


def test_rw_generation_for_fresh_passage():
    uid = make_user()
    with session() as s:
        p = Passage(title="Test Article", author="A. Writer", genre="science", text="Bees dance. " * 60, origin="fetched")
        s.add(p)
        for q in s.exec(select(Question).where(Question.section == "rw")).all():
            s.delete(q)
        s.commit()
        for old in s.exec(select(Passage).where(Passage.origin == "seed")).all():
            s.delete(old)
        s.commit()

    def writer(prompt):
        return {"items": [{"skill": "x", "excerpt": "Bees dance.", "stem": f"Q{i}?", "choices": ["w", "x", "y", "z"],
                           "answer": "A", "rationale": {L: "..." for L in "ABCD"}} for i in range(5)]}

    set_llm(FakeLLM(writer, lambda p: {"answers": ["A"] * 5}))
    data = svc.load_session(svc.start_session(uid), uid, reveal=False)
    rw = [q for q in data["questions"] if q["section"] == "rw"]
    assert len(rw) == 5 and data["passages"][rw[0]["passage_id"]]["title"] == "Test Article"


def test_plan_marks_bank_slots_and_matches_real_session():
    uid = make_user()
    plan = svc.plan_session(uid, llm_available=False)
    assert plan.kind == "baseline"
    assert all(m.source == "bank" and m.origin == "seed" for m in plan.math)
    assert plan.rw_source == "bank" and len(plan.rw) == 5
    planned = [m.question_id for m in plan.math + plan.rw]
    sid = svc.start_session(uid)
    data = svc.load_session(sid, uid, reveal=False)
    assert [q["id"] for q in data["questions"]] == planned  # preview == what actually happened
    assert all(q["origin"] == "seed" and q["source"] == "bank" for q in data["questions"])
    assert all("answer" not in q for q in data["questions"])


def test_plan_reports_generation_and_session_records_it():
    uid = make_user()
    with session() as s:
        for q in s.exec(select(Question).where(Question.section == "math")).all():
            s.delete(q)
        s.commit()
    plan = svc.plan_session(uid, llm_available=True)
    assert [m.source for m in plan.math] == ["generate"] * 8
    with session() as s:  # planning makes no writes
        assert s.exec(select(SkillState).where(SkillState.user_id == uid)).all() == []
    set_llm(FakeLLM(lambda p: {"items": [_item("B", n) for n in range(2, 10)]}, lambda p: {"answers": ["B"] * 8}))
    data = svc.load_session(svc.start_session(uid), uid, reveal=False)
    math = [q for q in data["questions"] if q["section"] == "math"]
    assert len(math) == 8
    assert all(q["source"] == "generated" and q["origin"] == "generated" for q in math)


def test_offline_plan_uses_fallback_when_bank_is_exhausted():
    uid = make_user()
    with session() as s:
        for q in s.exec(select(Question).where(Question.section == "math", Question.skill != "percentages")).all():
            s.delete(q)
        s.commit()
    plan = svc.plan_session(uid, llm_available=False)
    assert any(m.source == "fallback" for m in plan.math)
    assert not any(m.source == "generate" for m in plan.math)
