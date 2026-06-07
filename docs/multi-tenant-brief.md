# ClawRange Multi-Tenant Template — Project Brief

**Date**: 2026-06-07
**Author**: the operator (mission: attune full lifecycle)
**Status**: Approved
**Branch**: feat/replace-n8n-with-fastapi
**Artifacts**: docs/multi-tenant-brief.md → docs/multi-tenant-spec.md → docs/multi-tenant-plan.md

## Problem Statement

**Who**: A business owner (not Alex) who wants their own ClawRange-style
assistant for their own operational needs.

**What**: ClawRange is currently hardwired to one tenant. The assistant
persona (`openclaw/soul.md` = "John-117", knows Alex's family, employer,
GitHub), the seeded work (`_DEFAULT_PROJECTS` / `_DEFAULT_SCHEDULES` in
`generators.py` = Alex's five marketing repos), and the entire capability
set (Reddit/GitHub marketing scanners) are baked into source code. Pulling
the repo down for a *different* purpose means editing Python.

**Where**: A fresh clone on a separate single host (Docker), configured by
someone who is not the original author and may not be a Python developer.

**When**: At deploy time, and continuously as the business owner tunes
their assistant's behavior.

**Why**: The architecture is genuinely reusable — a scheduler, an
LLM proxy, a task queue, a persistent brain, and a Telegram channel are
business-agnostic infrastructure. But the reuse is blocked by hardcoded
single-tenant content. The cost of the gap: ClawRange can only ever be
Alex's assistant.

**Current State**: DB-backed config exists for *projects* and *schedules*
(editable via `/projects`, `/sched`), and LLM tiers live in `models.json`.
But the **persona**, the **default seeds**, and the **capability set**
(what generators/connectors exist) are all in code. There is no
source→transform→sink connector concept, and no CRM/structured-data
capability at all.

## Goals

1. **Genericize the tenant surface** — extract everything Alex-specific
   (persona, seeded projects/schedules, marketing defaults) out of code
   and into a declarative **tenant profile**. The current marketing setup
   becomes "just another profile" with zero behavior change.
2. **Add a connector framework** — a Source → Transform → Sink pipeline
   abstraction the existing scheduler can drive, so new tenants wire up
   data flows declaratively instead of writing generators.
3. **Ship a working reference profile** — the lead/CRM bot: scheduled
   scrape of leads from a portal → clean → load into a CRM, plus
   natural-language querying of that CRM (relational + time-series) on
   heartbeat and via Telegram. The CRM is **pluggable**: a local SQLite
   CRM is the default, fully-testable backend, but the sink/query layer
   talks to a `CRMAdapter` interface so a REST/SaaS CRM can be dropped in
   without touching the pipeline. The scrape **source accepts auth
   params** (API key / bearer token / basic auth / login-form session)
   from the profile + `.env`, so it works against export-link, API-export,
   and login-gated portals.
4. **Keep it pull-down-and-configure** — a documented setup path that
   renders the persona, seeds the profile, and starts the stack without
   editing Python.

## Constraints

### Technical
- **Single uvicorn worker** (hard requirement — APScheduler runs
  single-process). Connectors and CRM queries must be safe under one
  worker; no multi-process assumptions.
- **All LLM calls route through `llm_proxy`** — the CRM NL-query router
  must call the proxy, never a provider SDK directly.
- **SQLite for persistence** — the brain DB pattern (`brain_db.py`) is the
  house style. The reference CRM uses its own SQLite DB via the same
  contract-first wrapper approach.
- **Additive to the test suite** — the existing pytest suite (test_app,
  test_brain*, test_llm_proxy, test_marketing, test_research, test_telegram,
  test_tome_bridge) must stay green. The marketing profile must produce
  byte-identical seeds to today's hardcoded constants.
- **Bash scripts portable** (macOS + Ubuntu); `:latest` images for testing.
- **New dependency budget**: `pyyaml` (already present at 6.0.1, needs
  pinning). Avoid heavyweight deps; prefer stdlib + httpx (already pinned).

### Resources
- **Timeline**: This branch (directive: ignore scope guard, stay on branch).
- **Team**: Solo + Claude Code, TDD discipline.

### Integration
- OpenClaw reads `openclaw/soul.md` from a bind mount → persona must be
  rendered to that path at setup time (OpenClaw is a separate container).
- Existing `GENERATORS` registry, `schedules`/`projects` tables, and
  `app.include_router` are the extension points.
- Telegram delivery via existing `telegram.notify`.

### Compliance / Safety
- **No arbitrary SQL from the LLM** — CRM querying uses parameterized
  templates the LLM selects; the model never emits raw SQL.
- **Scrape credentials** live in `.env` (gitignored), referenced by the
  profile; never committed.
- **No auto-posting / no destructive external writes** without the same
  draft-and-approve posture the marketing side already uses.

### Success Criteria
- [ ] `CLAWRANGE_PROFILE=marketing` reproduces today's exact seeds and
      behavior (regression-locked by tests).
- [ ] `CLAWRANGE_PROFILE=lead-crm` boots a working lead pipeline +
      CRM query capability against the bundled SQLite CRM.
- [ ] CRM access goes through a `CRMAdapter` interface; the SQLite adapter
      is the default, and a second (REST) adapter stub proves the seam is
      real (swappable via profile config, no pipeline edits).
- [ ] The scrape source authenticates via profile-declared auth params
      (api_key / bearer / basic / login-form), with secrets resolved from
      `.env`; unit-tested against a local fixture server for at least the
      api-key/bearer and login-form paths.
- [ ] Persona for either profile renders to `openclaw/soul.md` from a
      template with no Alex-specific leakage in the generic core.
- [ ] A new profile can be added by dropping a YAML file + (optional)
      persona template — no Python edits for the declarative path.
- [ ] Connector primitives (source/transform/sink) and query templates
      are unit-tested with fixtures (local fixture server for the scrape).

## Approaches Considered

### Approach A: Profile-as-data + connector registry

**Description**: Profiles are pure YAML loaded at boot; connector
primitives are registered functions mirroring the existing `GENERATORS`
dict; a new `pipeline` generator kind reads a connector spec from schedule
kwargs.

**Stack**: pyyaml, existing APScheduler, SQLite, httpx.

**Pros**:
- Maximum consistency with the existing registry pattern.
- Business owners edit YAML only.
- Fully testable; no per-tenant code.

**Cons**:
- Pure-data profiles can't express genuinely custom logic.
- Risk of an over-general YAML "mini-language".

**Risks**: YAML schema sprawl (medium). Connector spec expressiveness
ceiling (low — escape hatch exists via custom generators).

**Effort**: L

### Approach B: Profile-as-plugin (Python package per tenant)

**Description**: Each profile is a Python package exposing hooks
(seeds, connectors, persona). Loaded by import.

**Stack**: Python entry points / dynamic import.

**Pros**: Unlimited per-tenant power; natural for developers.

**Cons**: Defeats "business owner, not a Python dev" goal; heavier
coupling; harder to sandbox; bigger blast radius.

**Risks**: Import-time side effects, security of loading tenant code
(high). Onboarding friction for non-devs (high).

**Effort**: XL

### Approach C: Hybrid — declarative YAML profile + code connector registry ⭐

**Description**: Declarative YAML profile owns the *tenant surface*
(persona fields, seeds, schedules, connector wiring, CRM config, query
templates). A code-level **connector registry** owns the *primitives*
(source kinds, transform kinds, sink kinds, CRM adapters). Profiles
reference primitives by name. Developers extend primitives in Python;
operators compose them in YAML.

**Stack**: pyyaml, APScheduler (existing), SQLite (CRM + brain), httpx
(scrape + REST), llm_proxy (NL query router).

**Pros**:
- Clean separation: operators compose, developers extend.
- "Business owner configures without Python" for the common path; full
  power available by adding a primitive.
- Reuses scheduler, task queue, brain, Telegram unchanged.
- Marketing setup extracts cleanly into the `marketing` profile.

**Cons**:
- Two layers to learn (profile YAML + primitive registry).
- Requires a persona render step (template → soul.md).

**Risks**: Extraction regressions in the working marketing path
(mitigated by seed-equivalence tests). Profile schema drift (mitigated by
a validated loader with a typed schema).

**Effort**: L–XL

## Approach Comparison

| Criterion | A: data+registry | B: plugin | C: hybrid ⭐ |
|-----------|------------------|-----------|--------------|
| Technical Fit | 🟢 High | 🟡 Medium | 🟢 High |
| Non-dev usability | 🟢 High | 🔴 Low | 🟢 High |
| Extensibility | 🟡 Medium | 🟢 High | 🟢 High |
| Blast radius | 🟢 Low | 🔴 High | 🟡 Medium |
| Testability | 🟢 High | 🟡 Medium | 🟢 High |
| Maintainability | 🟡 Medium | 🟡 Medium | 🟢 High |

## War Room Assessment (Reversibility Gate)

**Reversibility Score: ~0.80 (HIGH — Type 2, reversible).** All work is
contained on `feat/replace-n8n-with-fastapi`, not merged to `main`. The
extraction is structural and git-revertible; the marketing path is
regression-locked by seed-equivalence tests. There is no irreversible
external side effect (no prod migration, no destructive data op).

**War Room bypass justified** under the skill's conditions: Type 2
(RS > 0.40, clearly reversible), a single clearly-superior approach (C)
after the user pre-selected the four key forks, well-documented patterns
(registry mirrors existing `GENERATORS`), and an explicit user directive
to proceed autonomously ("ignore scope guard, do on this branch"). The RS
assessment is recorded here in lieu of the full multi-LLM panel.

## Selected Approach: C — Hybrid declarative profile + connector registry ⭐

### Rationale

The user's four design selections collectively *are* a vote for C:
**full template extraction** (persona + seeds out of code), a **local
SQLite CRM** reference (testable primitive in the registry),
**authenticated session scrape** (a source primitive), and **templated
queries + LLM router** (a code-level query layer composed via profile
config). C is the only approach that holds those four together while
keeping the non-developer composition path.

C also respects the codebase's grain: it mirrors the `GENERATORS`
registry, reuses the DB-backed `schedules`/`projects` tables, and treats
the persona as a render artifact — matching how `soul.md` is already a
mounted file OpenClaw consumes.

### Trade-offs Accepted
- **Two-layer model (YAML + registry)** → Mitigation: a single documented
  profile schema + a `make profile` render/seed command; the registry is
  invisible to operators using existing primitives.
- **Extraction touches working marketing code** → Mitigation: seed
  equivalence tests assert the `marketing` profile yields byte-identical
  projects/schedules to today's constants before deleting them.
- **Scrape path is heavier/less reliable to test** → Mitigation: a local
  fixture login+export server in tests; the source is auth-pluggable
  (api_key / bearer / basic / login-form). Headless-browser scraping (JS
  portals) is a documented extension point, not in initial scope.
- **Pluggable CRM raises the abstraction cost up front** → Mitigation:
  keep the `CRMAdapter` interface minimal (create/upsert leads, run a
  named query template); SQLite adapter is the reference implementation
  and the REST adapter ships as a thin, documented stub proving the seam.

### Rejected Approaches
- **B (plugin)**: Rejected — defeats the non-developer goal and adds
  tenant-code-loading security surface.
- **A (pure data)**: Rejected as the *primary* model but effectively
  subsumed — C's profile layer IS approach A; C adds the code registry so
  the CRM/scrape primitives have a real home.

## Next Steps
1. `Skill(attune:project-specification)` — detailed spec
2. `Skill(attune:project-planning)` — architecture + dependency-ordered tasks
3. `Skill(attune:project-execution)` — TDD implementation
