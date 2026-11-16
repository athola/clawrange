# Configure ClawRange for Your Business

ClawRange ships as a **pull-down-and-configure template**. Everything that
is specific to one operator (the assistant's persona, what it tracks, where
it pulls data from, and what CRM it writes to) lives in a single declarative
**tenant profile** (`config/profiles/<name>/profile.yaml`). The reusable
machinery (connectors, CRM adapters, query execution) lives in code and is
shared by every profile.

Four profiles ship in the box. All but `marketing` are free of any real
operator's identity; `marketing` is the maintainer's own worked example:

- **`starter`**: the default (`CLAWRANGE_PROFILE` unset → `starter`). A
  generic, identity-free baseline so a fresh clone runs without carrying
  anyone's personal data. Empty seeds. The persona renders from
  `openclaw/soul.template.md`.
- **`marketing`**: a worked content-marketing example with a verbatim
  John-117 persona and example open-source projects/schedules. Edit it to
  point at your own products.
- **`lead-crm`**: a worked business example: an assistant that hourly syncs
  leads from a web portal into a local CRM and answers relational and
  time-series questions about them on a schedule or via a Telegram prompt.
- **`chief-of-staff`**: the end-to-end example of the persona, identity
  and meta-learning loop (an assistant named Max, with an ops agent and a
  `learned.yaml` overlay). See section 10.

To stand up your own assistant, copy one of these, rename it, and edit YAML.
Operator-private profiles named `local-*` are gitignored, so your real
identity never gets committed.

```bash
cp -r config/profiles/lead-crm config/profiles/acme
$EDITOR config/profiles/acme/profile.yaml      # set profile: acme
make profile PROFILE=acme                        # render the persona, set .env
```

`make profile` renders the same files as `make persona` (soul.md, and
identity.md when the profile has an `identity` block, with any approved
learnings) and also sets `CLAWRANGE_PROFILE` in `.env`. Profile
validation is strict, so load a profile once before you deploy it:
`CLAWRANGE_PROFILE=acme uv run python -c 'import sys; sys.path.insert(0, "workflows"); import tenant_profile as t; t.load_profile()'`.

## 1. The profile at a glance

```yaml
profile: acme            # MUST equal the directory name
assistant: {...}         # persona: who the bot is, who it works for
seeds:
  projects: []           # marketing-style tracked entities (optional)
  schedules: [...]       # standing cron jobs (generator + cron + kwargs)
connectors: [...]        # source -> transform -> sink data pipelines
crm: {...}               # backend adapter + named query templates
reddit: {...}            # default and extra subreddits (optional)
awesome_lists: [...]     # curated lists to check for tracked projects
outreach: {...}          # outreach rundown policy (optional)
homelab_deals: {...}     # deal scanner: targets, sources, policy (optional)
```

`${VAR}` tokens in any value are resolved from the environment at load time.
An **unset variable resolves to `""`** and the affected connector or CRM is
treated as *unconfigured*. It logs a warning and is skipped rather than
crashing boot. Put real secrets in `.env` (see `.env.example`), never in the
committed profile.

## 2. Persona (`assistant`)

Two ways to define the persona:

- **Structured** (recommended for new tenants): set `name`, `role`,
  `owner.{name,org,context}`, and `capabilities: [...]`. The generic
  `openclaw/soul.template.md` is filled from these fields. You write data,
  not prose.
- **Timezone**: `assistant.timezone` (an IANA zone such as
  `Europe/Berlin`) drives schedules, quiet hours, the heartbeat watchdog
  and report headers. `SCHEDULER_TZ` in `.env` overrides it; with neither
  set, everything runs in UTC. An unknown zone fails at load. OpenClaw
  cannot read the profile, so its `userTimezone` is rendered from
  `SCHEDULER_TZ` at container start: set it to the same zone.
- **Operator name**: `assistant.owner.name` is how prompts and task labels
  refer to the operator ("the operator" when empty). Keep a real name in
  `.env` and reference it as `${OPERATOR_NAME}`.
- **Search hygiene** (fictional or branded personas):
  `assistant.search_hygiene.strip_headings` lists persona sections dropped
  from web-search prompts, and `blocked_citations` lists regexes for
  citation URLs/titles to filter, so persona lore never floods research.
