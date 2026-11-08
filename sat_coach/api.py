from __future__ import annotations

from pathlib import Path

from fastapi import Cookie, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlmodel import func, select

from .coach import sessions as svc
from .config import get_settings
from .db import Passage, Question, User, session
from .llm import LLMError, get_llm

WEB = Path(__file__).parent / "web"
app = FastAPI(title="SAT Coach")


def current_user(uid: str | None) -> int:
    if not uid:
        raise HTTPException(401, "Pick a profile first.")
    with session() as s:
        u = s.get(User, int(uid))
    if u is None:
        raise HTTPException(401, "Profile not found.")
    return u.id


class NewUser(BaseModel):
    name: str


class Login(BaseModel):
    user_id: int


class Submission(BaseModel):
    answers: dict[str, dict]


class MissReason(BaseModel):
    reason: str


@app.get("/api/status")
def status():
    s = get_settings()
    llm = get_llm()
    with session() as db:
        counts = {
            sec: db.exec(select(func.count()).select_from(Question).where(Question.section == sec)).one()
            for sec in ("math", "rw")
        }
        counts["passages"] = db.exec(select(func.count()).select_from(Passage)).one()
        by_origin: dict[str, dict[str, int]] = {"math": {}, "rw": {}}
        rows = db.exec(select(Question.section, Question.origin, func.count()).group_by(Question.section, Question.origin)).all()
        for section, origin, n in rows:
            by_origin.setdefault(section, {})[origin] = n
        counts["by_origin"] = by_origin
    return {"llm": llm.name if llm else None, "provider": s.llm_provider, "bank": counts}


@app.get("/api/users")
def users():
    with session() as s:
        return [{"id": u.id, "name": u.name} for u in s.exec(select(User).order_by(User.name)).all()]


@app.post("/api/users")
def create_user(body: NewUser, response: Response):
    name = body.name.strip()[:40]
    if not name:
        raise HTTPException(400, "Name required.")
    with session() as s:
        if s.exec(select(User).where(User.name == name)).first():
            raise HTTPException(409, "That name is taken.")
        u = User(name=name)
        s.add(u)
        s.commit()
        s.refresh(u)
    response.set_cookie("uid", str(u.id), samesite="lax", max_age=60 * 60 * 24 * 365)
    return {"id": u.id, "name": u.name}


@app.post("/api/login")
def login(body: Login, response: Response):
    with session() as s:
        u = s.get(User, body.user_id)
    if u is None:
        raise HTTPException(404, "Profile not found.")
    response.set_cookie("uid", str(u.id), samesite="lax", max_age=60 * 60 * 24 * 365)
    return {"id": u.id, "name": u.name}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("uid")
    return {"ok": True}


@app.get("/api/me")
def me(uid: str | None = Cookie(default=None)):
    user_id = current_user(uid)
    with session() as s:
        u = s.get(User, user_id)
    open_ps = svc.open_session(user_id)
    return {"id": u.id, "name": u.name, "open_session": open_ps.id if open_ps else None}


@app.post("/api/sessions")
def start(uid: str | None = Cookie(default=None)):
    user_id = current_user(uid)
    try:
        sid = svc.start_session(user_id)
    except LLMError as e:
        raise HTTPException(502, f"Question generation failed: {e}") from e
    return svc.load_session(sid, user_id, reveal=False)


@app.get("/api/sessions/{sid}")
def get_session(sid: int, uid: str | None = Cookie(default=None)):
    user_id = current_user(uid)
    try:
        data = svc.load_session(sid, user_id, reveal=False)
        if data["completed"]:
            data = svc.load_session(sid, user_id, reveal=True)
        return data
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@app.post("/api/sessions/{sid}/submit")
def submit(sid: int, body: Submission, uid: str | None = Cookie(default=None)):
    user_id = current_user(uid)
    try:
        return svc.submit_session(sid, user_id, {int(k): v for k, v in body.answers.items()})
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@app.post("/api/attempts/{aid}/reason")
def miss_reason(aid: int, body: MissReason, uid: str | None = Cookie(default=None)):
    user_id = current_user(uid)
    try:
        svc.set_miss_reason(aid, user_id, body.reason)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


@app.get("/api/progress")
def progress(uid: str | None = Cookie(default=None)):
    return svc.progress(current_user(uid))


@app.get("/api/insights")
def insights(uid: str | None = Cookie(default=None)):
    return svc.insights(current_user(uid))


app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")
