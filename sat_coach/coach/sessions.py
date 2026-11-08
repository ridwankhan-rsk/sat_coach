"""Session lifecycle: build a session from a blueprint, grade a submission, update the
student's model (mastery, review queue, projected score)."""

from __future__ import annotations

import logging
import random
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


def _pick_from_bank(s: Session, skill: str, difficulty: str, exclude: set[int]) -> Question | None:
    base = select(Question).where(Question.section == "math", Question.skill == skill)
    rows = [q for q in s.exec(base).all() if q.id not in exclude]
    exact = [q for q in rows if q.difficulty == difficulty]
    pool = exact or rows
    return random.choice(pool) if pool else None


def _fallback_math(s: Session, skill: str, exclude: set[int], seen: set[int]) -> Question | None:
    """No unseen item and no generator: same domain unseen, then any unseen, then a repeat."""
    dom = SKILL_BY_ID[skill].domain
    all_math = s.exec(select(Question).where(Question.section == "math")).all()
    for pool in (
        [q for q in all_math if q.domain == dom and q.id not in exclude],
        [q for q in all_math if q.id not in exclude],
        [q for q in all_math if q.skill == skill and q.id not in exclude - seen],
    ):
        if pool:
            return random.choice(pool)
    return None


def open_session(user_id: int) -> PracticeSession | None:
    with session() as s:
        return s.exec(
            select(PracticeSession).where(PracticeSession.user_id == user_id, PracticeSession.completed_at == None)  # noqa: E711
            .order_by(PracticeSession.id.desc())
        ).first()


def start_session(user_id: int, rng: random.Random | None = None) -> int:
    """Create (or resume) the user's next session and return its id."""
    existing = open_session(user_id)
    if existing:
        return existing.id
    rng = rng or random.Random()
    llm = get_llm()
    with session() as s:
        done = sessions_done(s, user_id)
        states = mastery.get_states(s, user_id)
        s.commit()
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
        seen = _seen_ids(s, user_id)

    chosen: list[tuple[Question, str]] = []  # (question, reason)
    exclude = set(seen)

    # ---- Math
    need_gen: list[int] = []
    slots_q: list[Question | None] = []
    with session() as s:
        for i, slot in enumerate(bp.math):
            if slot.reason == "review" and slot.review_question_id:
                orig = s.get(Question, slot.review_question_id)
                slots_q.append(orig)
                continue
            q = _pick_from_bank(s, slot.skill, slot.difficulty, exclude)
            if q is not None:
                exclude.add(q.id)
            else:
                need_gen.append(i)
            slots_q.append(q)

    # Review items after the first pass get a fresh variant ("similar to what you missed").
    if llm is not None:
        with session() as s:
            for i, slot in enumerate(bp.math):
                if slot.reason != "review" or slots_q[i] is None:
                    continue
                item = s.exec(select(ReviewItem).where(ReviewItem.user_id == user_id,
                                                       ReviewItem.question_id == slot.review_question_id)).first()
                if item and item.box >= 2:
                    var = generate_math(llm, [(slot.skill, slot.difficulty)], variant_of=slots_q[i], rounds=1)[0]
                    if var is not None:
                        slots_q[i] = var
        if need_gen:
            made = generate_math(llm, [(bp.math[i].skill, bp.math[i].difficulty) for i in need_gen])
            for i, q in zip(need_gen, made):
                slots_q[i] = q
    with session() as s:
        for i, slot in enumerate(bp.math):
            q = slots_q[i]
            if q is None:
                q = _fallback_math(s, slot.skill, exclude, seen)
            if q is None:
                continue
            exclude.add(q.id)
            chosen.append((q, slot.reason))

    # ---- Reading & Writing: one article + its question set
    chosen += _build_rw(user_id, bp, llm, seen, rng)

    with session() as s:
        ps = PracticeSession(user_id=user_id, kind=bp.kind)
        s.add(ps)
        s.commit()
        s.refresh(ps)
        for pos, (q, reason) in enumerate(chosen):
            s.add(SessionItem(session_id=ps.id, question_id=q.id, position=pos, reason=reason))
        s.commit()
        return ps.id


def _build_rw(user_id: int, bp: selector.Blueprint, llm, seen: set[int], rng: random.Random) -> list[tuple[Question, str]]:
    out: list[tuple[Question, str]] = []
    with session() as s:
        passages = s.exec(select(Passage)).all()
        rw_qs = s.exec(select(Question).where(Question.section == "rw")).all()
        used_passages = {q.passage_id for q in rw_qs if q.id in seen and q.passage_id}
        by_passage: dict[int, list[Question]] = {}
        for q in rw_qs:
            if q.passage_id:
                by_passage.setdefault(q.passage_id, []).append(q)
        fresh = [p for p in passages if p.id not in used_passages]

        def pick_set(qs: list[Question]) -> list[Question]:
            # Prefer questions on the blueprint's skills, keeping the set to RW_COUNT.
            wanted = list(bp.rw_skills)
            pref = []
            for sk in wanted:
                m = next((q for q in qs if q.skill == sk and q not in pref), None)
                if m:
                    pref.append(m)
            rest = [q for q in qs if q not in pref]
            return (pref + rest)[: selector.RW_COUNT]

        selected: list[Question] = []
        # 1) A fresh passage that already has a full, unseen question set.
        ready = [p for p in fresh if len([q for q in by_passage.get(p.id, []) if q.id not in seen]) >= 4]
        if ready:
            p = rng.choice(ready)
            selected = pick_set([q for q in by_passage[p.id] if q.id not in seen])
        elif llm is not None and fresh:
            # 2) Generate a set for a fresh passage (prefer ones with no questions yet).
            p = min(fresh, key=lambda x: (len(by_passage.get(x.id, [])), rng.random()))
            selected = generate_rw_set(llm, p, bp.rw_skills, bp.rw_difficulties)
        if not selected and by_passage:
            # 3) Offline and out of fresh passages: reuse the passage seen longest ago.
            pid = rng.choice(list(by_passage))
            selected = pick_set(by_passage[pid])
        out += [(q, "baseline" if bp.kind == "baseline" else "weak") for q in selected]

        for qid in bp.rw_review:
            q = s.get(Question, qid)
            if q and q not in selected:
                out.append((q, "review"))
    return out


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
                "reason": it.reason,
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
