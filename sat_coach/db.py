"""Database models. Every table holding progress carries user_id, so the same schema
works for a single local profile today and a hosted multi-user site later."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, Column
from sqlmodel import Field, Session, SQLModel, create_engine, select

from .config import get_settings


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    created_at: datetime = Field(default_factory=_now)


class Passage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    author: str
    year: Optional[str] = None
    genre: str  # literature | history | social_science | science | humanities
    text: str
    source_url: str = ""
    license: str = "Public domain"
    origin: str = "seed"  # seed | fetched | official


class Question(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    section: str = Field(index=True)  # math | rw
    domain: str = Field(index=True)
    skill: str = Field(index=True)
    difficulty: str = Field(index=True)  # easy | medium | hard
    qtype: str = "mcq"  # mcq | spr (student-produced response)
    stem: str
    choices: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    answer: str  # "A".."D" for mcq; the numeric value(s) for spr, "|"-separated if several are accepted
    # Official-style rationale per choice: {"A": "...", ...}; spr uses {"correct": "..."}
    rationale: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    passage_id: Optional[int] = Field(default=None, foreign_key="passage.id", index=True)
    excerpt: Optional[str] = None  # R&W: the specific text the question is about
    origin: str = "seed"  # seed | official | generated
    parent_id: Optional[int] = Field(default=None, foreign_key="question.id")  # variant of
    created_at: datetime = Field(default_factory=_now)


class PracticeSession(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    kind: str = "adaptive"  # baseline | adaptive
    created_at: datetime = Field(default_factory=_now)
    completed_at: Optional[datetime] = None
    math_score: Optional[int] = None
    rw_score: Optional[int] = None
    total_score: Optional[int] = None
    band: Optional[int] = None


class SessionItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="practicesession.id", index=True)
    question_id: int = Field(foreign_key="question.id")
    position: int
    reason: str  # baseline | weak | coverage | review


class Attempt(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    session_id: int = Field(foreign_key="practicesession.id", index=True)
    question_id: int = Field(foreign_key="question.id", index=True)
    response: str = ""
    correct: bool
    seconds: Optional[float] = None
    miss_reason: Optional[str] = None  # concept | careless | time
    created_at: datetime = Field(default_factory=_now)


class SkillState(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    skill: str = Field(index=True)
    theta: float = 0.0
    unc: float = 1.0  # 1.0 = no information, shrinks toward ~0.25
    attempts: int = 0
    correct: int = 0


class ReviewItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    question_id: int = Field(foreign_key="question.id")
    box: int = 1  # Leitner box 1..4
    due_session: int = 0  # due once the user has completed this many sessions
    retired: bool = False
    times_missed: int = 1


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings().db_url, connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(_engine)
    return _engine


def reset_engine(url: str | None = None):
    """Point the app at another database (used by tests)."""
    global _engine
    _engine = create_engine(url or get_settings().db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(_engine)
    return _engine


def session() -> Session:
    return Session(get_engine())


__all__ = [
    "User", "Passage", "Question", "PracticeSession", "SessionItem", "Attempt",
    "SkillState", "ReviewItem", "get_engine", "reset_engine", "session", "select",
]
