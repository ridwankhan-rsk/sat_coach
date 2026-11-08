"""Session lifecycle: build a session from a blueprint, grade a submission, update the
student's model (mastery, review queue, projected score)."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlmodel import Session, func, select

from ..db import Attempt, Passage, PracticeSession, Question, ReviewItem, SessionItem, session
from ..gen.generate import generate_math, generate_rw_set
from ..gen.validate import grade
from ..llm import get_llm
from ..taxonomy import SKILL_BY_ID, skills_in
from . import mastery, review, scoring, selector

log = logging.getLogger(__name__)


def sessions_done(s: Session, user_id: int) -> int:
    return s.exec(
        select(func.count()).select_from(PracticeSession).where(
            PracticeSession.user_id == user_id, PracticeSession.completed_at != None  # noqa: E711
        )
    ).one()


def _seen_ids(s: Session, user_id: int) -> set[int]:
    rows = s.exec(
        select(SessionItem.question_id).join(PracticeSession, PracticeSession.id == SessionItem.session_id)
        .where(PracticeSession.user_id == user_id)
    ).all()
    return set(rows)


def _pick_from_bank(s: Session, skill: str, difficulty: str, exclude: set[int], rng: random.Random) -> Question | None:
    base = select(Question).where(Question.section == "math", Question.skill == skill).order_by(Question.id)
    rows = [q for q in s.exec(base).all() if q.id not in exclude]
    exact = [q for q in rows if q.difficulty == difficulty]
    pool = exact or rows
    return rng.choice(pool) if pool else None


def _fallback_math(s: Session, skill: str, exclude: set[int], seen: set[int], rng: random.Random) -> Question | None:
    """No unseen item and no generator: same domain unseen, then any unseen, then a repeat."""
    dom = SKILL_BY_ID[skill].domain
    all_math = s.exec(select(Question).where(Question.section == "math").order_by(Question.id)).all()
    for pool in (
        [q for q in all_math if q.domain == dom and q.id not in exclude],
        [q for q in all_math if q.id not in exclude],
        [q for q in all_math if q.skill == skill and q.id not in exclude - seen],
    ):
        if pool:
            return rng.choice(pool)
    return None


def open_session(user_id: int) -> PracticeSession | None:
    with session() as s:
        return s.exec(
            select(PracticeSession).where(PracticeSession.user_id == user_id, PracticeSession.completed_at == None)  # noqa: E711
            .order_by(PracticeSession.id.desc())
        ).first()


# ---------------------------------------------------------------- planning
# A session is built in two steps. plan_session() decides, for every slot, where the
# question will come from (question bank, LLM generation, or a fallback repeat) without
# calling the LLM or writing anything; start_session() then carries out that plan.
# `sat-coach preview` prints the plan, so you can see in advance what will be generated.


@dataclass
class PlannedItem:
    section: str
    skill: str
    difficulty: str
    reason: str  # baseline | weak | coverage | review
    source: str  # bank | generate | variant | fallback | review
    question_id: int | None = None  # set for bank / fallback / review (and the original, for variant)
    origin: str | None = None  # origin of question_id: seed | official | generated


@dataclass
class SessionPlan:
    kind: str
    math: list[PlannedItem] = field(default_factory=list)
    rw: list[PlannedItem] = field(default_factory=list)
    rw_source: str = "none"  # bank | generate | fallback | none
    passage_id: int | None = None
    passage_title: str | None = None
    rw_skills: list[str] = field(default_factory=list)
    rw_difficulties: list[str] = field(default_factory=list)


def _session_rng(user_id: int, done: int) -> random.Random:
    """Deterministic per (user, session number), so a preview matches the real session."""
    return random.Random(f"{user_id}:{done}")


def plan_session(user_id: int, llm_available: bool | None = None) -> SessionPlan:
    if llm_available is None:
        llm_available = get_llm() is not None
    with session() as s:
        done = sessions_done(s, user_id)
        rng = _session_rng(user_id, done)
        states = mastery.get_states(s, user_id)
        if done == 0:
            bp = selector.baseline(rng)
        else:
            due = review.due(s, user_id, done)
            meta = {}
            for r in due:
                q = s.get(Question, r.question_id)
                if q:
                    meta[q.id] = (q.section, q.skill, q.difficulty)
            bp = selector.adaptive(states, due, meta, rng)
        s.rollback()  # get_states may have created rows; planning writes nothing
        seen = _seen_ids(s, user_id)
        plan = SessionPlan(kind=bp.kind)
        exclude = set(seen)

        # ---- Math
        pending_fallback: list[PlannedItem] = []
        for slot in bp.math:
            item = PlannedItem("math", slot.skill, slot.difficulty, slot.reason, "bank")
            if slot.reason == "review" and slot.review_question_id:
                orig = s.get(Question, slot.review_question_id)
                if orig is None:
                    continue
                ri = s.exec(select(ReviewItem).where(ReviewItem.user_id == user_id,
                                                     ReviewItem.question_id == slot.review_question_id)).first()
                item.question_id, item.origin = orig.id, orig.origin
                item.source = "variant" if (llm_available and ri and ri.box >= 2) else "review"
                exclude.add(orig.id)
            else:
                q = _pick_from_bank(s, slot.skill, slot.difficulty, exclude, rng)
                if q is not None:
                    item.question_id, item.origin = q.id, q.origin
                    exclude.add(q.id)
                elif llm_available:
                    item.source = "generate"
                else:
                    item.source = "fallback"
                    pending_fallback.append(item)
            plan.math.append(item)
        for item in pending_fallback:
            q = _fallback_math(s, item.skill, exclude, seen, rng)
            if q is not None:
                item.question_id, item.origin = q.id, q.origin
                exclude.add(q.id)
        plan.math = [m for m in plan.math if m.source in ("generate", "variant") or m.question_id is not None]

        # ---- Reading & Writing: one article and its question set
        plan.rw_skills, plan.rw_difficulties = list(bp.rw_skills), list(bp.rw_difficulties)
        passages = s.exec(select(Passage).order_by(Passage.id)).all()
        rw_qs = s.exec(select(Question).where(Question.section == "rw").order_by(Question.id)).all()
        used_passages = {q.passage_id for q in rw_qs if q.id in seen and q.passage_id}
        by_passage: dict[int, list[Question]] = {}
        for q in rw_qs:
            if q.passage_id:
                by_passage.setdefault(q.passage_id, []).append(q)
        fresh = [p for p in passages if p.id not in used_passages]
        reason = "baseline" if bp.kind == "baseline" else "weak"

        def pick_set(qs: list[Question]) -> list[Question]:
            # Prefer questions on the blueprint's skills, keeping the set to RW_COUNT.
            pref: list[Question] = []
            for sk in bp.rw_skills:
                m = next((q for q in qs if q.skill == sk and q not in pref), None)
                if m:
                    pref.append(m)
            return (pref + [q for q in qs if q not in pref])[: selector.RW_COUNT]

        def use_set(qs: list[Question], source: str) -> None:
            plan.rw_source = source
            plan.rw = [PlannedItem("rw", q.skill, q.difficulty, reason, source, q.id, q.origin) for q in qs]

        ready = [p for p in fresh if len([q for q in by_passage.get(p.id, []) if q.id not in seen]) >= 4]
        if ready:
            p = rng.choice(ready)
            plan.passage_id, plan.passage_title = p.id, p.title
            use_set(pick_set([q for q in by_passage[p.id] if q.id not in seen]), "bank")
        elif llm_available and fresh:
            p = min(fresh, key=lambda x: (len(by_passage.get(x.id, [])), rng.random()))
            plan.passage_id, plan.passage_title, plan.rw_source = p.id, p.title, "generate"
            plan.rw = [PlannedItem("rw", sk, d, reason, "generate") for sk, d in zip(bp.rw_skills, bp.rw_difficulties)]
        elif by_passage:
            pid = rng.choice(sorted(by_passage))
            plan.passage_id, plan.passage_title = pid, s.get(Passage, pid).title
            use_set(pick_set(by_passage[pid]), "fallback")

        for qid in bp.rw_review:
            q = s.get(Question, qid)
            if q and all(r.question_id != q.id for r in plan.rw):
                plan.rw.append(PlannedItem("rw", q.skill, q.difficulty, "review", "review", q.id, q.origin))
        return plan


def start_session(user_id: int) -> int:
    """Create (or resume) the user's next session by carrying out plan_session()."""
    existing = open_session(user_id)
    if existing:
        return existing.id
    llm = get_llm()
    plan = plan_session(user_id, llm_available=llm is not None)
    with session() as s:
        done = sessions_done(s, user_id)
        seen = _seen_ids(s, user_id)
    rng = _session_rng(user_id, done)
    exclude = set(seen) | {m.question_id for m in plan.math if m.question_id}

    # (question_id, reason, how it was sourced) in session order
    math_out: list[tuple[int | None, str, str]] = []
    gen_idx = [i for i, m in enumerate(plan.math) if m.source == "generate"]
    made = generate_math(llm, [(plan.math[i].skill, plan.math[i].difficulty) for i in gen_idx]) if gen_idx else []
    made_by_slot = dict(zip(gen_idx, made))
    for i, m in enumerate(plan.math):
        if m.source == "generate":
            q = made_by_slot.get(i)
            math_out.append((q.id, m.reason, "generated") if q else (None, m.reason, "fallback"))
        elif m.source == "variant":
            with session() as s:
                orig = s.get(Question, m.question_id)
            var = generate_math(llm, [(m.skill, m.difficulty)], variant_of=orig, rounds=1)[0]
            math_out.append((var.id, m.reason, "generated") if var else (orig.id, m.reason, "review"))
        else:
            math_out.append((m.question_id, m.reason, m.source))
    # Generation failures fall back to the bank.
    with session() as s:
        for i, (qid, reason, src) in enumerate(math_out):
            if qid is None:
                q = _fallback_math(s, plan.math[i].skill, exclude, seen, rng)
                if q is not None:
                    exclude.add(q.id)
                    math_out[i] = (q.id, reason, "fallback")

    rw_out: list[tuple[int | None, str, str]] = []
    if plan.rw_source == "generate":
        with session() as s:
            p = s.get(Passage, plan.passage_id)
        made_rw = generate_rw_set(llm, p, plan.rw_skills, plan.rw_difficulties)
        rw_out = [(q.id, it.reason, "generated") for q, it in zip(made_rw, plan.rw)]
        if not made_rw:  # generation failed: reuse an existing set, as the offline path does
            rw_out = _fallback_rw_set(plan, rng)
        rw_out += [(it.question_id, "review", "review") for it in plan.rw if it.source == "review"]
    else:
        rw_out = [(it.question_id, it.reason, it.source) for it in plan.rw]

    with session() as s:
        ps = PracticeSession(user_id=user_id, kind=plan.kind)
        s.add(ps)
        s.commit()
        s.refresh(ps)
        pos = 0
        for qid, reason, src in math_out + rw_out:
            if qid is None:
                continue
            s.add(SessionItem(session_id=ps.id, question_id=qid, position=pos, reason=reason, source=src))
            pos += 1
        s.commit()
        return ps.id


