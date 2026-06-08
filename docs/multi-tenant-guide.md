# Configure ClawRange for Your Business

ClawRange ships as a **pull-down-and-configure template**. Everything that
is specific to one operator — the assistant's persona, what it tracks, where
it pulls data from, and what CRM it writes to — lives in a single declarative
**tenant profile** (`config/profiles/<name>/profile.yaml`). The reusable
machinery (connectors, CRM adapters, query execution) lives in code and is
shared by every profile.

Three profiles ship in the box, all identity-free:

- **`starter`** — the default (`CLAWRANGE_PROFILE` unset → `starter`). A
  generic, identity-free baseline so a fresh clone runs without carrying
  anyone's personal data. Empty seeds; the persona renders from
  `openclaw/soul.template.md`.
- **`marketing`** — a worked content-marketing example with a verbatim
  John-117 persona and example open-source projects/schedules. Edit it to
  point at your own products.
- **`lead-crm`** — a worked business example: an assistant that hourly syncs
  leads from a web portal into a local CRM and answers relational and
  time-series questions about them on a schedule or via a Telegram prompt.

To stand up your own assistant, copy one of these, rename it, and edit YAML.
Operator-private profiles named `local-*` are gitignored, so your real
identity never gets committed.

```bash
cp -r config/profiles/lead-crm config/profiles/acme
$EDITOR config/profiles/acme/profile.yaml      # set profile: acme
make profile PROFILE=acme                        # render soul.md + set .env
```

## 1. The profile at a glance

```yaml
profile: acme            # MUST equal the directory name
assistant: {...}         # persona: who the bot is, who it works for
seeds:
  projects: []           # marketing-style tracked entities (optional)
  schedules: [...]       # standing cron jobs (generator + cron + kwargs)
connectors: [...]        # source -> transform -> sink data pipelines
crm: {...}               # backend adapter + named query templates
```

`${VAR}` tokens in any value are resolved from the environment at load time.
An **unset variable resolves to `""`** and the affected connector or CRM is
treated as *unconfigured* — it logs a warning and is skipped rather than
crashing boot. Put real secrets in `.env` (see `.env.example`), never in the
committed profile.

## 2. Persona (`assistant`)

Two ways to define the persona:

- **Structured** (recommended for new tenants): set `name`, `role`,
  `owner.{name,org,context}`, and `capabilities: [...]`. The generic
  `openclaw/soul.template.md` is filled from these fields — you write data,
  not prose.
- **Verbatim**: set `assistant.persona_markdown` to a full markdown body and
  it is used as-is (the `marketing` profile does this to preserve John-117
  byte-for-byte).

Render it to `openclaw/soul.md` (which OpenClaw reads at runtime) with:

```bash
make profile PROFILE=acme
```

The generic template and core never contain any one operator's identity, so
nothing leaks between deployments.

## 3. Connectors — getting data in

A connector is a `source → transform → sink` chain. Each stage names a
**kind** the code registers; the profile supplies the parameters.

### Sources (`source.kind`)

| kind | what it does | auth kinds |
|------|--------------|------------|
| `http_csv` | GET a CSV export URL | `none`, `api_key`, `bearer`, `basic` |
| `login_scrape` | POST a login form, then GET an export behind the session | `login_form` |

Auth is declared under `source.auth`:

```yaml
auth:
  kind: bearer
  token: ${PORTAL_TOKEN}
# api_key: {header: X-API-Key, value: ${PORTAL_API_KEY}}
# basic:   {username: ${PORTAL_USERNAME}, password: ${PORTAL_PASSWORD}}
# login_form:
#   login_url: ${PORTAL_LOGIN_URL}
#   username_field: username
#   password_field: password
#   username: ${PORTAL_USERNAME}
#   password: ${PORTAL_PASSWORD}
#   export_path: ${PORTAL_EXPORT_URL}
```

> JS-rendered portals that need a real browser are **out of scope** for the
> bundled sources — see "Extending" below.

### Transforms (`transform.kind`)

`leads_clean` renames source columns to CRM fields, trims whitespace, drops
rows missing any `required` field, defaults `status`/`created_at`, and
dedupes on `dedup_key` (last row wins):

```yaml
transform:
  kind: leads_clean
  mapping:                 # source CSV column -> CRM field
    "Full Name": name
    "Email Address": email
  required: [email]        # drop rows missing these
  dedup_key: email
```

`passthrough` is the identity transform for sources that already emit
CRM-shaped rows.

