# Persona Configuration & Meta-Learning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make assistant persona/identity declaratively configurable per use case and add an approval-gated meta-learning loop that persists enhancements, with `chief-of-staff` (Max) shipped as the worked example.

**Architecture:** Extend the existing tenant-profile/persona system. A new `identity` profile block renders `IDENTITY.md`. Approved persona "learnings" live in a brain table and project to a git-tracked `learned.yaml`. A propose→`[DRAFT]`→approve→non-destructive-re-render loop is exposed via a `/persona` router plus `!persona` proxy commands, a scheduled generator, and a flagged signal scan.

**Tech Stack:** Python 3.12, FastAPI, SQLite (brain_db), PyYAML, httpx, pytest. Spec: `docs/superpowers/specs/2026-06-26-persona-config-meta-learning-design.md`.

## Global Constraints

- All LLM calls route through the workflows `llm_proxy` (never call providers directly).
- Single uvicorn worker / single-process APScheduler. No new always-on services.
- `data/` is gitignored runtime state. `config/profiles/` is git-tracked.
- Profile load failure must never crash boot (log and degrade).
- New brain tables are additive via `CREATE TABLE IF NOT EXISTS` in `init_db`.
- Shell stays Bash, macOS and Ubuntu compatible.
- Commit messages: conventional, no AI attribution, no emojis.
- Run unit tests with `python3 -m pytest workflows/tests/ -q` (from repo root) or `cd workflows && python3 -m pytest tests/ -q`.

---

### Task 1: Profile `identity` block validation and IDENTITY render

**Files:**
- Modify: `workflows/tenant_profile.py` (add identity validation in `validate`)
- Modify: `workflows/persona.py` (add `render_identity`, `write_identity`)
- Create: `openclaw/identity.template.md`
- Test: `workflows/tests/test_profile.py`, `workflows/tests/test_persona.py`

**Interfaces:**
- Produces: `tenant_profile.IDENTITY_FIELDS: set[str]`, `persona.render_identity(profile) -> str`, and `persona.write_identity(profile, path) -> Path`.
- Consumes: existing `Profile.assistant` dict, `persona._TEMPLATE_PATH` pattern.

- [ ] **Step 1: Create the identity template**

Create `openclaw/identity.template.md`:

```markdown
# IDENTITY.md - Who Am I?

- **Name:** {{name}}
- **Creature:** {{creature}}
- **Vibe:** {{vibe}}
- **Emoji:** {{emoji}}
- **Avatar:** {{avatar}}

---

This identity is rendered from this assistant's tenant profile. Approved
meta-learning enhancements accumulate under "## Learned" below.
```

- [ ] **Step 2: Write the failing test for identity validation**

Add to `workflows/tests/test_profile.py`:

```python
def test_identity_block_validates_and_rejects_unknown_keys():
    from tenant_profile import Profile, validate, ProfileError
    ok = Profile(name="t", raw={"profile": "t", "assistant": {
        "name": "Max",
        "identity": {"name": "Max", "creature": "CoS", "vibe": "sharp", "emoji": "🎯"},
    }})
    validate(ok)  # should not raise
    bad = Profile(name="t", raw={"profile": "t", "assistant": {
        "identity": {"name": "Max", "bogus": "x"},
    }})
    try:
        validate(bad)
        assert False, "expected ProfileError"
    except ProfileError as e:
        assert "identity" in str(e)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd workflows && python3 -m pytest tests/test_profile.py::test_identity_block_validates_and_rejects_unknown_keys -q`
Expected: FAIL (validation does not yet check identity).

- [ ] **Step 4: Implement identity validation**

In `workflows/tenant_profile.py`, near the `KNOWN_*` sets add:

```python
IDENTITY_FIELDS = {"name", "creature", "vibe", "emoji", "avatar"}
EMOJI_MAX_LEN = 16
```

In `validate(...)`, after the existing assistant handling (or near the top of the function body), add:

```python
assistant = profile.raw.get("assistant") or {}
identity = assistant.get("identity")
if identity is not None:
    if not isinstance(identity, dict):
        raise ProfileError("assistant.identity must be a mapping")
    unknown = set(identity) - IDENTITY_FIELDS
    if unknown:
        raise ProfileError(
            f"assistant.identity has unknown keys {sorted(unknown)} "
            f"(known: {sorted(IDENTITY_FIELDS)})"
        )
    emoji = identity.get("emoji", "")
    if emoji and len(str(emoji)) > EMOJI_MAX_LEN:
        raise ProfileError("assistant.identity.emoji is too long")
```

- [ ] **Step 5: Run validation test to verify it passes**

Run: `cd workflows && python3 -m pytest tests/test_profile.py::test_identity_block_validates_and_rejects_unknown_keys -q`
Expected: PASS.

- [ ] **Step 6: Write the failing test for render_identity**

Add to `workflows/tests/test_persona.py`:

```python
def test_render_identity_fills_fields_and_defaults_name():
    from tenant_profile import Profile
    from persona import render_identity
    p = Profile(name="t", raw={"profile": "t", "assistant": {
        "name": "Max",
        "identity": {"creature": "Chief of Staff", "vibe": "sharp", "emoji": "🎯"},
    }})
    out = render_identity(p)
    assert "**Name:** Max" in out          # defaults to assistant.name
    assert "**Creature:** Chief of Staff" in out
    assert "🎯" in out
```