def _fallback_rw_set(plan: SessionPlan, rng: random.Random) -> list[tuple[int | None, str, str]]:
    with session() as s:
        rw_qs = s.exec(select(Question).where(Question.section == "rw", Question.passage_id != None).order_by(Question.id)).all()  # noqa: E711
    by_passage: dict[int, list[Question]] = {}
    for q in rw_qs:
        by_passage.setdefault(q.passage_id, []).append(q)
    if not by_passage:
        return []
    pid = rng.choice(sorted(by_passage))
    reason = "baseline" if plan.kind == "baseline" else "weak"
    return [(q.id, reason, "fallback") for q in by_passage[pid][: selector.RW_COUNT]]


BLANK = "______"


def mask_passage(text: str, excerpt: str | None) -> str | None:
    """For fill-in-the-blank items (transitions, boundaries, form/structure/sense) whose
    excerpt is a sentence from the article, blank out that spot in the article too, so the
    original wording doesn't give the answer away. Returns None if no masking applies."""
    if not excerpt or BLANK not in excerpt:
        return None
    before, after = excerpt.split(BLANK, 1)
    head = before[-40:].strip()
    tail = after[:40].strip()
    if not head and not tail:
        return None
    start = text.find(head) if head else -1
    if head and start == -1:
        return None
    gap_start = start + len(head) if head else 0
    end = text.find(tail, gap_start) if tail else gap_start
    if tail and (end == -1 or end - gap_start > 40):
        return None
    return text[:gap_start] + " " + BLANK + " " + text[end:]