- **Verbatim**: set `assistant.persona_markdown` to a full markdown body and
  it is used as-is (the `marketing` profile does this to preserve John-117
  byte-for-byte).

Render it to `openclaw/soul.md` (which OpenClaw reads at runtime), plus
`identity.md` when the profile has an identity block, with:

```bash
make profile PROFILE=acme
```

The generic template and core never contain any one operator's identity, so
nothing leaks between deployments.

## 3. Connectors: getting data in

A connector is a `source → transform → sink` chain. Each stage names a
**kind** the code registers. The profile supplies the parameters.

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
> bundled sources (see "Extending" below).

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

## 4. CRM: storing and querying

```yaml
crm:
  adapter: sqlite          # sqlite (default) | rest
  sqlite:
    path: ${CRM_DB_PATH}   # defaults to /data/crm.db when unset
  query_templates: [...]   # see below
```

Inside the workflows container the CRM is always `/data/crm.db` (host
`data/brain/crm.db`): `docker-compose.yml` sets `CRM_DB_PATH` there.
Setting `CRM_DB_PATH` yourself only affects host-side tools such as
`make seed-demo`.

### Swapping the backend

The pipeline and query layers only ever talk to the `CRMAdapter` interface,
so switching backends is a one-line profile change:

- **`sqlite`** (default): a fully local, offline, relational and time-series
  store. Supports `run_template` (read-only SQL) for analytics.
- **`rest`**: a documented seam stub. `upsert`/`list` map to the profile's
  `objects` endpoints over HTTP. `run_template` raises a clear error because
  SaaS CRMs don't run ad-hoc SQL (define a server-side report and map it in).
  `upsert` needs a `list` endpoint for the object: it looks each record up
  with `?<upsert_key>=<value>` (matched again client-side, case-insensitive)
  and creates only the new ones. Add an `update` endpoint such as
  `"PATCH /leads/{id}"` to update existing records; without it they are
  skipped, so a re-sync never duplicates remote leads.

To plug in a brand-new CRM, implement `CRMAdapter` in
`workflows/crm/<your>_adapter.py` and register it in `crm/adapter.py`'s
`get_adapter`.

### Query templates: the only way the LLM touches data

The LLM **never writes SQL**. It only *selects* a named template and fills
its parameters. The SQL is authored by you and executed read-only: each
template must be a single `SELECT` or `WITH` statement with no `;`, and
the SQLite adapter runs it on a read-only connection. Each template has a
`name`, `description`, typed `params`, and `sql`:

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

Standing jobs live under `seeds.schedules`. Each names a **generator kind**:

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
    kwargs: {connector: portal-leads}
  - id: crm-digest
    name: "Morning CRM digest"
    kind: crm_digest
    cron: "0 8 * * *"
    kwargs: {queries: [new_leads_count, leads_by_status]}
```

The scheduler passes each generator its own row id as `schedule_id`, so
kwargs never need to repeat it (a value given there is overridden).

Profiles are validated at load time: an unknown source/sink/adapter kind, a
schedule whose `kind` is not a registered generator, or a schedule
referencing an undefined connector all raise a `ProfileError` immediately.

## 6. HTTP API (mounted only when the profile defines `crm`)

| method and path | purpose |
|---------------|---------|
| `POST /crm/query` `{prompt}` | NL question → `{answer, template, params, rows}` |
| `POST /crm/query/run` `{template, params}` | run a template directly |
| `GET  /crm/templates` | list available templates |
| `GET  /crm/leads?status=&limit=` | list leads |
| `POST /crm/sync/{connector_id}` | run a connector now |
| `GET  /healthz/crm` | adapter health and configured connectors |

All but `GET /healthz/crm` need `Authorization: Bearer $PROXY_AUTH_TOKEN`.
`/healthz/crm` is public, so it reports only `status`, `adapter` and
`leads` from the adapter (never the DB path, REST base URL or raw error
text), plus connector and template names. It returns 503 when the adapter
reports an error. A marketing-only deployment exposes none of these.

## 7. Try it offline (no real portal)

```bash
make profile PROFILE=lead-crm     # render the persona, set CLAWRANGE_PROFILE
make seed-demo                     # load config/profiles/lead-crm/demo_leads.csv
```

`make seed-demo` runs the demo CSV through the real `leads_clean` transform
and upserts it into the CRM, so `/crm/query` and the `crm_digest` schedule
work end-to-end without any external service. `make seed-demo` runs on the
host and writes to `CRM_DB_PATH`: a value on the command line wins, then
the one in `.env`, then the default `data/crm/crm.db`. The container reads
`data/brain/crm.db`, so run `CRM_DB_PATH=data/brain/crm.db make seed-demo`
(or set it in `.env`) to seed the database the running stack uses.

## 8. Outreach and deal policy

### `reddit` and `awesome_lists`

```yaml
reddit:
  default_subreddits: [selfhosted]        # scans that name none
  digest_extra_subreddits: [homelab]      # added to every digest/pulse
