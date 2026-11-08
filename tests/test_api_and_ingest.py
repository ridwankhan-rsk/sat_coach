from fastapi.testclient import TestClient

from sat_coach.api import app
from sat_coach.ingest import passages
from sat_coach.llm import parse_json


def test_profile_flow_and_auth():
    c = TestClient(app)
    assert c.get("/api/me").status_code == 401
    assert c.post("/api/users", json={"name": "Sam"}).status_code == 200
    assert c.post("/api/users", json={"name": "Sam"}).status_code == 409
    me = c.get("/api/me").json()
    assert me["name"] == "Sam" and me["open_session"] is None
    data = c.post("/api/sessions").json()
    assert len(data["questions"]) == 13
    res = c.post(f"/api/sessions/{data['id']}/submit", json={"answers": {}}).json()
    assert res["completed"] and "projection" in res
    miss = next(q for q in res["questions"] if not q["correct"])
    assert c.post(f"/api/attempts/{miss['attempt_id']}/reason", json={"reason": "careless"}).status_code == 200
    assert c.get("/api/insights").json()["miss_reasons"] == {"careless": 1}
    assert len(c.get("/api/progress").json()["history"]) == 1
    # Another profile can't read Sam's session
    other = TestClient(app)
    other.post("/api/users", json={"name": "Lee"})
    assert other.get(f"/api/sessions/{data['id']}").status_code == 404
    assert c.get("/").status_code == 200


def test_status_reports_offline_mode():
    s = TestClient(app).get("/api/status").json()
    assert s["llm"] is None and s["bank"]["math"] >= 38 and s["bank"]["passages"] >= 4


def test_passage_excerpting():
    para = "The quick brown fox considers the nature of rivers and time in a long and winding sentence that keeps going. " * 3
    book = "*** START OF THE PROJECT GUTENBERG EBOOK X ***\n\nCHAPTER I\n\n" + "\n\n".join([para] * 40) + "\n\n*** END OF THE PROJECT GUTENBERG EBOOK X ***\nlicense"
    body = passages.strip_gutenberg(book)
    assert "license" not in body and "START OF" not in body
    ex = passages.excerpts(body, limit=2)
    assert len(ex) == 2
    assert all(250 <= len(e.split()) <= 700 for e in ex)


def test_parse_json_tolerates_fences():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Here you go: {"a": [1, 2]} thanks') == {"a": [1, 2]}

