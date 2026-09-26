# ClawRange Multi-Tenant Template: Implementation Plan

**Date**: 2026-06-07
**Status**: Approved
**Spec**: docs/multi-tenant-spec.md

## Architecture Components

| Component | Module(s) | Responsibility | Consumes |
|-----------|-----------|----------------|----------|
| Profile loader | `workflows/profile.py` | Load/validate/env-resolve YAML profile | pyyaml, env |
| Persona renderer | `workflows/persona.py`, `openclaw/soul.template.md` | Profile → `soul.md` | profile |
| Connector registry | `workflows/connectors/__init__.py`, `base.py` | Name→primitive lookup, Record type | none |
| Sources | `workflows/connectors/sources.py` | Fetch raw rows (http_csv, login_scrape) w/ auth | httpx |
| Transforms | `workflows/connectors/transforms.py` | Clean/map/dedup rows | none |
| Sinks | `workflows/connectors/sinks.py` | Persist via CRM adapter | crm |
| Pipeline | `workflows/connectors/pipeline.py` | Chain source→transform→sink | registry, crm |
| CRM adapter | `workflows/crm/adapter.py` | `CRMAdapter` ABC and factory | none |
| SQLite CRM | `workflows/crm/sqlite_adapter.py` | Default backend, read-only queries | sqlite3 |
| REST CRM | `workflows/crm/rest_adapter.py` | Seam-proving stub | httpx |
| Query layer | `workflows/crm/query.py` | Param coercion and NL→template router | llm_proxy |
| Generators | `workflows/generators.py` (+2) | `pipeline`, `crm_digest` jobs | pipeline, crm, telegram |
| API | `workflows/app.py` (+ CRM router) | `/crm/*`, profile-driven lifespan | all |

## File Structure

| Path | New/Mod | Task | Notes |
|------|---------|------|-------|
| `workflows/requirements.txt` | Mod | T01 | pin `pyyaml==6.0.1` |
| `.env.example` | Mod | T01 | profile, portal, and crm vars |
| `config/profiles/marketing/profile.yaml` | New | T03 | extracted marketing seeds |
| `config/profiles/lead-crm/profile.yaml` | New | T17 | reference profile |
| `config/profiles/lead-crm/demo_leads.csv` | New | T17 | offline demo fixture |
| `workflows/profile.py` | New | T02 | loader/validator |
| `workflows/persona.py` | New | T04 | renderer |
| `openclaw/soul.template.md` | New | T04 | persona template |
| `openclaw/soul.md` | Mod→gen | T04 | becomes generated. Gitignore |
| `workflows/connectors/__init__.py` | New | T10 | registries |
| `workflows/connectors/base.py` | New | T10 | Record and protocols |
| `workflows/connectors/sources.py` | New | T11/T12 | http_csv, login_scrape |
| `workflows/connectors/transforms.py` | New | T13 | leads_clean |
| `workflows/connectors/sinks.py` | New | T14 | crm sink |
| `workflows/connectors/pipeline.py` | New | T14 | run_connector |
| `workflows/crm/__init__.py` | New | T06 | package |
| `workflows/crm/adapter.py` | New | T06 | ABC and factory |
| `workflows/crm/sqlite_adapter.py` | New | T06 | default backend |
| `workflows/crm/rest_adapter.py` | New | T08 | stub |
| `workflows/crm/query.py` | New | T07/T09 | templates and NL router |
| `workflows/generators.py` | Mod | T05/T15 | seed_from_profile, 2 gens |
| `workflows/app.py` | Mod | T05/T16/T17 | lifespan and CRM router |
| `Makefile` | Mod | T18 | profile, seed-demo |
| `docs/multi-tenant-guide.md` | New | T18 | operator guide |
| `workflows/tests/test_profile.py` | New | T02/T03 | loader and seed parity |
| `workflows/tests/test_persona.py` | New | T04 | render and genericity guard |
| `workflows/tests/test_connectors.py` | New | T11-14 | sources/transforms/pipeline |
| `workflows/tests/test_crm.py` | New | T06-08 | adapter, templates, and seam |
| `workflows/tests/test_crm_query.py` | New | T09 | coerce/route/answer |
| `workflows/tests/test_app.py` | Mod | T16 | CRM endpoints |
| `workflows/tests/test_marketing.py` | Mod | T03/T05 | seed equivalence |