### Sinks (`sink.kind`)

`crm` writes records through the CRM adapter:

```yaml
sink:
  kind: crm
  object: leads
  upsert_key: email        # insert new / update existing on this key
```

## 4. CRM — storing and querying

```yaml
crm:
  adapter: sqlite          # sqlite (default) | rest
  sqlite:
    path: ${CRM_DB_PATH}   # defaults to /data/crm.db when unset
  query_templates: [...]   # see below
```

### Swapping the backend

The pipeline and query layers only ever talk to the `CRMAdapter` interface,
so switching backends is a one-line profile change:

- **`sqlite`** (default): a fully local, offline, relational + time-series
  store. Supports `run_template` (read-only SQL) for analytics.
- **`rest`**: a documented seam stub. `upsert`/`list` map to the profile's
  `objects` endpoints over HTTP; `run_template` raises a clear error because
  SaaS CRMs don't run ad-hoc SQL (define a server-side report and map it in).

To plug in a brand-new CRM, implement `CRMAdapter` in
`workflows/crm/<your>_adapter.py` and register it in `crm/adapter.py`'s
`get_adapter`.

### Query templates — the only way the LLM touches data

The LLM **never writes SQL**. It only *selects* a named template and fills
its parameters; the SQL is authored by you and executed read-only. Each
template has a `name`, `description`, typed `params`, and `sql`:

```yaml
query_templates:
  - name: new_leads_count
    description: "Count of leads created within a recent time window."
    params: {window: {type: duration, default: "7d"}}
    sql: "SELECT COUNT(*) AS n FROM leads WHERE created_at >= :since"
```

Param types: `duration` (e.g. `7d`, `24h` → binds `:since`), `enum`
(membership-checked, optional `map`), `string` (binds `:q` and `:like`
`%q%`). The `leads_over_time` template buckets by day/week for time-series.

## 5. Schedules and generators

Standing jobs live under `seeds.schedules`; each names a **generator kind**:

| kind | does |
|------|------|
| `pipeline` | run a connector (`kwargs.connector`) into the CRM, report counts |
| `crm_digest` | run named query templates (`kwargs.queries`) → Telegram digest |

```yaml
schedules:
  - id: lead-sync
    name: "Hourly lead sync"
    kind: pipeline
    cron: "0 * * * *"
    kwargs: {connector: portal-leads, schedule_id: lead-sync}
  - id: crm-digest
    name: "Morning CRM digest"
    kind: crm_digest
    cron: "0 8 * * *"
    kwargs: {queries: [new_leads_count, leads_by_status], schedule_id: crm-digest}
```

Profiles are validated at load time: an unknown source/sink/adapter kind, a
schedule whose `kind` is not a registered generator, or a schedule
referencing an undefined connector all raise a `ProfileError` immediately.

## 6. HTTP API (mounted only when the profile defines `crm`)

| method + path | purpose |
|---------------|---------|
| `POST /crm/query` `{prompt}` | NL question → `{answer, template, params, rows}` |
| `POST /crm/query/run` `{template, params}` | run a template directly |
| `GET  /crm/templates` | list available templates |
| `GET  /crm/leads?status=&limit=` | list leads |
| `POST /crm/sync/{connector_id}` | run a connector now |
| `GET  /healthz/crm` | adapter health + configured connectors |

A marketing-only deployment exposes none of these.

## 7. Try it offline (no real portal)

```bash
make profile PROFILE=lead-crm     # render the persona, set CLAWRANGE_PROFILE
make seed-demo                     # load config/profiles/lead-crm/demo_leads.csv
```

`make seed-demo` runs the demo CSV through the real `leads_clean` transform
and upserts it into the CRM, so `/crm/query` and the `crm_digest` schedule
work end-to-end without any external service. Point `CRM_DB_PATH` at a host
path to keep the demo database outside the container.

## 8. Extending

- **New connector kind**: add a callable in `workflows/connectors/{sources,
  transforms,sinks}.py` and register it in `connectors/__init__.py`. Mirror
  the kind in `tenant_profile.KNOWN_*_KINDS` so profiles fail fast.
- **New CRM backend**: implement `CRMAdapter`, register in `get_adapter`.
- **New generator**: add it to `workflows/generators.py` and the `GENERATORS`
  registry; reference it by `kind` in a schedule.
- **Browser scraping** (JS-rendered portals): out of scope for the bundled
  sources; add a `headless` source kind backed by the chrome tooling.