awesome_lists: [someone/awesome-thing]    # owner/repo, checked weekly
```

With no `default_subreddits`, scans fall back to the union of the tracked
projects' own `subreddits`; with neither, nothing is scanned.

Both rundowns keep only generic behaviour in code. Everything about one
tenant's audience, domain and marketplaces is profile data, validated at
load so a typo fails at boot rather than at the first cron fire.

### `outreach` (daily outreach rundown)

```yaml
outreach:
  audience: a solo maintainer promoting these open-source projects
  web_venues: forum threads, Lobsters, dev.to   # where web search looks
  venue_actions:          # override what the operator may do per channel
    hn: read the thread; comment only on a direct question
  banned_actions: [newsletter, discount code]   # extra phrases that drop
                                                # an action item
```

Channels are `reddit`, `hn`, `github` and `web`. The built-in defaults
already forbid cross-posting, guest-post pitches, cold outreach and triage
verbs aimed at issues on repos the operator does not own. Each project is
described to the model only by the first sentence of its `posture`, so
write that sentence as what the project is.

### `homelab_deals` (deal scanner)

```yaml
homelab_deals:
  title: Homelab deal rundown        # report heading (default "Deal rundown")
  goal: one line under the heading
  current_setup: ${MY_SETUP}         # keep personal detail in .env
  advisor:
    audience: who the buy read advises, and for what
    rules: [domain rules the model must apply]
  too_good_share: 0.5    # under this share of max_price_great: flagged, and
                         # eBay is not searched below it
  max_per_target: 2      # listings per target per section; rest -> "+N more"
  red_flags:             # flag label -> phrases (word-start, any case)
    "barebone: CPU, RAM or drives not in the price": [barebone, no cpu]
  part_categories: [gpu, mainboard]   # judged as parts vs. whole systems
  memory_check_categories: [gpu]      # title may not state less memory
  exclude: [waterblock, for parts]    # appended to every target
  sources:
    ebay:
      categories: {gpu: 27386}        # target category -> eBay category id
      marketplace: EBAY_US            # defaults: EBAY_US, USD, US
      currency: USD
      delivery_country: US
    slickdeals: {}
    servethehome: {}                  # STH forum "Great Deals" feed
    reddit: {subreddits: [homelabsales]}
  stack_gaps: {vram: what is missing}
  targets:
  - {name: RTX 3090 24GB, query: rtx 3090, category: gpu,
     max_price_great: 750, min_vram_gb: 24, fills: [vram]}