## Tasks (dependency-ordered, TDD)

### Sprint 0: Profile foundation & marketing extraction (regression spine)

- **T01** Deps & env scaffold. Pin pyyaml, add env vars, and create `config/profiles/`.
  Deps: none. Effort: S. AC: `pip install` resolves. `.env.example` documents new vars.
- **T02** `profile.py` loader/validator (FR-1). RED `test_profile.py` for
  load, `${VAR}` resolve, and validation errors, then GREEN.
  Deps: T01. Effort: M. AC: FR-1.1–1.3 acceptance.
- **T03** Marketing profile extraction and seed-equivalence (FR-1 AC, FR-8.2).
  RED: test asserts `load_profile("marketing")` seeds == current
  `_DEFAULT_PROJECTS`/`_DEFAULT_SCHEDULES`. GREEN: author
  `marketing/profile.yaml`. Deps: T02. Effort: M. AC: byte-equal normalized dicts.
- **T04** `persona.py` and `soul.template.md` (FR-2). RED `test_persona.py`
  (marketing contains John-117, lead-crm structured, genericity guard).
  Move current `soul.md` body into marketing `persona_markdown`, and gitignore
  generated `soul.md`. Deps: T02. Effort: M. AC: FR-2 acceptance.
- **T05** `seed_from_profile` and lifespan wiring. Delete hardcoded constants
  (FR-8). Deps: T03, T04. Effort: M. AC: boot with no env seeds marketing
  exactly. Existing `test_marketing`/`test_app` green.

### Sprint 1: Pluggable CRM core

- **T06** `crm/adapter.py` ABC and factory. `crm/sqlite_adapter.py`
  (schema/init/upsert/list/health and read-only run_template) (FR-4.1, 4.2, 4.4).
  RED `test_crm.py`. Deps: T01. Effort: L. AC: FR-4 upsert and read-only guard.
- **T07** Query templates and `leads_over_time` time-series in `query.py`
  (FR-5.1, FR-4.2). RED time-series and coerce tests. Deps: T06. Effort: M.
  AC: count/group/time-series correct. Param coercion.
- **T08** `crm/rest_adapter.py` stub and seam test (FR-4.3). Deps: T06. Effort: S.
  AC: `get_adapter({adapter:rest})` → RestCRM. `run_template` raises documented error.
- **T09** NL router `route_nl` and `answer` with injectable llm (FR-5.2, 5.3).
  RED `test_crm_query.py` w/ stub LLM and malformed-response fallback.
  Deps: T07. Effort: M. AC: FR-5 acceptance.

### Sprint 2: Connector framework

- **T10** `connectors/base.py` and registries (FR-3). Deps: T01. Effort: S.
- **T11** `sources.py` `http_csv` and auth (none/api_key/bearer/basic) (FR-3.1).
  RED fixture-server test asserting auth headers. Deps: T10. Effort: M.
- **T12** `sources.py` `login_scrape` (login_form session) (FR-3.1). RED
  fixture login+export test. Deps: T11. Effort: M.
- **T13** `transforms.py` `leads_clean` (FR-3.2). RED rename/drop/dedupe test.
  Deps: T10. Effort: S.
- **T14** `sinks.py` crm sink and `pipeline.py` `run_connector` (FR-3.3, 3.4).
  RED end-to-end test (source→transform→SQLite CRM counts). Deps: T11, T13, T06.
  Effort: M. AC: FR-3.4 acceptance.