def load_session(session_id: int, user_id: int, reveal: bool) -> dict:
    """Session payload for the UI. Answers and rationales only when reveal=True."""
    with session() as s:
        ps = s.get(PracticeSession, session_id)
        if ps is None or ps.user_id != user_id:
            raise KeyError("session not found")
        items = s.exec(select(SessionItem).where(SessionItem.session_id == session_id).order_by(SessionItem.position)).all()
        attempts = {a.question_id: a for a in s.exec(select(Attempt).where(Attempt.session_id == session_id)).all()}
        questions, passages = [], {}
        for it in items:
            q = s.get(Question, it.question_id)
            d = {
                "id": q.id, "section": q.section, "domain": q.domain, "skill": q.skill,
                "skill_name": SKILL_BY_ID[q.skill].name, "difficulty": q.difficulty, "qtype": q.qtype,
                "stem": q.stem, "choices": q.choices, "excerpt": q.excerpt, "passage_id": q.passage_id,
                "reason": it.reason, "source": it.source, "origin": q.origin, "parent_id": q.parent_id,
            }
            if q.passage_id:
                p = s.get(Passage, q.passage_id)
                if p.id not in passages:
                    passages[p.id] = {"id": p.id, "title": p.title, "author": p.author, "year": p.year, "text": p.text,
                                      "source_url": p.source_url, "license": p.license}
                if not reveal:
                    masked = mask_passage(p.text, q.excerpt)
                    if masked:
                        d["passage_text"] = masked
            if reveal:
                a = attempts.get(q.id)
                d.update(answer=q.answer, rationale=q.rationale, response=a.response if a else "",
                         correct=a.correct if a else False, attempt_id=a.id if a else None,
                         miss_reason=a.miss_reason if a else None)
            questions.append(d)
        return {
            "id": ps.id, "kind": ps.kind, "completed": ps.completed_at is not None,
            "questions": questions, "passages": passages,
            "score": {"math": ps.math_score, "rw": ps.rw_score, "total": ps.total_score, "band": ps.band}
            if ps.completed_at else None,
        }