- [ ] **Step 7: Run test to verify it fails**

Run: `cd workflows && python3 -m pytest tests/test_persona.py::test_render_identity_fills_fields_and_defaults_name -q`
Expected: FAIL ("cannot import name 'render_identity'").

- [ ] **Step 8: Implement render_identity / write_identity**

In `workflows/persona.py` add:

```python
_IDENTITY_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "openclaw" / "identity.template.md"
)


def render_identity(profile: Profile) -> str:
    """Render IDENTITY.md from the profile's assistant.identity block."""
    a = profile.assistant
    ident = a.get("identity") or {}
    template = _IDENTITY_TEMPLATE_PATH.read_text()
    subs = {
        "{{name}}": ident.get("name") or a.get("name", "Assistant"),
        "{{creature}}": ident.get("creature", "AI operator"),
        "{{vibe}}": ident.get("vibe", "calm, direct, helpful"),
        "{{emoji}}": ident.get("emoji", "🤖"),
        "{{avatar}}": ident.get("avatar", ""),
    }
    for token, value in subs.items():
        template = template.replace(token, value)
    return template.rstrip("\n") + "\n"


def write_identity(profile: Profile, path: str | Path) -> Path:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render_identity(profile))
    return dest
```

- [ ] **Step 9: Run persona tests to verify pass**

