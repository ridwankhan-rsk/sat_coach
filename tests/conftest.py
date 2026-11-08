import json
import random

import pytest

from sat_coach import db
from sat_coach.llm import set_llm
from sat_coach.seed import load_seed


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SAT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    db.reset_engine(f"sqlite:///{tmp_path / 'test.db'}")
    load_seed()
    set_llm(None)
    random.seed(0)
    yield
    set_llm(None)


def make_user(name="Ana"):
    with db.session() as s:
        u = db.User(name=name)
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


class FakeLLM:
    """Scripted provider: `writer` returns the generation payload, `solver` the re-solve answers."""

    name = "fake"

    def __init__(self, writer, solver):
        self.writer, self.solver = writer, solver
        self.calls = []

    def generate_json(self, system, prompt, *, check=False):
        self.calls.append(("check" if check else "write", prompt))
        fn = self.solver if check else self.writer
        out = fn(prompt)
        return json.loads(json.dumps(out))
