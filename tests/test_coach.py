import random

from sat_coach.coach import mastery, review, scoring, selector
from sat_coach.db import ReviewItem, SkillState, session
from sat_coach.taxonomy import MATH_DOMAINS, RW_DOMAINS, SKILL_BY_ID, SKILLS


def states(**overrides):
    out = {s.id: SkillState(user_id=1, skill=s.id) for s in SKILLS}
    for k, (theta, attempts, correct) in overrides.items():
        out[k].theta, out[k].attempts, out[k].correct, out[k].unc = theta, attempts, correct, 0.5
    return out


def test_mastery_moves_toward_evidence():
    st = SkillState(user_id=1, skill="percentages")
    mastery.update(st, "medium", True)
    assert st.theta > 0 and st.unc < 1
    t = st.theta
    mastery.update(st, "easy", False)
    assert st.theta < t
    # A miss on a hard item costs less than a miss on an easy one.
    a, b = SkillState(user_id=1, skill="x"), SkillState(user_id=1, skill="x")
    mastery.update(a, "hard", False)
    mastery.update(b, "easy", False)
    assert a.theta > b.theta


def test_score_bounds_and_monotonic():
    lo = scoring.projected({k: SkillState(user_id=1, skill=k, theta=-3) for k in SKILL_BY_ID})
    mid = scoring.projected({k: SkillState(user_id=1, skill=k, theta=0) for k in SKILL_BY_ID})
    hi = scoring.projected({k: SkillState(user_id=1, skill=k, theta=3) for k in SKILL_BY_ID})
    assert 400 <= lo["total"] < mid["total"] < hi["total"] <= 1600
    assert mid["math"] == 500 and mid["rw"] == 500
    # Band narrows with evidence
    sure = {k: SkillState(user_id=1, skill=k, theta=0, unc=0.25, attempts=10) for k in SKILL_BY_ID}
    assert scoring.projected(sure)["band"] < mid["band"]


def test_baseline_covers_every_domain():
    bp = selector.baseline(random.Random(1))
    assert len(bp.math) == 8
    assert {SKILL_BY_ID[s.skill].domain for s in bp.math} == set(MATH_DOMAINS)
    assert sorted(s.difficulty for s in bp.math).count("hard") == 2
    assert len(bp.rw_skills) == 5
    assert set(RW_DOMAINS) <= {SKILL_BY_ID[s].domain for s in bp.rw_skills}
    assert all(SKILL_BY_ID[s].article_ok for s in bp.rw_skills)


def test_adaptive_targets_weak_skills_and_review():
    st = states(circles=(-1.5, 4, 0), percentages=(1.5, 4, 4), systems_linear=(-0.8, 3, 1))
    due = [ReviewItem(user_id=1, question_id=99, box=1, due_session=1)]
    bp = selector.adaptive(st, due, {99: ("math", "probability", "medium")}, random.Random(0))
    skills = [s.skill for s in bp.math]
    assert len(bp.math) == 8
    assert "circles" in skills and "systems_linear" in skills
    assert any(s.reason == "review" and s.review_question_id == 99 for s in bp.math)
    assert {SKILL_BY_ID[s].domain for s in skills} == set(MATH_DOMAINS)
    weak = [s for s in bp.math if s.skill == "circles"][0]
    assert weak.difficulty in ("easy", "medium")  # pitched below a hard item for a struggling student


def test_review_leitner_cycle():
    with session() as s:
        review.record(s, 1, 10, False, sessions_done=1)  # missed in session 1
        s.commit()
        assert [r.question_id for r in review.due(s, 1, 0)] == []
        assert [r.question_id for r in review.due(s, 1, 1)] == [10]  # comes back in session 2
        review.record(s, 1, 10, True, sessions_done=2)  # box 2: skip one session
        s.commit()
        assert review.due(s, 1, 2) == []
        assert len(review.due(s, 1, 3)) == 1
        review.record(s, 1, 10, False, sessions_done=4)  # missed again: back to box 1
        s.commit()
        item = review.due(s, 1, 4)[0]
        assert item.box == 1 and item.times_missed == 2
        for n in range(5, 9):
            review.record(s, 1, 10, True, sessions_done=n)
        s.commit()
        assert review.due(s, 1, 100) == []  # retired after clearing box 4
        review.record(s, 1, 11, True, sessions_done=1)  # a correct first answer isn't queued
        s.commit()
        assert all(r.question_id != 11 for r in review.due(s, 1, 100))