```

Find eBay category ids with the Taxonomy API
(`get_category_suggestions`). Leave a category unmapped when real listings
span several eBay categories. The `marketing` profile is a full worked
example.

Only the sources listed under `sources` are polled. Each one gets its own
status line in the rundown, so an outage shows as a failed source rather
than a quiet day:

- **eBay** without `EBAY_CLIENT_ID`/`EBAY_CLIENT_SECRET`, or with a keyset
  eBay rejects, reports as not configured and names the fix. One failed
  target search is skipped and counted (`1/3 targets failed`); the source
  fails only when every target does.
- **Slickdeals** runs one search per target and fails only when all of
  them fail. **ServeTheHome** is a single feed and fails when that feed does.
- **Reddit** reads with OAuth when the `REDDIT_*` credentials are set and
  falls back to public feeds otherwise. The other subreddits still read
  when one fails.

## 9. Extending

- **New connector kind**: add a callable in `workflows/connectors/{sources,
  transforms,sinks}.py` and register it in `connectors/__init__.py`. Mirror
  the kind in `tenant_profile.KNOWN_*_KINDS` so profiles fail fast.
- **New CRM backend**: implement `CRMAdapter`, register in `get_adapter`.
- **New generator**: add it to `workflows/generators.py` and the `GENERATORS`
  registry. Reference it by `kind` in a schedule.
- **Browser scraping** (JS-rendered portals): out of scope for the bundled
  sources. Add a `headless` source kind backed by the chrome tooling.

## 10. Persona & meta-learning

A profile shapes not just *what* the assistant does but *who* it is. The
`assistant.identity` block renders the agent's `IDENTITY.md`, and an
approval-gated learning loop lets the persona improve over time without ever
leaving the operator out of the decision. The `chief-of-staff` profile (Max)
is the worked example.

### Identity block

```yaml
assistant:
  name: "Max"
  identity:
    name: "Max"            # defaults to assistant.name when omitted
    creature: "Chief of Staff: strategic AI operator"
    vibe: "Sharp, direct, calm under pressure."
    emoji: "🎯"
    avatar: ""             # workspace-relative path, URL, or data URI
```

`identity` is optional. Profiles without it (e.g. `starter`) render no
`IDENTITY.md`, exactly as before. Unknown identity keys fail validation at
load, so a typo never reaches runtime.

### The meta-learning loop

The assistant proposes persona enhancements. You approve them. Approved
enhancements are appended to the rendered persona under a `## Learned`
region and persisted. Nothing is applied without approval, the same posture
as outward-facing actions.

- **Give feedback:** `!persona lead with the recommendation` (or the
  `!learn` alias) queues the feedback as a `[DRAFT]` proposal.
- **Reflect on demand:** `!persona reflect` reviews recent activity (through
  the LLM proxy) and queues any suggestions. A `persona_reflect` schedule can
  run the same pass on a cadence (see the `chief-of-staff` profile's
  `seeds.schedules`).
- **Approve / reject:** `POST /persona/proposals/<id>/approve` (or
  `/reject`). Approval re-renders the persona. Rejection drops the proposal.
  Proposals and approvals are scoped to the active profile. One tenant
  cannot approve another's drafts.
- **Inspect:** `GET /persona/proposals?status=pending`,
  `GET /persona/identity`, `GET /persona/soul`, and `GET /healthz/persona`.
  The health route is public and reports the profile, the pending count,
  the target names and `writable`: per target group, whether a render
  could replace every file it writes right now (no paths are shown).

### Persisting an evolved persona for git

Approved learnings live in the brain (the live store). To capture them as a
git-trackable overlay that anyone who pulls the repo inherits:

```bash
make persona-export PROFILE=chief-of-staff
```

This writes `config/profiles/chief-of-staff/learned.yaml`. On boot,
`seed_from_profile` reads that overlay back into the brain (a bad or partial
overlay is skipped with a warning, never crashing boot), so the file and the
live store stay in sync. Edit either side and re-export/re-seed.

`make persona` and `make profile` render on the host from both sides:
approvals in `data/brain/brain.db` merged with `learned.yaml`, so
unexported approvals still render. If the brain DB is missing or
unreadable, they render `learned.yaml` alone and print a warning, leaving
the DB as it found it. The command exits non-zero when any target file
could not be written.

> Outcome-signal proposals (the assistant noticing recurring corrections on
> its own) are off by default. Set `PERSONA_SIGNAL_LEARNING=1` to enable them.

### Why three layers

The persona system reconciles two opposing requirements: "anyone who pulls
the repo can configure the assistant" wants git-tracked files, while "the
assistant improves itself over time" happens in runtime state that cannot
be hand-edited into git. Three layers bridge them:

1. **Config (static, git-tracked)** — `profile.yaml` persona/identity
   blocks are the authored baseline everyone inherits.
2. **Learning store (dynamic)** — the brain's `persona_learnings` table is
   the live record of proposals and approvals, scoped per profile.
3. **Learned overlay (portable)** — `learned.yaml` is the git-exportable
   projection of approved learnings, seeded back into the brain on boot.

Renders are non-destructive: approved learnings are appended under a
`## Learned` region rather than rewriting the authored blocks, and every
render goes through the same atomic `render_all`, so the authored baseline
always remains the source a fresh clone starts from.
