# ClawRange Multi-Tenant Template — Specification

**Date**: 2026-06-07
**Status**: Approved
**Brief**: docs/multi-tenant-brief.md
**Selected approach**: C — hybrid declarative profile + connector registry

## Overview

Turn ClawRange into a pull-down-and-configure template. A **tenant profile**
(declarative YAML) owns everything tenant-specific: persona, seeded
projects/schedules, connector wiring, and CRM config. A code-level
**connector registry** owns the reusable primitives (source/transform/sink
kinds, CRM adapters, query execution). The active profile is selected by the
`CLAWRANGE_PROFILE` env var (default `marketing`, reproducing today's
behavior exactly).

Two profiles ship:
- `marketing` — the current John-117 / Reddit-GitHub setup, extracted from
  code into config with **no behavior change**.
- `lead-crm` — the reference business profile: scheduled authenticated
  scrape of leads → clean → load into a pluggable CRM (SQLite default) +
  natural-language CRM querying on heartbeat and via Telegram.

## Data Model

### Tenant Profile (`config/profiles/<name>/profile.yaml`)

```yaml
profile: lead-crm                 # must equal the directory name
assistant:
  name: "Acme Sales Assistant"
  # persona_markdown: verbatim soul body (preferred when present);
  # otherwise the template assembles from the structured fields below.
  persona_markdown: null
  role: "sales operations assistant"
  owner:
    name: "Dana Owner"
    org: "Acme Co"
    context: "Acme sells HVAC services; leads come from a web portal."
  capabilities:
    - "Sync new leads from the portal into the CRM every hour"
    - "Answer questions about leads (counts, status, trends)"
  channel: telegram
seeds:
  projects: []                    # marketing-style tracked entities
  schedules:
    - id: lead-sync
      name: "Hourly lead sync"
      kind: pipeline              # generator kind
      cron: "0 * * * *"
      kwargs: {connector: portal-leads}
    - id: crm-digest
      name: "Morning CRM digest"
      kind: crm_digest
      cron: "0 8 * * *"
      kwargs: {queries: [new_leads_count, leads_by_status]}
connectors:
  - id: portal-leads
    source:
      kind: http_csv              # http_csv | login_scrape
      url: ${PORTAL_EXPORT_URL}
      auth:
        kind: bearer              # none | api_key | bearer | basic | login_form
        token: ${PORTAL_TOKEN}    # bearer
        # api_key: {header: X-API-Key, value: ${PORTAL_API_KEY}}
        # basic: {username: ${U}, password: ${P}}
        # login_form: {login_url, username_field, password_field,
        #              username: ${U}, password: ${P}, export_path}
    transform:
      kind: leads_clean
      mapping:                    # source CSV column -> CRM field
        "Full Name": name
        "Email Address": email
        "Lead Source": source
        "Created": created_at
      required: [email]           # drop rows missing these
      dedup_key: email
    sink:
      kind: crm
      object: leads
      upsert_key: email
crm:
  adapter: sqlite                 # sqlite | rest
  sqlite: {path: /data/crm.db}
  rest:
    base_url: ${CRM_BASE_URL}
    auth: {kind: bearer, token: ${CRM_TOKEN}}
    objects: {leads: {create: "POST /leads", list: "GET /leads"}}
  query_templates:
    - name: new_leads_count
      description: "Count of leads created within a recent time window."
      params: {window: {type: duration, default: "7d"}}
      sql: "SELECT COUNT(*) AS n FROM leads WHERE created_at >= :since"
    - name: leads_by_status
      description: "Lead counts grouped by pipeline status."
      params: {}
      sql: "SELECT status, COUNT(*) AS n FROM leads GROUP BY status ORDER BY n DESC"
    - name: leads_over_time
      description: "New-lead counts bucketed by day or week over a window."
      params:
        bucket: {type: enum, values: [day, week], default: week}
        window: {type: duration, default: "30d"}
      sql: "<bucketed time-series; see crm/query.py>"
    - name: lead_lookup
      description: "Find leads matching a name or email fragment."
      params: {q: {type: string}}
      sql: "SELECT * FROM leads WHERE email LIKE :like OR name LIKE :like LIMIT 10"
```