def submit_session(session_id: int, user_id: int, answers: dict[int, dict]) -> dict:
    """Grade all answers, update mastery and review queue, compute projected score."""
    with session() as s:
        ps = s.get(PracticeSession, session_id)
        if ps is None or ps.user_id != user_id:
            raise KeyError("session not found")
        if ps.completed_at is not None:
            return load_session(session_id, user_id, reveal=True)
        done_before = sessions_done(s, user_id)
        prev = s.exec(
            select(PracticeSession).where(PracticeSession.user_id == user_id, PracticeSession.completed_at != None)  # noqa: E711
            .order_by(PracticeSession.completed_at.desc())
        ).first()
        states = mastery.get_states(s, user_id)
        items = s.exec(select(SessionItem).where(SessionItem.session_id == session_id)).all()
        for it in items:
            q = s.get(Question, it.question_id)
            a = answers.get(q.id) or answers.get(str(q.id)) or {}
            resp = str(a.get("response", "")).strip()
            ok = grade(q.qtype, q.answer, resp)
            s.add(Attempt(user_id=user_id, session_id=session_id, question_id=q.id, response=resp,
                          correct=ok, seconds=a.get("seconds")))
            mastery.update(states[q.skill], q.difficulty, ok)
            s.add(states[q.skill])
            # Review is keyed on the original question, so a correct variant advances its box.
            review.record(s, user_id, q.parent_id or q.id, ok, done_before + 1)
        proj = scoring.projected(states)
        ps.completed_at = datetime.now(timezone.utc)
        ps.math_score, ps.rw_score, ps.total_score, ps.band = proj["math"], proj["rw"], proj["total"], proj["band"]
        s.add(ps)
        s.commit()
        prev_total = prev.total_score if prev else None
    out = load_session(session_id, user_id, reveal=True)
    out["projection"] = proj
    out["previous_total"] = prev_total
    return out


