# Persona Configuration & Meta-Learning (PCM)

Status: approved design (2026-06-26)
Branch: variable-config

## 1. Problem & Goals

Anyone who pulls ClawRange should be able to shape their assistant's
**persona and identity** declaratively (per use case), and the assistant
should be able to **meta-learn**: propose and persist enhancements to its
own persona/identity over time. Our "Max / Chief of Staff" setup becomes a
shipped, reproducible example of the system rather than hand-edited runtime
state.

### Goals
- Identity (name, creature, vibe, emoji, avatar) becomes a first-class,
  profile-driven, per-user configuration, not runtime-only state.
- A meta-learning loop where the assistant proposes persona/identity
  enhancements, gated by operator approval, and those approved enhancements
  persist and feed back into the rendered persona.
- Configuration is variable per use case and reproducible for anyone who
  pulls the repo (git-tracked files), while still being dynamic at runtime
  (brain-backed).
- Ship `chief-of-staff` (Max) as the worked, end-to-end example.

### Non-goals
- No autonomous, un-approved persona changes (the project posture is
  "never an irreversible/outward action without explicit approval").
- No new always-on services. APScheduler stays single-process / single
  uvicorn worker.
- No replacement of the existing profile system. This extends it.

## 2. Existing System (what we build on)

- `config/profiles/<name>/profile.yaml` holds an `assistant` block
  (name, role, channel, owner, capabilities, optional verbatim
  `persona_markdown`). `CLAWRANGE_PROFILE` selects the active profile.
- `workflows/persona.py` renders `openclaw/soul.template.md` →
  `openclaw/soul.md` at setup (`make profile`).
- `workflows/tenant_profile.py` loads/validates/env-resolves the profile.
  `seed_from_profile` seeds projects/schedules into the brain.
- Two persona surfaces exist:
  - `openclaw/soul.md` → mounted read-only as the **config-level persona**.
  - `data/openclaw-state/workspace*/{SOUL,IDENTITY}.md` → the **agent
    runtime workspace** identity (gitignored, agent-evolved). These are
    what was hand-edited for Max.

The tension this design resolves: "others pull the repo and configure"
needs git-tracked files. "The assistant evolves" happens in gitignored
runtime state. The **learned overlay** bridges them.

## 3. Architecture (Approach 1 and benefits of 2 and 3)

Three layers, all extending the profile system:

1. **Config (static, git-tracked).** `profile.yaml` gains an `identity`
   block. A new `openclaw/identity.template.md` renders `IDENTITY.md`.
2. **Learning store (dynamic).** A brain-backed `persona_learnings` table is
   the live store. `config/profiles/<name>/learned.yaml` is its
   git-exportable projection (seed-on-boot, export-on-commit). *(Benefit
   from Approach 3: brain is the dynamic source of truth at runtime.)*
3. **Render composition.** `compose(base and approved learnings)` writes the
   agent's read surfaces **non-destructively**: the base template
   regenerates, and a marked `## Learned` region accumulates approved
   enhancements. *(Benefit from Approach 2: identity genuinely grows. The
   "evolve your soul" feel is preserved, just reproducible and gated.)*

## 4. Data Model

New brain table `persona_learnings`:

| column      | type | notes |
|-------------|------|-------|
| id          | text (uuid) | primary key |
| profile     | text | profile name the learning belongs to |
| kind        | text | `identity` \| `persona` |
| target      | text | identity field (e.g. `vibe`) or persona section label |
| content     | text | the enhancement text |
| status      | text | `pending` \| `approved` \| `rejected` |
| source      | text | `feedback` \| `reflect` \| `scheduled` \| `signal` |
| task_id     | text\|null | linked `[DRAFT]` task id |
| created_at  | text (iso) | |
| decided_at  | text\|null (iso) | set on approve/reject |

`learned.yaml` projection (per profile):

```yaml
learned:
  - kind: persona
    target: "Communication"
    content: "Lead with the recommendation, then the supporting analysis."
    source: feedback
    approved_at: "2026-06-26T..."
```