### Sprint 3: Generators, API, reference profile, docs

- **T15** `pipeline_generator`, `crm_digest_generator`, and registry (FR-6).
  RED generator tests. Deps: T14, T09. Effort: M.
- **T16** CRM API router and `/healthz/crm`, profile-conditional mount (FR-7).
  RED `test_app.py` additions. Deps: T14, T09. Effort: M.
- **T17** `lead-crm/profile.yaml` and `demo_leads.csv`. Lifespan CRM init on
  `app.state.crm` (FR-8.1 crm branch). Deps: T05, T16. Effort: M. AC:
  `CLAWRANGE_PROFILE=lead-crm` boots pipeline and CRM query.
- **T18** Makefile (`profile`, `seed-demo`), `.env.example`, guide doc,
  README/CLAUDE.md updates (FR-9). Deps: T17. Effort: M.
- **T19** Full suite green, `make test-unit`/dogfood, and proof-of-work evidence
  capture. Deps: all. Effort: S.

## FR → Task Coverage

| FR | Tasks |
|----|-------|
| FR-1 profile loader | T02, T03 |
| FR-2 persona | T04 |
| FR-3 connectors | T10, T11, T12, T13, T14 |
| FR-4 pluggable CRM | T06, T08, (T07 time-series) |
| FR-5 NL query | T07, T09 |
| FR-6 generators | T15 |
| FR-7 HTTP API | T16 |
| FR-8 lifespan/extraction | T05, T17 |
| FR-9 setup/ops | T01, T18 |

## Critical Path

`T01 → T02 → T03 → T05` (marketing extraction, regression-sensitive) is the
spine. After **T02**, two tracks run in parallel:
- CRM track: `T06 → T07 → T09` (+ T08)
- Connector track: `T10 → T11 → T12/T13 → T14`
Both converge at **T14 → T15/T16 → T17 → T18 → T19**.

Longest chain: T01→T02→T06→T07→T09→T15→T17→T18→T19 (and the parallel
T10→T11→T14 must complete before T15). Critical path ≈ 9 tasks.

## Dependency Graph (acyclic)

```
T01─┬─T02─┬─T03─┬─T05─────────────┐
    │     │     └(T04)──┘         │
    │     ├─T06─┬─T07─┬─T09─┐     │
    │     │     └─T08 │     │     │
    │     └─T10─┬─T11─┴─T12 │     │
    │           └─T13─┐     │     │
    │            T11+T13+T06→T14──┤
    └───────────────────────────T15,T16─T17─T18─T19
```

## Risks & Mitigations

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Marketing extraction regresses seeds | Med | Seed-equivalence test RED-first (T03) before deleting constants (T05) |
| Persona render drifts from current soul.md | Med | Marketing uses verbatim `persona_markdown`. Genericity guard test (T04) |
| login_scrape flaky/untestable | Med | Local fixture login+export server. Browser path out of scope |
| LLM router nondeterminism in tests | Low | `llm` injected. Tests use a stub, never a live call |
| Read-only SQL guard incomplete | Med | Open query connection read-only and reject non-SELECT templates (T06) |
| `/crm/*` mounted when profile lacks crm | Low | Conditional mount on `profile.crm` presence (T16) |
| Scope sprawl (RED branch) | High | Directive: ignore scope guard. Still gated by per-task AC and tests |

## Sprint Balance

| Sprint | Tasks | Effort | Theme |
|--------|-------|--------|-------|
| 0 | T01-T05 | S+M+M+M+M | Profile foundation and extraction |
| 1 | T06-T09 | L+M+S+M | Pluggable CRM core |
| 2 | T10-T14 | S+M+M+S+M | Connector framework |
| 3 | T15-T19 | M+M+M+M+S | Generators/API/profile/docs |

## Next Steps
- `Skill(attune:project-execution)`: execute T01… in order, TDD per task.