def set_miss_reason(attempt_id: int, user_id: int, reason: str) -> None:
    if reason not in ("concept", "careless", "time"):
        raise ValueError("reason must be concept, careless or time")
    with session() as s:
        a = s.get(Attempt, attempt_id)
        if a is None or a.user_id != user_id:
            raise KeyError("attempt not found")
        a.miss_reason = reason
        s.add(a)
        s.commit()


def progress(user_id: int) -> dict:
    with session() as s:
        hist = s.exec(
            select(PracticeSession).where(PracticeSession.user_id == user_id, PracticeSession.completed_at != None)  # noqa: E711
            .order_by(PracticeSession.completed_at)
        ).all()
        states = mastery.get_states(s, user_id)
        s.commit()
        proj = scoring.projected(states)
        doms = {}
        for section in ("math", "rw"):
            for dom, (theta, unc) in scoring.domain_ability(states, section).items():
                doms[dom] = {"section": section, "theta": round(theta, 2), "unc": round(unc, 2),
                             "p_medium": round(mastery.p_correct(theta, "medium"), 2)}
        return {
            "history": [{"id": h.id, "date": h.completed_at.isoformat(), "kind": h.kind, "math": h.math_score,
                         "rw": h.rw_score, "total": h.total_score, "band": h.band} for h in hist],
            "projection": proj,
            "domains": doms,
        }


def insights(user_id: int) -> dict:
    """Backend view of historical difficulty: per-skill model, most-missed items, review queue."""
    with session() as s:
        states = mastery.get_states(s, user_id)
        s.commit()
        attempts = s.exec(select(Attempt).where(Attempt.user_id == user_id)).all()
        qs = {q.id: q for q in s.exec(select(Question)).all()}
        by_skill_diff: dict[tuple[str, str], list[bool]] = {}
        miss_count: dict[int, int] = {}
        reasons: dict[str, int] = {}
        for a in attempts:
            q = qs.get(a.question_id)
            if not q:
                continue
            by_skill_diff.setdefault((q.skill, q.difficulty), []).append(a.correct)
            if not a.correct:
                key = q.parent_id or q.id
                miss_count[key] = miss_count.get(key, 0) + 1
                if a.miss_reason:
                    reasons[a.miss_reason] = reasons.get(a.miss_reason, 0) + 1
        skills = []
        for section in ("math", "rw"):
            for sk in skills_in(section):
                st = states[sk.id]
                skills.append({
                    "skill": sk.id, "name": sk.name, "section": section, "domain": sk.domain,
                    "theta": round(st.theta, 2), "unc": round(st.unc, 2), "attempts": st.attempts,
                    "accuracy": round(st.correct / st.attempts, 2) if st.attempts else None,
                    "weakness": round(selector.weakness(st), 2),
                    "by_difficulty": {d: _acc(by_skill_diff.get((sk.id, d))) for d in ("easy", "medium", "hard")},
                })
        done = sessions_done(s, user_id)
        queue = s.exec(select(ReviewItem).where(ReviewItem.user_id == user_id, ReviewItem.retired == False)).all()  # noqa: E712
        hardest = sorted(miss_count.items(), key=lambda kv: -kv[1])[:10]
        return {
            "skills": skills,
            "hardest": [{"id": qid, "times_missed": n, "skill_name": SKILL_BY_ID[qs[qid].skill].name,
                         "stem": qs[qid].stem[:160]} for qid, n in hardest],
            "review_queue": [{"question_id": r.question_id, "box": r.box, "due_in_sessions": max(0, r.due_session - done),
                              "times_missed": r.times_missed, "skill_name": SKILL_BY_ID[qs[r.question_id].skill].name}
                             for r in queue if r.question_id in qs],
            "miss_reasons": reasons,
        }


def _acc(xs: list[bool] | None) -> dict | None:
    if not xs:
        return None
    return {"n": len(xs), "accuracy": round(sum(xs) / len(xs), 2)}