Seed/export are inverse operations (round-trip symmetry, tested).

## 5. Profile schema additions

```yaml
assistant:
  name: "Max"
  role: "Chief of Staff"
  identity:                 # NEW — renders IDENTITY.md
    name: "Max"
    creature: "Chief of Staff — strategic AI operator"
    vibe: "Sharp, direct, calm under pressure. White House CoS energy."
    emoji: "🎯"
    avatar: ""              # optional; workspace-relative path / URL / data URI
```

`identity.name` defaults to `assistant.name` when omitted (no duplication
required). Validation (in `tenant_profile.validate`): `identity` is optional.
When present, `creature`/`vibe`/`emoji` are strings. `emoji` length-capped.
Unknown identity keys rejected (fail fast at load, per project convention).

## 6. Components

- `workflows/persona.py` *(extend)*: `render_identity(profile)`,
  `compose_persona(profile, learnings)`, `compose_identity(profile, learnings)`,
  `write_identity(...)`, and a `render_all(profile, targets)` that writes the
  configured render targets atomically.
- `workflows/persona_learning.py` *(new)*: `propose()`, `list_proposals()`,
  `approve()`, `reject()`, `apply_approved()` (append to brain and trigger
  re-render), `export_overlay()`, `seed_overlay()`.
- `workflows/persona_api.py` *(new router, mounted in `app.py`)*:
  - `POST /persona/propose {kind, target, content, source}` → store pending
    + create `[DRAFT]` task and Telegram notice
  - `GET  /persona/proposals?status=&profile=`
  - `POST /persona/proposals/{id}/approve` → apply and re-render
  - `POST /persona/proposals/{id}/reject`
  - `POST /persona/reflect` → on-demand reflection (source=reflect)
  - `POST /persona/render` → re-render current profile to targets
  - `GET  /persona/identity` and `GET /persona/soul` → current rendered text
  - `GET  /healthz/persona` → profile, render-target writability, pending count
- **Triggers (pluggable proposal sources, all call `propose()`):**
  1. *Explicit feedback*: a `!persona <feedback>` (and `!learn`) command
     intercepted in `llm_proxy` (reusing the existing command-interception
     pattern) → posts a `persona` proposal.
  2. *On-demand*: `POST /persona/reflect` or `!persona reflect`.
  3. *Scheduled*: new `persona_reflect` generator added to the `GENERATORS`
     registry in `generators.py`, wired via a profile `seeds.schedules` entry.
  4. *Outcome signals* (**flagged, minimal**): `PERSONA_SIGNAL_LEARNING=1`
     enables a recent-pattern analyzer (repeated corrections) that emits
     low-confidence proposals. Off by default (false-signal risk).

## 7. Render Targets

Two write contexts:
- **Setup-time** (host): the existing `make profile` render target is
  extended (it already renders `soul.md`) to also render `identity.md` and
  the workspace files under
  `data/openclaw-state/workspace*/{SOUL,IDENTITY}.md`. Host has full write
  access. No container change needed for setup. (`make persona` may be added
  as a clearer alias for this render step.)
- **Runtime approval** (workflows container): on approve, re-render must
  reach the agent's read surfaces. The workflows service gets a **scoped
  read-write mount** for the render outputs (default: a narrow mount of the
  persona/identity render files, not the whole state dir). A
  `RENDER_TARGETS` setting lists the files to write. If a target is
  unwritable, the approval still records and reports "approved, pending next
  render" naming the failed target (graceful degradation).

Render is **atomic**: write to a temp file in the same dir, validate it is
non-empty and contains the expected anchors, then `os.replace` into place.
Prior render is left intact on any failure.

## 8. Data Flow (end-to-end)

1. **Setup:** `make profile PROFILE=chief-of-staff` (extended render) → load
   profile and `learned.yaml` → render `soul.md`, `IDENTITY.md`, and workspace
   files → seed `persona_learnings` (approved rows) into the brain.