**Env interpolation**: any `${VAR}` in a profile value is resolved from the
environment at load time. Missing required vars degrade gracefully (the
connector/CRM logs a warning and is treated as unconfigured, mirroring the
marketing scanners' graceful-degradation posture).

### Reference CRM schema (SQLite, `crm.db`)

```
leads
├── id          INTEGER PRIMARY KEY
├── name        TEXT
├── email       TEXT UNIQUE              -- upsert key
├── phone       TEXT
├── source      TEXT                     -- portal / referral / ...
├── status      TEXT DEFAULT 'new'       -- new | contacted | qualified | won | lost
├── created_at  TEXT NOT NULL            -- ISO8601 (enables time-series)
└── updated_at  TEXT NOT NULL

activities                               -- supports time-series queries
├── id          INTEGER PRIMARY KEY
├── lead_id     INTEGER REFERENCES leads(id)
├── kind        TEXT                     -- call | email | note | status_change
├── detail      TEXT
└── ts          TEXT NOT NULL            -- ISO8601
```

## Module Layout

```
config/profiles/
  marketing/profile.yaml      # extracted John-117 + 5 projects + 3 schedules
  lead-crm/profile.yaml       # reference business profile
  lead-crm/demo_leads.csv     # fixture leads for the demo
openclaw/
  soul.template.md            # persona template ({{...}} placeholders)
  soul.md                     # GENERATED (gitignored after extraction)
workflows/
  profile.py                  # load/validate/env-resolve profile (<300 lines)
  persona.py                  # render soul.template.md from a profile
  connectors/
    __init__.py               # SOURCES / TRANSFORMS / SINKS registries
    base.py                   # Record dataclass + Protocol types
    sources.py                # http_csv, login_scrape (auth-pluggable)
    transforms.py             # leads_clean / generic column-mapping clean
    sinks.py                  # crm sink (delegates to CRMAdapter)
    pipeline.py               # run_connector(spec, crm) → counts
  crm/
    __init__.py
    adapter.py                # CRMAdapter ABC + get_adapter(crm_cfg)
    sqlite_adapter.py         # SQLiteCRM (default, fully testable)
    rest_adapter.py           # RestCRM (stub proving the seam)
    query.py                  # template params coercion + NL→template router
  generators.py               # + pipeline_generator, crm_digest_generator
  app.py                      # + CRM router, profile-driven lifespan seeding
```

## Functional Requirements

### FR-1: Profile loader (`workflows/profile.py`)
- **FR-1.1** `load_profile(name=None)` reads `config/profiles/<name>/profile.yaml`,
  where `name` defaults to `os.environ["CLAWRANGE_PROFILE"]` or `"marketing"`.
- **FR-1.2** `${VAR}` tokens in any string value resolve from `os.environ`;
  unresolved tokens resolve to empty string and emit a logged warning.
- **FR-1.3** `validate(profile)` raises `ProfileError` on: missing `profile`
  key, `profile` != directory name, unknown source/sink/adapter kind,
  schedule `kind` not in `GENERATORS`, or connector referenced by a schedule
  that is not defined.
- **Acceptance**:
  - Loading `marketing` returns a profile whose `seeds.projects` equals the
    five current `_DEFAULT_PROJECTS` and `seeds.schedules` equals the three
    current `_DEFAULT_SCHEDULES` (seed-equivalence test, byte-level on the
    normalized dicts).
  - Loading a profile with `${PORTAL_TOKEN}` set resolves it; unset → "" + warning.
  - A profile naming an undefined connector in a schedule raises `ProfileError`.

### FR-2: Persona rendering (`workflows/persona.py`)
- **FR-2.1** `render_persona(profile) -> str` produces markdown. If
  `assistant.persona_markdown` is set, it is used verbatim as the body;
  otherwise the template assembles name/role/owner-context/capabilities.
- **FR-2.2** `write_soul(profile, path="openclaw/soul.md")` writes the result.
- **Acceptance**:
  - Rendering `marketing` produces a soul containing "John-117" and the
    current owner context (persona_markdown holds the existing soul body).
  - Rendering `lead-crm` (no persona_markdown) produces a soul containing the
    assistant name, role, owner org, and each capability line.
  - No string from the generic core/template contains "John-117", "Alex",
    "Eridanus", or "the company" (genericity guard test).

### FR-3: Connector framework (`workflows/connectors/`)
- **FR-3.1 Sources** return a list of raw dict rows.
  - `http_csv`: GET the URL with optional auth, parse CSV → rows.
  - `login_scrape`: perform a `login_form` POST to establish an httpx session,
    then GET `export_path` and parse CSV → rows.
  - Auth kinds supported by both where applicable: `none`, `api_key`
    (custom header), `bearer`, `basic`, `login_form`.
- **FR-3.2 Transforms** map/clean rows.
  - `leads_clean`: apply `mapping` (rename columns), drop rows missing any
    `required` field, dedupe on `dedup_key` (last-wins), trim whitespace,
    default `created_at` to now when absent, default `status` to `new`.
- **FR-3.3 Sinks** persist records via the CRM adapter.
  - `crm` sink calls `adapter.upsert(object, records, upsert_key)`.
- **FR-3.4** `run_connector(spec, crm, *, http_client=None) -> {fetched, kept, written}`
  chains source→transform→sink and returns counts. `http_client` is injectable
  for tests.
- **Acceptance**:
  - `http_csv` against a local fixture server returns parsed rows; bearer and
    api-key auth send the expected headers (assert on fixture server).
  - `login_scrape` against a fixture login+export server authenticates then
    downloads (asserts the session cookie gates the export).
  - `leads_clean` renames, drops `required`-missing rows, and dedupes.
  - `run_connector` end-to-end loads cleaned rows into the SQLite CRM and the
    returned counts match.

### FR-4: Pluggable CRM (`workflows/crm/`)
- **FR-4.1** `CRMAdapter` ABC: `init()`, `upsert(object, records, upsert_key)
  -> int`, `list(object, filters) -> list[dict]`, `run_template(template,
  params) -> list[dict]`, `health() -> dict`.
- **FR-4.2** `SQLiteCRM` implements all methods; `run_template` binds params
  into the template SQL over a **read-only** connection; `leads_over_time`
  builds day/week buckets. Schema auto-created on `init()`.
- **FR-4.3** `RestCRM` is a documented stub: `upsert`/`list` map to the
  profile's `objects` endpoints via httpx; `run_template` raises
  `NotImplementedError("define server-side reports")` with a clear message.
  Its purpose is to prove the seam (selectable via `crm.adapter: rest`).
- **FR-4.4** `get_adapter(crm_cfg)` returns the adapter named by
  `crm_cfg["adapter"]`.
- **Acceptance**:
  - `SQLiteCRM.upsert` inserts new + updates existing on `upsert_key`,
    returning the affected count.
  - `run_template("new_leads_count", {window: "7d"})` returns the correct
    count given seeded rows with known `created_at`.
  - `run_template("leads_over_time", {bucket: "week"})` returns one row per
    week bucket with counts.
  - Switching the profile to `adapter: rest` yields a `RestCRM` from
    `get_adapter` (seam test); `run_template` raises the documented error.
  - A query template SQL is executed read-only: an attempt to run a template
    whose SQL mutates is rejected (guard test).

### FR-5: NL query router (`workflows/crm/query.py`)
- **FR-5.1** `coerce_params(template, raw)` validates/coerces params by type
  (`duration` → `:since` ISO timestamp; `enum` → membership check; `string`
  → `:like` `%q%`); defaults fill missing.
- **FR-5.2** `route_nl(prompt, templates, *, llm=_llm_call)` asks the LLM to
  choose a template name + params as JSON, given the template
  names/descriptions/params. Returns `{template, params}`. `llm` is injectable
  for tests.
- **FR-5.3** `answer(prompt, adapter, templates, *, llm=...)` = route → coerce
  → `adapter.run_template` → deterministic NL formatting of the rows.
- **Acceptance**:
  - With a stub LLM returning `{"template":"new_leads_count","params":{"window":"7d"}}`,
    `answer("how many leads this week?", ...)` returns a string containing the count.
  - `coerce_params` rejects an out-of-enum bucket and an unknown template.
  - A malformed LLM response (non-JSON / unknown template) yields a safe
    fallback message, not an exception.

### FR-6: Generators (scheduler-driven, `workflows/generators.py`)
- **FR-6.1** `pipeline_generator(brain_db, connector, profile=None, **kw)`
  loads the named connector from the active profile, runs `run_connector`
  against the app CRM, updates schedule status with counts, and (if any rows
  written) posts a one-line Telegram summary.
- **FR-6.2** `crm_digest_generator(brain_db, queries, profile=None, **kw)`
  runs each named query template and delivers a formatted Telegram digest.
- **FR-6.3** Both register in `GENERATORS` as `pipeline` and `crm_digest`.
- **Acceptance**:
  - `pipeline` registered; running it with the fixture connector writes leads
    and updates schedule status to `ok (N written)`.
  - `crm_digest` formats `new_leads_count` + `leads_by_status` into a digest
    string and calls `telegram.notify`.

### FR-7: HTTP API (`workflows/app.py` + CRM router)
- New endpoints (mounted only when the active profile defines `crm`):
  - `POST /crm/query` `{prompt}` → `{answer, template, params}`
  - `POST /crm/query/run` `{template, params}` → `{rows}`
  - `GET  /crm/templates` → available templates (name/description/params)
  - `GET  /crm/leads?status=&limit=` → rows
  - `POST /crm/sync/{connector_id}` → run a connector now → counts
  - `GET  /healthz/crm` → adapter health + configured connectors
- **Acceptance**: each endpoint returns the documented shape against the
  bundled SQLite CRM in `test_app`-style tests; `/healthz/crm` reports
  `configured` per the active profile.

### FR-8: Profile-driven lifespan + extraction (`workflows/app.py`)
- **FR-8.1** Lifespan loads the active profile, renders the persona to
  `openclaw/soul.md` if missing/stale, seeds projects+schedules from the
  profile (replacing `seed_default_projects`'s hardcoded constants), and —
  when `crm` is present — inits the CRM adapter on `app.state.crm`.
- **FR-8.2** `seed_from_profile(brain_db, profile)` replaces the hardcoded
  `_DEFAULT_PROJECTS`/`_DEFAULT_SCHEDULES`; those constants are deleted only
  after the seed-equivalence test (FR-1) passes.
- **Acceptance**: with `CLAWRANGE_PROFILE` unset, boot seeds the five
  marketing projects + three schedules exactly as before (existing
  `test_marketing` / `test_app` seed assertions stay green).

### FR-9: Setup & ops (`Makefile`, `.env.example`, docs)
- **FR-9.1** `make profile PROFILE=<name>` renders `openclaw/soul.md` and sets
  `CLAWRANGE_PROFILE` in `.env`.
- **FR-9.2** `make seed-demo` loads `config/profiles/lead-crm/demo_leads.csv`
  into `crm.db` so the example works offline with no real portal.
- **FR-9.3** `.env.example` gains: `CLAWRANGE_PROFILE`, `PORTAL_EXPORT_URL`,
  `PORTAL_TOKEN`/`PORTAL_API_KEY`, `PORTAL_USERNAME`/`PORTAL_PASSWORD`,
  `CRM_BASE_URL`/`CRM_TOKEN`. `requirements.txt` pins `pyyaml==6.0.1`.
- **FR-9.4** A "Configure ClawRange for your business" guide
  (`docs/multi-tenant-guide.md`) documents profile authoring, the connector
  kinds, auth options, CRM adapter swap, and query templates.

## Non-Functional Requirements
- Single uvicorn worker safe (no shared mutable global beyond existing pattern).
- All LLM calls via `llm_proxy._llm_call`.
- Graceful degradation: unconfigured connector/CRM never crashes boot.
- New Python modules stay under ~300 lines each (split when larger).

## Out of Scope (Won't Have, this branch)
- Headless-browser scraping for JS-rendered portals (documented extension).
- LLM-generated raw SQL (explicitly rejected; templates only).
- A full REST CRM adapter implementation (only a seam-proving stub).
- Multi-tenant in a single process (one profile per deployment).
- A web UI for profile editing.
- Auth/RBAC on the new `/crm/*` endpoints beyond existing proxy/gateway posture.

## Test Strategy
- **Unit**: `test_profile.py` (load/validate/env + seed-equivalence),
  `test_persona.py` (render + genericity guard), `test_connectors.py`
  (sources w/ fixture server, transforms, pipeline), `test_crm.py`
  (SQLite adapter, templates, time-series, read-only guard, REST seam),
  `test_crm_query.py` (coerce/route/answer with stub LLM),
  additions to `test_app.py` (CRM endpoints) and `test_marketing.py`
  (seed parity).
- **Fixtures**: a local `pytest` fixture HTTP server (stdlib `http.server` or
  httpx `MockTransport`) for http_csv + login_scrape; seeded `crm.db` for
  query tests.
- **Regression lock**: marketing seed-equivalence test must pass before the
  hardcoded constants are removed (TDD: write it RED against the new loader,
  GREEN by populating the marketing profile).

## Next Steps
1. `Skill(attune:project-planning)` — dependency-ordered task plan
2. `Skill(attune:project-execution)` — TDD implementation