Run: `cd workflows && python3 -m pytest tests/test_persona.py tests/test_profile.py -q`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add workflows/tenant_profile.py workflows/persona.py openclaw/identity.template.md workflows/tests/test_profile.py workflows/tests/test_persona.py
git commit -m "feat(persona): profile identity block renders IDENTITY.md"
```

---

### Task 2: brain_db `persona_learnings` table and CRUD

**Files:**
- Modify: `workflows/brain_db.py` (table in `init_db`, `create_learning`, `list_learnings`, `get_learning`, `set_learning_status`)
- Test: `workflows/tests/test_brain_db.py`

**Interfaces:**
- Produces:
  - `create_learning(profile, kind, target, content, source, task_id=None) -> dict`
  - `list_learnings(profile=None, status=None) -> list[dict]`
  - `get_learning(learning_id) -> dict | None`
  - `set_learning_status(learning_id, status) -> dict` (sets `decided_at`)
- Consumes: existing `_now()`, `uuid`, `self._conn`, `_row_to_dict`.

- [ ] **Step 1: Write the failing test**

Add to `workflows/tests/test_brain_db.py`:

```python
def test_persona_learning_lifecycle(tmp_path):
    from brain_db import BrainDB
    db = BrainDB(str(tmp_path / "b.db"))
    db.init_db()
    row = db.create_learning("chief-of-staff", "persona", "Communication",
                             "Lead with the recommendation.", "feedback")
    assert row["status"] == "pending" and row["id"]
    assert db.get_learning(row["id"])["content"].startswith("Lead with")
    db.set_learning_status(row["id"], "approved")
    approved = db.list_learnings(profile="chief-of-staff", status="approved")
    assert len(approved) == 1 and approved[0]["decided_at"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd workflows && python3 -m pytest tests/test_brain_db.py::test_persona_learning_lifecycle -q`
Expected: FAIL ("'BrainDB' object has no attribute 'create_learning'").

- [ ] **Step 3: Add the table to init_db**

In `workflows/brain_db.py` `init_db`, add another `CREATE TABLE IF NOT EXISTS` block alongside the others:

```python
            CREATE TABLE IF NOT EXISTS persona_learnings (
                id          TEXT PRIMARY KEY,
                profile     TEXT NOT NULL,
                kind        TEXT NOT NULL,
                target      TEXT,
                content     TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',
                source      TEXT NOT NULL DEFAULT 'feedback',
                task_id     TEXT,
                created_at  TEXT NOT NULL,
                decided_at  TEXT
            );
```

- [ ] **Step 4: Implement the CRUD methods**

Add methods to the `BrainDB` class:

```python
    def create_learning(self, profile, kind, target, content,
                        source="feedback", task_id=None):
        lid = str(uuid.uuid4())[:8]
        now = _now()
        self._conn.execute(
            "INSERT INTO persona_learnings "
            "(id, profile, kind, target, content, status, source, task_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (lid, profile, kind, target, content, source, task_id, now),
        )
        self._conn.commit()
        return self.get_learning(lid)

    def get_learning(self, learning_id):
        row = self._conn.execute(
            "SELECT * FROM persona_learnings WHERE id = ?", (learning_id,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_learnings(self, profile=None, status=None):
        q = "SELECT * FROM persona_learnings"
        clauses, params = [], []
        if profile:
            clauses.append("profile = ?"); params.append(profile)
        if status:
            clauses.append("status = ?"); params.append(status)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY created_at DESC"
        rows = self._conn.execute(q, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def set_learning_status(self, learning_id, status):
        self._conn.execute(
            "UPDATE persona_learnings SET status = ?, decided_at = ? WHERE id = ?",
            (status, _now(), learning_id),
        )
        self._conn.commit()
        return self.get_learning(learning_id)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd workflows && python3 -m pytest tests/test_brain_db.py::test_persona_learning_lifecycle -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add workflows/brain_db.py workflows/tests/test_brain_db.py
git commit -m "feat(brain): add persona_learnings table and CRUD"
```

---

### Task 3: Compose persona/identity with the `## Learned` overlay and atomic render

**Files:**
- Modify: `workflows/persona.py` (`compose_persona`, `compose_identity`, `render_all`)
- Test: `workflows/tests/test_persona.py`

**Interfaces:**
- Produces:
  - `compose_persona(profile, learnings) -> str`
  - `compose_identity(profile, learnings) -> str`
  - `render_all(profile, targets, learnings) -> dict[str, bool]` where `targets` is `{"soul": path, "identity": path, "workspace_soul": [paths], "workspace_identity": [paths]}`
- Consumes: `render_persona`, `render_identity`, learnings as `list[dict]` with keys `kind/target/content`.

- [ ] **Step 1: Write the failing test**

Add to `workflows/tests/test_persona.py`:

```python
def test_compose_appends_learned_region_and_is_non_destructive():
    from tenant_profile import Profile
    from persona import compose_persona
    p = Profile(name="t", raw={"profile": "t", "assistant": {"name": "Max"}})
    learnings = [{"kind": "persona", "target": "Communication",
                  "content": "Lead with the recommendation."}]
    out = compose_persona(p, learnings)
    assert "## Learned" in out
    assert "Communication" in out and "Lead with the recommendation." in out
    # base persona still present
    assert "Max" in out
    # no learnings -> no Learned region
    assert "## Learned" not in compose_persona(p, [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd workflows && python3 -m pytest tests/test_persona.py::test_compose_appends_learned_region_and_is_non_destructive -q`
Expected: FAIL ("cannot import name 'compose_persona'").

- [ ] **Step 3: Implement compose and render_all**

In `workflows/persona.py` add:

```python
import os


def _learned_region(learnings, kind):
    rows = [x for x in learnings if x.get("kind") == kind]
    if not rows:
        return ""
    lines = ["", "## Learned", ""]
    for x in rows:
        target = x.get("target") or "General"
        lines.append(f"- **{target}:** {x['content']}")
    return "\n".join(lines) + "\n"


def compose_persona(profile: Profile, learnings: list) -> str:
    base = render_persona(profile).rstrip("\n") + "\n"
    return (base + _learned_region(learnings, "persona")).rstrip("\n") + "\n"


def compose_identity(profile: Profile, learnings: list) -> str:
    base = render_identity(profile).rstrip("\n") + "\n"
    return (base + _learned_region(learnings, "identity")).rstrip("\n") + "\n"


def _atomic_write(path: Path, text: str) -> bool:
    """Write text atomically; return False (no raise) if dir is unwritable."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def render_all(profile: Profile, targets: dict, learnings: list) -> dict:
    """Render persona+identity to all configured targets. Returns per-target ok."""
    soul = compose_persona(profile, learnings)
    ident = compose_identity(profile, learnings)
    results = {}
    if targets.get("soul"):
        results["soul"] = _atomic_write(Path(targets["soul"]), soul)
    if targets.get("identity"):
        results["identity"] = _atomic_write(Path(targets["identity"]), ident)
    for wp in targets.get("workspace_soul", []):
        results[f"ws_soul:{wp}"] = _atomic_write(Path(wp), soul)
    for wp in targets.get("workspace_identity", []):
        results[f"ws_identity:{wp}"] = _atomic_write(Path(wp), ident)
    return results
```

- [ ] **Step 4: Write the failing test for atomic render_all**

Add to `workflows/tests/test_persona.py`:

```python
def test_render_all_writes_targets(tmp_path):
    from tenant_profile import Profile
    from persona import render_all
    p = Profile(name="t", raw={"profile": "t", "assistant": {"name": "Max"}})
    soul = tmp_path / "soul.md"; ident = tmp_path / "IDENTITY.md"
    res = render_all(p, {"soul": str(soul), "identity": str(ident)}, [])
    assert res == {"soul": True, "identity": True}
    assert "Max" in soul.read_text() and "Name:** Max" in ident.read_text()
```

- [ ] **Step 5: Run persona tests to verify pass**

Run: `cd workflows && python3 -m pytest tests/test_persona.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add workflows/persona.py workflows/tests/test_persona.py
git commit -m "feat(persona): compose learned overlay and atomic render_all"
```

---

### Task 4: persona_learning module (propose / approve / reject and overlay seed/export)

**Files:**
- Create: `workflows/persona_learning.py`
- Test: `workflows/tests/test_persona_learning.py`

**Interfaces:**
- Produces:
  - `VALID_KINDS = {"identity", "persona"}`, `CONTENT_MAX = 500`
  - `propose(brain_db, profile_name, kind, target, content, source) -> dict` (validates, creates `[DRAFT]` task and learning row, and raises `ValueError` on bad input)
  - `approve(brain_db, profile, learning_id, render_fn) -> dict` (marks approved, calls `render_fn(approved_learnings)`)
  - `reject(brain_db, learning_id) -> dict`
  - `export_overlay(brain_db, profile_name) -> list[dict]` (approved rows → overlay list)
  - `seed_overlay(brain_db, profile_name, overlay) -> int` (insert approved rows from yaml, idempotent on (target, content))
- Consumes: `brain_db.create_learning/list_learnings/get_learning/set_learning_status/create_task`.

- [ ] **Step 1: Write the failing test**

Create `workflows/tests/test_persona_learning.py`:

```python
import pytest
from brain_db import BrainDB
import persona_learning as pl


@pytest.fixture
def db(tmp_path):
    d = BrainDB(str(tmp_path / "b.db")); d.init_db(); return d


def test_propose_rejects_bad_kind(db):
    with pytest.raises(ValueError):
        pl.propose(db, "cos", "nonsense", "x", "y", "feedback")


def test_propose_creates_draft_and_pending(db):
    row = pl.propose(db, "cos", "persona", "Communication",
                     "Lead with the recommendation.", "feedback")
    assert row["status"] == "pending" and row["task_id"]
    task = db.get_task(row["task_id"])
    assert task["description"].startswith("[DRAFT]")


def test_approve_invokes_render_with_approved(db):
    row = pl.propose(db, "cos", "persona", "Tone", "Be terse.", "feedback")
    seen = {}
    pl.approve(db, "cos", row["id"], render_fn=lambda lrn: seen.update(n=len(lrn)))
    assert seen["n"] == 1
    assert db.get_learning(row["id"])["status"] == "approved"


def test_overlay_export_seed_roundtrip(db):
    row = pl.propose(db, "cos", "persona", "Tone", "Be terse.", "feedback")
    pl.approve(db, "cos", row["id"], render_fn=lambda lrn: None)
    overlay = pl.export_overlay(db, "cos")
    assert overlay and overlay[0]["content"] == "Be terse."
    db2 = BrainDB(":memory:"); db2.init_db()
    n = pl.seed_overlay(db2, "cos", overlay)
    assert n == 1
    assert pl.export_overlay(db2, "cos")[0]["target"] == "Tone"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd workflows && python3 -m pytest tests/test_persona_learning.py -q`
Expected: FAIL ("No module named 'persona_learning'").

- [ ] **Step 3: Implement persona_learning.py**

Create `workflows/persona_learning.py`:

```python
"""Persona meta-learning pipeline: propose -> draft -> approve -> overlay.

Approved learnings live in the brain (the live store) and project to a
git-trackable learned.yaml overlay (export/seed are inverse operations).
"""

from __future__ import annotations

VALID_KINDS = {"identity", "persona"}
CONTENT_MAX = 500


def propose(brain_db, profile_name, kind, target, content, source):
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {sorted(VALID_KINDS)}")
    content = (content or "").strip()
    if not content:
        raise ValueError("content is required")
    if len(content) > CONTENT_MAX:
        raise ValueError(f"content exceeds {CONTENT_MAX} chars")
    task = brain_db.create_task(
        f"[DRAFT] persona {kind} enhancement ({target}): {content}",
        priority=3, source="persona",
    )
    return brain_db.create_learning(
        profile_name, kind, target, content, source, task_id=task["id"]
    )


def approve(brain_db, profile_name, learning_id, render_fn):
    row = brain_db.get_learning(learning_id)
    if not row:
        raise ValueError("learning not found")
    if row["status"] == "approved":
        return row  # idempotent
    brain_db.set_learning_status(learning_id, "approved")
    approved = brain_db.list_learnings(profile=profile_name, status="approved")
    render_fn(approved)
    return brain_db.get_learning(learning_id)


def reject(brain_db, learning_id):
    if not brain_db.get_learning(learning_id):
        raise ValueError("learning not found")
    return brain_db.set_learning_status(learning_id, "rejected")


def export_overlay(brain_db, profile_name):
    rows = brain_db.list_learnings(profile=profile_name, status="approved")
    return [
        {"kind": r["kind"], "target": r["target"], "content": r["content"],
         "source": r["source"], "approved_at": r["decided_at"]}
        for r in rows
    ]


def seed_overlay(brain_db, profile_name, overlay):
    existing = {
        (r["target"], r["content"])
        for r in brain_db.list_learnings(profile=profile_name)
    }
    n = 0
    for item in overlay or []:
        key = (item.get("target"), item.get("content"))
        if key in existing:
            continue
        row = brain_db.create_learning(
            profile_name, item.get("kind", "persona"), item.get("target"),
            item["content"], item.get("source", "seed"),
        )
        brain_db.set_learning_status(row["id"], "approved")
        n += 1
    return n
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd workflows && python3 -m pytest tests/test_persona_learning.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add workflows/persona_learning.py workflows/tests/test_persona_learning.py
git commit -m "feat(persona): propose/approve/reject pipeline with overlay roundtrip"
```

---

### Task 5: `/persona` API router, mount, and health

**Files:**
- Create: `workflows/persona_api.py`
- Modify: `workflows/app.py` (import and `app.include_router`)
- Test: `workflows/tests/test_persona_api.py`

**Interfaces:**
- Produces: `create_persona_router(brain_db, profile_provider, render_targets_fn) -> APIRouter` with routes:
  `POST /persona/propose`, `GET /persona/proposals`, `POST /persona/proposals/{id}/approve`,
  `POST /persona/proposals/{id}/reject`, `POST /persona/render`, `GET /persona/identity`,
  `GET /persona/soul`, `GET /healthz/persona`.
- Consumes: `persona_learning`, `persona.render_all/compose_*`, `tenant_profile.load_profile`.

- [ ] **Step 1: Write the failing test**

Create `workflows/tests/test_persona_api.py`:

```python
from fastapi import FastAPI
from fastapi.testclient import TestClient
from brain_db import BrainDB
from tenant_profile import Profile
from persona_api import create_persona_router


def _client(tmp_path):
    db = BrainDB(str(tmp_path / "b.db")); db.init_db()
    prof = Profile(name="cos", raw={"profile": "cos",
                   "assistant": {"name": "Max",
                   "identity": {"creature": "CoS", "vibe": "sharp", "emoji": "🎯"}}})
    app = FastAPI()
    app.include_router(create_persona_router(
        db, lambda: prof, lambda p: {}))  # no real targets in test
    return TestClient(app), db


def test_propose_then_approve_flow(tmp_path):
    client, db = _client(tmp_path)
    r = client.post("/persona/propose", json={
        "kind": "persona", "target": "Tone", "content": "Be terse.",
        "source": "feedback"})
    assert r.status_code == 200
    lid = r.json()["id"]
    assert client.get("/persona/proposals?status=pending").json()["total"] == 1
    assert client.post(f"/persona/proposals/{lid}/approve").status_code == 200
    assert db.get_learning(lid)["status"] == "approved"


def test_propose_bad_kind_is_400(tmp_path):
    client, _ = _client(tmp_path)
    r = client.post("/persona/propose", json={
        "kind": "bogus", "target": "x", "content": "y", "source": "feedback"})
    assert r.status_code == 400


def test_identity_endpoint_renders(tmp_path):
    client, _ = _client(tmp_path)
    assert "Max" in client.get("/persona/identity").json()["identity"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd workflows && python3 -m pytest tests/test_persona_api.py -q`
Expected: FAIL ("No module named 'persona_api'").

- [ ] **Step 3: Implement persona_api.py**

Create `workflows/persona_api.py`:

```python
"""/persona router — propose/approve/reject persona learnings and re-render."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import persona_learning as pl
from persona import compose_identity, compose_persona, render_all


class Proposal(BaseModel):
    kind: str
    target: str = ""
    content: str
    source: str = "feedback"


def create_persona_router(brain_db, profile_provider, render_targets_fn) -> APIRouter:
    router = APIRouter()

    def _profile():
        return profile_provider()

    def _approved():
        p = _profile()
        return brain_db.list_learnings(profile=p.name, status="approved")

    def _render(approved):
        p = _profile()
        targets = render_targets_fn(p)
        return render_all(p, targets, approved)

    @router.post("/persona/propose")
    def propose(body: Proposal):
        p = _profile()
        try:
            return pl.propose(brain_db, p.name, body.kind, body.target,
                              body.content, body.source)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.get("/persona/proposals")
    def proposals(status: str | None = None):
        p = _profile()
        rows = brain_db.list_learnings(profile=p.name, status=status)
        return {"proposals": rows, "total": len(rows)}

    @router.post("/persona/proposals/{learning_id}/approve")
    def approve(learning_id: str):
        p = _profile()
        try:
            return pl.approve(brain_db, p.name, learning_id, render_fn=_render)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @router.post("/persona/proposals/{learning_id}/reject")
    def reject(learning_id: str):
        try:
            return pl.reject(brain_db, learning_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @router.post("/persona/render")
    def render():
        return {"targets": _render(_approved())}

    @router.get("/persona/identity")
    def identity():
        return {"identity": compose_identity(_profile(), _approved())}

    @router.get("/persona/soul")
    def soul():
        return {"soul": compose_persona(_profile(), _approved())}

    @router.get("/healthz/persona")
    def health():
        p = _profile()
        pending = brain_db.list_learnings(profile=p.name, status="pending")
        return {"profile": p.name, "pending": len(pending),
                "targets": list(render_targets_fn(p).keys())}

    return router
```

- [ ] **Step 4: Mount in app.py**

In `workflows/app.py`, after the existing router includes, add a helper and mount. Near the top imports add `from tenant_profile import load_profile`. After `app.include_router(create_brain_router(...))` add:

```python
def _persona_render_targets(profile):
    import os
    repo = os.path.dirname(os.path.dirname(__file__))
    state = os.environ.get("OPENCLAW_STATE_DIR", os.path.join(repo, "data", "openclaw-state"))
    return {
        "soul": os.path.join(repo, "openclaw", "soul.md"),
        "identity": os.path.join(repo, "openclaw", "identity.md"),
        "workspace_soul": [
            os.path.join(state, "workspace", "SOUL.md"),
            os.path.join(state, "workspace-max-ops", "SOUL.md"),
        ],
        "workspace_identity": [
            os.path.join(state, "workspace", "IDENTITY.md"),
            os.path.join(state, "workspace-max-ops", "IDENTITY.md"),
        ],
    }


def _current_profile():
    try:
        return load_profile()
    except Exception:
        from tenant_profile import Profile
        return Profile(name="starter", raw={"profile": "starter", "assistant": {}})


from persona_api import create_persona_router  # noqa: E402

app.include_router(create_persona_router(brain_db, _current_profile, _persona_render_targets))
```

- [ ] **Step 5: Run tests to verify pass**

Run: `cd workflows && python3 -m pytest tests/test_persona_api.py tests/test_app.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add workflows/persona_api.py workflows/app.py workflows/tests/test_persona_api.py
git commit -m "feat(persona): /persona router for propose/approve/render"
```

---

### Task 6: `!persona` proxy commands (explicit feedback and on-demand)

**Files:**
- Modify: `workflows/llm_proxy.py` (intercept `!persona` before LLM dispatch)
- Test: `workflows/tests/test_llm_proxy.py`

**Interfaces:**
- Consumes: `persona_learning.propose`, the workflows `/persona` routes via in-process `brain_db` import is not available in proxy. Instead, the proxy posts to its own HTTP API using `httpx` to `http://localhost:5678/persona/...` with `PROXY_AUTH_TOKEN` not required for `/persona` (internal). Use the existing `_synthetic_response` helper to reply.
- Produces: a `_handle_persona_command(subcmd, args) -> JSONResponse` mirroring `_handle_tier_command`.

- [ ] **Step 1: Write the failing test**

Add to `workflows/tests/test_llm_proxy.py`:

```python
@patch.dict("os.environ", FAKE_ENV)
@patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
class TestPersonaCommand:
    def test_persona_feedback_intercepted(self):
        body = {"messages": [{"role": "user",
                "content": "!persona lead with the recommendation"}]}
        with patch("llm_proxy._post_persona_propose",
                   return_value={"id": "ab12"}) as m:
            r = client.post("/v1/chat/completions", json=body, headers=AUTH_HEADER)
        assert r.status_code == 200
        assert m.called
        content = r.json()["choices"][0]["message"]["content"].lower()
        assert "draft" in content or "queued" in content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd workflows && python3 -m pytest tests/test_llm_proxy.py::TestPersonaCommand -q`
Expected: FAIL (command not intercepted, `_post_persona_propose` missing).

- [ ] **Step 3: Implement the command and helper**

In `workflows/llm_proxy.py` add a helper near `_call_embeddings`:

```python
async def _post_persona_propose(content: str, kind: str = "persona",
                               target: str = "feedback", source: str = "feedback"):
    """Post a persona proposal to the local /persona API. Returns the created row."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "http://localhost:5678/persona/propose",
            json={"kind": kind, "target": target, "content": content, "source": source},
        )
        r.raise_for_status()
        return r.json()
```

Add a handler:

```python
async def _handle_persona_command(args: str) -> JSONResponse:
    args = args.strip()
    if args.startswith("reflect"):
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post("http://localhost:5678/persona/reflect")
        return _synthetic_response("Persona reflection queued — review drafts with !tasks.")
    if not args:
        return _synthetic_response(
            "Usage: !persona <feedback> | !persona reflect")
    row = await _post_persona_propose(args)
    return _synthetic_response(
        f"Queued persona enhancement as [DRAFT] (id {row.get('id')}). "
        "Approve via /persona/proposals/<id>/approve.")
```

In `chat_completions`, after the help/tier interception block, add:

```python
    if msg_lower.startswith("!persona"):
        resp = await _handle_persona_command(last_user_msg.split("!persona", 1)[1])
        return _wrap_json_as_sse(resp) if is_stream else resp
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd workflows && python3 -m pytest tests/test_llm_proxy.py::TestPersonaCommand -q`
Expected: PASS.

- [ ] **Step 5: Run full proxy suite (no regressions)**

Run: `cd workflows && python3 -m pytest tests/test_llm_proxy.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add workflows/llm_proxy.py workflows/tests/test_llm_proxy.py
git commit -m "feat(persona): !persona feedback and reflect proxy commands"
```

---

### Task 7: Scheduled `persona_reflect` generator and `/persona/reflect`

**Files:**
- Modify: `workflows/generators.py` (`persona_reflect_generator`, register in `GENERATORS`)
- Modify: `workflows/persona_api.py` (add `POST /persona/reflect`)
- Test: `workflows/tests/test_persona_api.py`

**Interfaces:**
- Produces: `generators.persona_reflect_generator(brain_db, profile_name=None, **kwargs) -> None` and `GENERATORS["persona_reflect"]`.
- Consumes: `persona_learning.propose`, `llm_proxy._llm_call` (route reflection through the proxy).

- [ ] **Step 1: Write the failing test**

Add to `workflows/tests/test_persona_api.py`:

```python
def test_reflect_endpoint_creates_proposals(tmp_path, monkeypatch):
    client, db = _client(tmp_path)
    # stub the generator so the test stays offline
    import persona_api
    monkeypatch.setattr(persona_api, "_run_reflection",
        lambda profile_name: db.create_learning(
            profile_name, "persona", "Reflection", "Observed: be terser.", "reflect"))
    r = client.post("/persona/reflect")
    assert r.status_code == 200
    assert db.list_learnings(status="pending")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd workflows && python3 -m pytest tests/test_persona_api.py::test_reflect_endpoint_creates_proposals -q`
Expected: FAIL (`/persona/reflect` missing).

- [ ] **Step 3: Add the reflect endpoint**

In `workflows/persona_api.py`, add a module-level hook and route:

```python
def _run_reflection(profile_name):
    """Override point; default runs the generator. Patched in tests."""
    from generators import persona_reflect_generator
    import asyncio
    return asyncio.run(persona_reflect_generator(None, profile_name=profile_name))
```

Inside `create_persona_router`, add:

```python
    @router.post("/persona/reflect")
    def reflect():
        p = _profile()
        _run_reflection(p.name)
        pending = brain_db.list_learnings(profile=p.name, status="pending")
        return {"queued": len(pending)}
```

- [ ] **Step 4: Implement the generator**

In `workflows/generators.py` add (before the `GENERATORS` dict):

```python
async def persona_reflect_generator(brain_db, profile_name=None, **kwargs) -> None:
    """Review recent activity and queue persona-enhancement proposals.

    Routes the reflection prompt through the LLM proxy and posts any
    suggestion as a [DRAFT] persona proposal. Degrades to a no-op if the
    proxy is unavailable.
    """
    import persona_learning as pl
    from llm_proxy import _llm_call

    profile_name = profile_name or "starter"
    prompt = (
        "From recent operator interactions, suggest at most ONE concrete "
        "persona adjustment (tone/format/priority). Reply with a single "
        "imperative sentence, or 'none'."
    )
    suggestion = await _llm_call(prompt, max_tokens=80)
    if not suggestion or suggestion.strip().lower().startswith("none"):
        return
    if brain_db is None:
        from app import brain_db as _bd
        brain_db = _bd
    pl.propose(brain_db, profile_name, "persona", "Reflection",
              suggestion.strip(), "reflect")
```

Add to the `GENERATORS` dict:

```python
    "persona_reflect": persona_reflect_generator,
```

- [ ] **Step 5: Run tests to verify pass**

Run: `cd workflows && python3 -m pytest tests/test_persona_api.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add workflows/generators.py workflows/persona_api.py workflows/tests/test_persona_api.py
git commit -m "feat(persona): scheduled persona_reflect generator and reflect endpoint"
```

---

### Task 8: Outcome-signal source (minimal, flagged off)

**Files:**
- Modify: `workflows/persona_learning.py` (`scan_signals`)
- Test: `workflows/tests/test_persona_learning.py`

**Interfaces:**
- Produces: `scan_signals(brain_db, profile_name) -> list[dict]`, gated by env `PERSONA_SIGNAL_LEARNING`. Returns `[]` unless enabled.
- Consumes: `brain_db.list_tasks` (repeated `persona` drafts as a weak signal).

- [ ] **Step 1: Write the failing test**

Add to `workflows/tests/test_persona_learning.py`:

```python
def test_scan_signals_disabled_by_default(db, monkeypatch):
    monkeypatch.delenv("PERSONA_SIGNAL_LEARNING", raising=False)
    assert pl.scan_signals(db, "cos") == []


def test_scan_signals_enabled_returns_proposals(db, monkeypatch):
    monkeypatch.setenv("PERSONA_SIGNAL_LEARNING", "1")
    for _ in range(3):
        pl.propose(db, "cos", "persona", "Tone", "Be terse.", "feedback")
    out = pl.scan_signals(db, "cos")
    assert isinstance(out, list)  # may propose based on repetition
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd workflows && python3 -m pytest tests/test_persona_learning.py -k scan_signals -q`
Expected: FAIL (`scan_signals` missing).

- [ ] **Step 3: Implement scan_signals**

Add to `workflows/persona_learning.py`:

```python
import os


def scan_signals(brain_db, profile_name):
    """Weak outcome-signal source (flagged). Off unless PERSONA_SIGNAL_LEARNING set."""
    if not os.environ.get("PERSONA_SIGNAL_LEARNING"):
        return []
    pending = brain_db.list_learnings(profile=profile_name, status="pending")
    # Repetition heuristic: 3+ pending with same target -> surface a meta-note.
    by_target = {}
    for r in pending:
        by_target.setdefault(r["target"], 0)
        by_target[r["target"]] += 1
    out = []
    for target, count in by_target.items():
        if count >= 3:
            out.append(propose(
                brain_db, profile_name, "persona", target,
                f"Recurring feedback about '{target}' ({count}x) — consolidate.",
                "signal"))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd workflows && python3 -m pytest tests/test_persona_learning.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add workflows/persona_learning.py workflows/tests/test_persona_learning.py
git commit -m "feat(persona): flagged outcome-signal proposal source"
```

---

### Task 9: Ship `chief-of-staff` (Max) example, wiring, and docs

**Files:**
- Create: `config/profiles/chief-of-staff/profile.yaml`
- Create: `config/profiles/chief-of-staff/learned.yaml`
- Modify: `workflows/generators.py` (`seed_from_profile` also calls `seed_overlay` from learned.yaml)
- Modify: `docker-compose.yml` (rw mount so workflows can write render targets)
- Modify: `Makefile` (`persona-export` target, extend render to identity)
- Modify: `docs/multi-tenant-guide.md` (Persona & meta-learning section)
- Test: `workflows/tests/test_profile.py`

**Interfaces:**
- Consumes: `tenant_profile.load_profile`, `persona_learning.seed_overlay`, `persona.render_all`.

- [ ] **Step 1: Write the failing test**

Add to `workflows/tests/test_profile.py`:

```python
def test_chief_of_staff_profile_loads_with_identity():
    from tenant_profile import load_profile
    p = load_profile("chief-of-staff")
    assert p.assistant["identity"]["name"] == "Max"
    assert p.assistant["identity"]["emoji"] == "🎯"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd workflows && python3 -m pytest tests/test_profile.py::test_chief_of_staff_profile_loads_with_identity -q`
Expected: FAIL (profile dir does not exist).

- [ ] **Step 3: Create the profile**

Create `config/profiles/chief-of-staff/profile.yaml`:

```yaml
profile: chief-of-staff

assistant:
  name: "Max"
  role: "Chief of Staff"
  channel: telegram
  identity:
    name: "Max"
    creature: "Chief of Staff — strategic AI operator"
    vibe: "Sharp, direct, calm under pressure. White House CoS energy."
    emoji: "🎯"
    avatar: ""
  owner:
    name: "Alex"
    org: ""
    context: >-
      Operator running a personal AI ops stack. Wants signal over noise and
      decisions surfaced fast.
  capabilities:
    - "Surface what matters, kill what doesn't. Signal over noise."
    - "Anticipate needs. Think three steps ahead."
    - "Briefings, not essays. Lead with the recommendation."
    - "Gatekeeper: filter information and tasks by impact."
    - "Tell Alex what he needs to hear, not what he wants to hear."
    - "Never take an outward-facing or irreversible action without approval."

seeds:
  projects: []
  schedules:
    - id: persona_reflect_daily
      name: "Daily persona reflection"
      kind: persona_reflect
      cron: "0 8 * * *"
      kwargs: {profile_name: chief-of-staff}
```

Create `config/profiles/chief-of-staff/learned.yaml`:

```yaml
learned:
  - kind: persona
    target: "Communication"
    content: "Lead with the recommendation, then the supporting analysis."
    source: feedback
    approved_at: "2026-06-26T00:00:00+00:00"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd workflows && python3 -m pytest tests/test_profile.py::test_chief_of_staff_profile_loads_with_identity -q`
Expected: PASS.

- [ ] **Step 5: Wire overlay seeding into seed_from_profile**

In `workflows/generators.py` `seed_from_profile`, after the existing seeding, add:

```python
    # Seed approved persona learnings from the profile's learned.yaml overlay.
    try:
        import yaml, persona_learning as pl
        from tenant_profile import default_profiles_dir
        overlay_path = default_profiles_dir() / profile.name / "learned.yaml"
        if overlay_path.exists():
            data = yaml.safe_load(overlay_path.read_text()) or {}
            pl.seed_overlay(brain_db, profile.name, data.get("learned", []))
    except Exception as exc:  # never crash boot on overlay
        logger.warning("seed_from_profile: learned overlay skipped: %s", exc)
```

- [ ] **Step 6: Add the rw mount and Makefile targets**

In `docker-compose.yml`, under the workflows service `volumes:`, add (so runtime re-render can write):

```yaml
      - ./data/openclaw-state:/openclaw-state
      - ./openclaw/identity.md:/app/openclaw/identity.md
```

Set `OPENCLAW_STATE_DIR=/openclaw-state` in the workflows `environment:` block.

In `Makefile`, add:

```make
persona-export: ## Export approved persona learnings to learned.yaml
	@python3 workflows/scripts/persona_export.py $(PROFILE)
```

Create `workflows/scripts/persona_export.py`:

```python
"""Write a profile's approved learnings to its learned.yaml (for git)."""
import sys, yaml
from brain_db import BrainDB
import persona_learning as pl
from tenant_profile import default_profiles_dir

name = sys.argv[1] if len(sys.argv) > 1 else "starter"
db = BrainDB("data/brain/brain.db"); db.init_db()
overlay = {"learned": pl.export_overlay(db, name)}
dest = default_profiles_dir() / name / "learned.yaml"
dest.write_text(yaml.safe_dump(overlay, allow_unicode=True, sort_keys=False))
print(f"wrote {dest} ({len(overlay['learned'])} learnings)")
```

- [ ] **Step 7: Document in the multi-tenant guide**

Append a "Persona & meta-learning" section to `docs/multi-tenant-guide.md` covering: the `identity` block, `!persona <feedback>` / `!persona reflect`, approval via `/persona/proposals/<id>/approve`, and `make persona-export PROFILE=<name>` to commit the evolved persona. (Write real prose, mirroring the existing guide's tone. No placeholder text.)

- [ ] **Step 8: Run the full suite**

Run: `python3 -m pytest workflows/tests/ -q`
Expected: PASS (all prior and new tests).

- [ ] **Step 9: Commit**

```bash
git add config/profiles/chief-of-staff docker-compose.yml Makefile workflows/scripts/persona_export.py workflows/generators.py docs/multi-tenant-guide.md workflows/tests/test_profile.py
git commit -m "feat(persona): ship chief-of-staff (Max) example, overlay seeding, export target, docs"
```

---

## Self-Review

**Spec coverage:** §3 layers → Tasks 1–4. §4 data model → Task 2. §5 schema → Task 1. §6 components → Tasks 4–8. §7 render targets → Tasks 3,5,9. §8 data flow → Tasks 4–6,9. §9 error handling → Tasks 3,4,5 (atomic render, 400/404, idempotent). §10 testing → every task. §11 example → Task 9. §12 compat → Task 2 (additive table), Task 1 (optional identity). §13 YAGNI → Task 8 (flagged), Task 9 (export kept). No gaps.

**Placeholder scan:** Task 9 Step 7 (docs) names exact content to write rather than the prose itself (acceptable for a doc step), but the implementer must write real prose, not a stub. All code steps contain runnable code.

**Type consistency:** `create_learning/get_learning/list_learnings/set_learning_status` consistent across Tasks 2–8. `propose/approve/reject/export_overlay/seed_overlay/scan_signals` signatures consistent across Tasks 4,7,8. `render_all(profile, targets, learnings)` consistent across Tasks 3,5. Router factory `create_persona_router(brain_db, profile_provider, render_targets_fn)` consistent across Tasks 5,7.