2. **Feedback:** operator says e.g. "lead with the recommendation" →
   `!persona` command → `POST /persona/propose` → pending row, `[DRAFT]`
   task, and Telegram notice.
3. **Approve:** `POST /persona/proposals/{id}/approve` (or `!persona approve
   <id>`) → mark approved in brain → append to learned overlay → atomic
   re-render of targets → confirmation.
4. **Export:** `make persona-export PROFILE=<name>` → write approved
   learnings to `config/profiles/<name>/learned.yaml` for commit. Anyone who
   pulls the repo gets the evolved example.
5. **Scheduled/on-demand reflect:** generator or endpoint reviews recent
   brain/task signals → emits proposals through the same pipeline.

## 9. Error Handling

- Proposal validation: `kind ∈ {identity, persona}`. For `identity`, `target`
  in the identity-field allowlist. Content size cap → `400`.
- Brain-write failure → `503`, no partial state.
- Atomic render with rollback (section 7). Unwritable target → approval
  recorded, response names the failed target.
- Approve/reject idempotent. Deciding an already-decided proposal is a no-op
  returning current state.
- Missing runtime mount → setup-time render still works. Runtime re-render
  degrades to "approved, pending next render".
- Profile load failure never crashes boot (existing posture preserved).

## 10. Testing (TDD)

- `tests/test_persona.py` *(extend)*: render identity from profile.
  `compose_*` merges approved learnings under `## Learned`. Non-destructive
  re-render preserves the Learned region. Max profile renders expected
  identity and persona.
- `tests/test_persona_learning.py` *(new)*: propose→list→approve/reject
  lifecycle. Approve appends overlay and marks brain. `export_overlay` /
  `seed_overlay` round-trip symmetry. Source tagging. Size-cap rejection.
- `tests/test_persona_api.py` *(new)*: auth gate. 400s. Approve triggers
  re-render (target writer mocked). `/persona/reflect` creates proposals.
  `/healthz/persona`.
- `tests/test_profile.py` *(extend)*: identity block validation.
  `chief-of-staff` profile loads and validates.
- `generators`: `persona_reflect` is registered. Scheduled-seed wiring.
- Constraints honored: single uvicorn worker. No new always-on service.

## 11. The Proven Example (Max)

- `config/profiles/chief-of-staff/profile.yaml`: identity `{name: Max,
  creature: "Chief of Staff, strategic AI operator", vibe: "...", emoji:
  "🎯"}`. Persona = Chief of Staff Mode principles. Owner context for Alex.
- `config/profiles/chief-of-staff/learned.yaml`: 1–2 seeded example
  learnings (e.g. "lead with the recommendation") demonstrating the loop.
- `docs/multi-tenant-guide.md` *(extend)*: a "Persona & meta-learning"
  section covering the `identity` block, the `!persona` workflow, approval,
  and `make persona-export`.

## 12. Compatibility & Migration

- Existing profiles (`starter`, `lead-crm`, `marketing`) work unchanged.
  `identity` is optional. Absent `identity` → `IDENTITY.md` is not rendered
  (current behavior). No DB migration beyond an additive
  `persona_learnings` table (created on `init_db` if missing).
- The current hand-edited Max runtime files are superseded by the
  `chief-of-staff` profile render. Setup-time render regenerates them.

## 13. YAGNI Calls

- *Outcome-signal* trigger ships minimal and flagged off by default.
- *Export-to-yaml* round-trip is kept. It is what makes the system
  portable/reproducible for repo-pullers.
- No web UI. Approval is via API/command/Telegram draft (matches existing
  task-approval ergonomics).

## 14. Open Items for the Plan

- Exact scoped rw mount for runtime render targets (narrow file mount vs.
  workspace-dir mount). Choose narrowest that works.
- Whether `openclaw/identity.md` should be a new RO config-level mount the
  agent reads, in addition to workspace `IDENTITY.md`.
- Reflection prompt design for the `persona_reflect` generator (must route
  through the LLM proxy per project convention).
