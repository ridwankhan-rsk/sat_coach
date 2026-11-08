"""Per-skill ability estimates (a simple Elo / 1-parameter IRT model).

P(correct) = sigmoid(theta - b), where b is the item difficulty (easy -1, medium 0, hard 1).
After each answer theta moves toward the evidence; the step size shrinks as uncertainty falls.
"""

from __future__ import annotations

import math

from sqlmodel import Session, select

from ..db import SkillState
from ..taxonomy import DIFFICULTY_B, SKILLS

MIN_UNC = 0.25
K = 0.8


def p_correct(theta: float, difficulty: str) -> float:
    return 1.0 / (1.0 + math.exp(-(theta - DIFFICULTY_B[difficulty])))


def update(state: SkillState, difficulty: str, correct: bool) -> None:
    p = p_correct(state.theta, difficulty)
    state.theta += K * state.unc * ((1.0 if correct else 0.0) - p)
    state.theta = max(-3.0, min(3.0, state.theta))
    state.unc = max(MIN_UNC, state.unc * 0.85)
    state.attempts += 1
    state.correct += int(correct)


def get_states(s: Session, user_id: int) -> dict[str, SkillState]:
    """All skill states for a user, creating missing rows (theta 0, full uncertainty)."""
    rows = {r.skill: r for r in s.exec(select(SkillState).where(SkillState.user_id == user_id)).all()}
    for sk in SKILLS:
        if sk.id not in rows:
            rows[sk.id] = SkillState(user_id=user_id, skill=sk.id)
            s.add(rows[sk.id])
    return rows


def target_difficulty(theta: float) -> str:
    """Pick the difficulty where the student is likely, but not certain, to succeed (~65-70%)."""
    return min(DIFFICULTY_B, key=lambda d: abs((theta - 0.7) - DIFFICULTY_B[d]))
