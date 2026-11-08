"""Decides what each session contains (the "blueprint"), before any question is fetched.

Baseline session: 8 math questions, 2 per domain (3 easy / 3 medium / 2 hard) plus one
article with 5 questions spanning all four R&W domains.

Adaptive sessions (math): ~1 spaced-review slot, ~5 slots on the weakest skills, and the
rest for coverage of domains not yet in the session. R&W: the weakest article-compatible
skill in each domain, plus the weakest remaining skill, plus a due R&W review item if any.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from ..db import ReviewItem, SkillState
from ..taxonomy import MATH_DOMAINS, RW_DOMAINS, skills_in
from .mastery import target_difficulty

MATH_COUNT = 8
RW_COUNT = 5
WEAK_SHARE = 0.6
REVIEW_MATH_MAX = 1
REVIEW_RW_MAX = 1


@dataclass
class Slot:
    skill: str
    difficulty: str
    reason: str  # baseline | weak | coverage | review
    review_question_id: int | None = None


@dataclass
class Blueprint:
    kind: str
    math: list[Slot] = field(default_factory=list)
    rw_skills: list[str] = field(default_factory=list)
    rw_difficulties: list[str] = field(default_factory=list)
    rw_review: list[int] = field(default_factory=list)  # question ids


def weakness(st: SkillState) -> float:
    """Higher = more in need of practice. Low ability and high uncertainty both count."""
    err = 1 - (st.correct / st.attempts) if st.attempts else 0.5
    return -st.theta + 0.6 * st.unc + 0.5 * err


def baseline(rng: random.Random) -> Blueprint:
    bp = Blueprint(kind="baseline")
    plan = {"algebra": ["easy", "medium"], "advanced_math": ["easy", "hard"], "psda": ["easy", "medium"],
            "geometry_trig": ["medium", "hard"]}
    for dom, diffs in plan.items():
        for sk, d in zip(rng.sample(skills_in("math", dom), 2), diffs):
            bp.math.append(Slot(sk.id, d, "baseline"))
    order = {"easy": 0, "medium": 1, "hard": 2}
    bp.math.sort(key=lambda s: order[s.difficulty])  # ramp up, like a test module
    picks = [rng.choice(skills_in("rw", dom, article_only=True)).id for dom in RW_DOMAINS]
    extra = [s.id for s in skills_in("rw", article_only=True) if s.id not in picks]
    picks.append(rng.choice(extra))
    bp.rw_skills = picks
    bp.rw_difficulties = ["easy", "medium", "medium", "hard", "medium"]
    return bp


def adaptive(states: dict[str, SkillState], due: list[ReviewItem], review_sections: dict[int, tuple[str, str, str]],
             rng: random.Random) -> Blueprint:
    """`review_sections` maps question_id -> (section, skill, difficulty) for due review items."""
    bp = Blueprint(kind="adaptive")
    used: set[str] = set()

    # 1. Spaced review
    math_due = [r for r in due if review_sections.get(r.question_id, ("",))[0] == "math"][:REVIEW_MATH_MAX]
    for r in math_due:
        _, skill, diff = review_sections[r.question_id]
        bp.math.append(Slot(skill, diff, "review", review_question_id=r.question_id))
        used.add(skill)
    bp.rw_review = [r.question_id for r in due if review_sections.get(r.question_id, ("",))[0] == "rw"][:REVIEW_RW_MAX]

    # 2. Weakest skills (only ones we have evidence about; untested skills come in via coverage)
    math_states = [states[s.id] for s in skills_in("math")]
    tested = sorted([st for st in math_states if st.attempts > 0], key=weakness, reverse=True)
    n_weak = round(MATH_COUNT * WEAK_SHARE)
    for st in tested:
        if len([s for s in bp.math if s.reason == "weak"]) >= n_weak:
            break
        if st.skill in used:
            continue
        bp.math.append(Slot(st.skill, target_difficulty(st.theta), "weak"))
        used.add(st.skill)
    # The weakest skill can take a second slot if we ran out of distinct tested skills.
    while tested and len([s for s in bp.math if s.reason == "weak"]) < min(n_weak, len(tested) + 1):
        st = tested[0]
        bp.math.append(Slot(st.skill, target_difficulty(st.theta), "weak"))

    # 3. Coverage: domains missing from this session first, then least-practiced skills.
    while len(bp.math) < MATH_COUNT:
        present = {_domain(s.skill) for s in bp.math}
        missing = [d for d in MATH_DOMAINS if d not in present] or list(MATH_DOMAINS)
        dom = missing[0] if len(missing) < len(MATH_DOMAINS) else rng.choice(missing)
        cands = [states[s.id] for s in skills_in("math", dom) if s.id not in used] or [states[s.id] for s in skills_in("math", dom)]
        st = min(cands, key=lambda c: (c.attempts, rng.random()))
        bp.math.append(Slot(st.skill, target_difficulty(st.theta), "coverage"))
        used.add(st.skill)

    order = {"easy": 0, "medium": 1, "hard": 2}
    bp.math.sort(key=lambda s: order[s.difficulty])

    # R&W: weakest article-compatible skill per domain + weakest remaining.
    picks = []
    for dom in RW_DOMAINS:
        cands = [states[s.id] for s in skills_in("rw", dom, article_only=True)]
        picks.append(max(cands, key=lambda c: (weakness(c), rng.random())).skill)
    rest = [states[s.id] for s in skills_in("rw", article_only=True) if s.id not in picks]
    picks.append(max(rest, key=lambda c: (weakness(c), rng.random())).skill)
    bp.rw_skills = picks[:RW_COUNT]
    bp.rw_difficulties = [target_difficulty(states[s].theta) for s in bp.rw_skills]
    return bp


def _domain(skill: str) -> str:
    from ..taxonomy import SKILL_BY_ID

    return SKILL_BY_ID[skill].domain
