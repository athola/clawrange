from fastapi import FastAPI
from fastapi.testclient import TestClient
from brain_db import BrainDB
from tenant_profile import Profile
from persona_api import create_persona_router


def _client(tmp_path):
    db = BrainDB(str(tmp_path / "b.db"))
    db.init_db()
    prof = Profile(
        name="cos",
        raw={
            "profile": "cos",
            "assistant": {
                "name": "Max",
                "identity": {"creature": "CoS", "vibe": "sharp", "emoji": "🎯"},
            },
        },
    )
    app = FastAPI()
    app.include_router(
        create_persona_router(db, lambda: prof, lambda p: {})
    )  # no real targets in test
    return TestClient(app), db


def test_propose_then_approve_flow(tmp_path):
    client, db = _client(tmp_path)
    r = client.post(
        "/persona/propose",
        json={
            "kind": "persona",
            "target": "Tone",
            "content": "Be terse.",
            "source": "feedback",
        },
    )
    assert r.status_code == 200
    lid = r.json()["id"]
    assert client.get("/persona/proposals?status=pending").json()["total"] == 1
    assert client.post(f"/persona/proposals/{lid}/approve").status_code == 200
    assert db.get_learning(lid)["status"] == "approved"


def test_propose_bad_kind_is_400(tmp_path):
    client, _ = _client(tmp_path)
    r = client.post(
        "/persona/propose",
        json={"kind": "bogus", "target": "x", "content": "y", "source": "feedback"},
    )
    assert r.status_code == 400


def test_identity_endpoint_renders(tmp_path):
    client, _ = _client(tmp_path)
    assert "Max" in client.get("/persona/identity").json()["identity"]


def test_reject_flow(tmp_path):
    client, db = _client(tmp_path)
    r = client.post(
        "/persona/propose",
        json={
            "kind": "persona",
            "target": "Style",
            "content": "Short answers.",
            "source": "feedback",
        },
    )
    lid = r.json()["id"]
    assert client.post(f"/persona/proposals/{lid}/reject").status_code == 200
    assert db.get_learning(lid)["status"] == "rejected"


def test_missing_approve_is_404(tmp_path):
    client, _ = _client(tmp_path)
    assert client.post("/persona/proposals/no-such-id/approve").status_code == 404


def test_missing_reject_is_404(tmp_path):
    client, _ = _client(tmp_path)
    assert client.post("/persona/proposals/no-such-id/reject").status_code == 404


def test_proposals_list_all(tmp_path):
    client, _ = _client(tmp_path)
    client.post(
        "/persona/propose",
        json={
            "kind": "persona",
            "target": "T",
            "content": "Abc.",
            "source": "feedback",
        },
    )
    result = client.get("/persona/proposals").json()
    assert result["total"] == 1
    assert result["proposals"][0]["status"] == "pending"


def test_soul_endpoint_renders(tmp_path):
    client, _ = _client(tmp_path)
    assert "Max" in client.get("/persona/soul").json()["soul"]


def test_render_endpoint(tmp_path):
    client, _ = _client(tmp_path)
    r = client.post("/persona/render")
    assert r.status_code == 200
    assert "targets" in r.json()


def test_health_endpoint(tmp_path):
    client, _ = _client(tmp_path)
    r = client.get("/healthz/persona")
    assert r.status_code == 200
    data = r.json()
    assert data["profile"] == "cos"
    assert "pending" in data
    assert "targets" in data
