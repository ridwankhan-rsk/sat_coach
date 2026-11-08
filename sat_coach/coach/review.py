"""Spaced review of missed questions (Leitner boxes, spaced by sessions rather than days,
since students practice irregularly).

Missed → box 1, due next session. Correct on review → next box, due further out.
Missed again → back to box 1. Clearing box 4 retires the item.
"""

from __future__ import annotations

from sqlmodel import Session, select

from ..db import ReviewItem

INTERVALS = {1: 1, 2: 2, 3: 4, 4: 8}  # sessions until next review, by box


def _due_at(sessions_done: int, box: int) -> int:
    """`sessions_done` counts the session just finished. Interval 1 = the very next session."""
    return sessions_done + INTERVALS[box] - 1


def record(s: Session, user_id: int, question_id: int, correct: bool, sessions_done: int) -> None:
    item = s.exec(
        select(ReviewItem).where(ReviewItem.user_id == user_id, ReviewItem.question_id == question_id)
    ).first()
    if item is None:
        if correct:
            return
        s.add(ReviewItem(user_id=user_id, question_id=question_id, box=1, due_session=_due_at(sessions_done, 1)))
        return
    if correct:
        if item.box >= 4:
            item.retired = True
        else:
            item.box += 1
            item.due_session = _due_at(sessions_done, item.box)
    else:
        item.box = 1
        item.retired = False
        item.times_missed += 1
        item.due_session = _due_at(sessions_done, 1)
    s.add(item)


def due(s: Session, user_id: int, sessions_done: int) -> list[ReviewItem]:
    rows = s.exec(
        select(ReviewItem).where(
            ReviewItem.user_id == user_id, ReviewItem.retired == False, ReviewItem.due_session <= sessions_done  # noqa: E712
        )
    ).all()
    return sorted(rows, key=lambda r: (r.due_session, -r.times_missed))
