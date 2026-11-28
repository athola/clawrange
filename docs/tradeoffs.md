# Tradeoffs Journal

Decisions, the alternatives weighed, and what was given up. Newest first.

## Active Index

| ID | Phase | Status | Decision |
|----|-------|--------|----------|
| TR-020 | specify | accepted | Homelab watch is pull-only: polls HA, Kuma and the scanner census; never logs in to the router |
| TR-019 | specify | accepted | Homelab data stays out of brain pages and LLM prompts (own table, internal census network, `[homelab]` history strip) |
| TR-018 | specify | accepted | HA actions: empty allowlist, hard-denied domains, out-of-band single-use code, lockout |
| TR-017 | specify | accepted | Homelab alerts: baseline first, debounce, flood collapse; batteries and updates are report-only |
| TR-016 | execute | accepted | `create_app()` factory; chat commands and heartbeat take the brain, never `from app import` |
| TR-015 | execute | accepted | Proxy token: env SecretRef, workspace-only file tools, one-time state migration, startup secrets audit |
| TR-014 | specify | accepted | Income loop reuses the task queue and tome bridge; every trade is a `[DRAFT]`, benchmarked against holding |
| TR-013 | specify | accepted | Persona config and meta-learning: git profile, brain learning store, approval-gated `## Learned` overlay |
| TR-012 | execute | accepted | OpenClaw 2026.11.6: heartbeat is an automation job that `sync-heartbeat.sh` ensures; render refuses retired keys |
| TR-011 | specify | accepted | Marketing output is drafts for approval; nothing auto-posts |
| TR-010 | specify | accepted | One server-side research orchestrator called as a tool, not lead/subagent agents |
| TR-009 | specify | accepted | APScheduler 3.x inside the workflows process, in-memory jobstore rebuilt from the brain |
| TR-008 | specify | accepted | Brain is SQLite (FTS5 and sqlite-vec) in the workflows container, not a separate vector DB |
| TR-007 | execute | accepted | Workflows keeps running as root; persona renders copy the target's mode and owner |
| TR-006 | execute | accepted | `/healthz` stays 200 while degraded; the body, boot alert and logs carry the state |
| TR-005 | execute | proposed | Keyword fallback ORs terms; hybrid with vectors unchanged until the eval shows a win |
| TR-004 | execute | superseded | Keep per-file dev mounts; directory mount blocked by the nested soul.md bind (resolved: soul.md moved to `/opt`) |
| TR-003 | plan | accepted | Regression-lock marketing seeds (RED equivalence test) before deleting constants |
| TR-002 | specify | accepted | Minimal CRMAdapter interface and templated-SQL only (no LLM raw SQL) |
| TR-001 | brainstorm | accepted | Multi-tenant via hybrid declarative profile and connector registry |

---

<!-- ENTRY TEMPLATE
## TR-NNN: <short title>
- **Phase**: brainstorm | specify | plan | execute
- **Status**: proposed | accepted | superseded
- **Decision (Y-statement)**: In the context of <use case>, facing <concern>,
  we decided <option> to achieve <quality>, accepting <downside>.
- **Options weighed**: ...
- **Negative consequences**: ...
-->

TR-008 to TR-011 were recovered from the planning briefs and specs that
lived in `docs/archive/` (removed; see `git log -- docs/archive`). TR-013
and TR-014 were recovered the same way from the design specs and plans in
`docs/superpowers/` (removed; untracked since `5e33c1c`). Each was
checked against the code when it was written here, and the
**Changed since** lines record where the shipped design moved away from
the plan.

## TR-020: Homelab watch is pull-only and reads the scanner's census

- **Phase**: specify
- **Status**: accepted
- **Decision (Y-statement)**: In the context of watching Home Assistant,
  Uptime Kuma and the router from clawrange on the secure side, facing a
  zone firewall and tailnet policy that let the IoT zone initiate nothing
  toward the secure zone, we decided that clawrange **polls** (HA REST
  `/api/states`, Kuma's public status-page JSON, the monitoring stack's
  `scanjson` census) and **never logs in to the router**, to achieve
  alerts without opening any IoT-to-secure path and without a second
  router collector, accepting up to 5 minutes of alert latency and the
  census's 6-hour cadence for device and port findings.
- **Options weighed**:
  - HA webhooks, `rest_command` or an MQTT bridge to clawrange. Rejected:
    each needs IoT-to-secure traffic the firewall forbids by design.
  - An outbound HA WebSocket subscription (laptop-initiated, so allowed).
    Deferred: push-like latency, but a long-lived connection inside the
    single uvicorn worker for a household that tolerates minutes.
  - RouterOS REST over `www-ssl` with a read-only group. Rejected: a fourth
    management port changes the documented 22/53/8291 baseline, needs a
    certificate, and one report says a read-only group can still list
    users (forum.mikrotik.com/t/rest-api-policy/169084, single source).
- **Negative consequences**: a state change that flips and recovers inside
  one poll is never seen (the DNP3 "integrity poll" analogy: snapshots
  miss transients; HA's logbook would close that gap). The census network
  `clawrange-homelab` is external to the monitoring stack, so clawrange
  must be up before that stack starts.
- **Evidence**: `workflows/homelab/sources.py`, `scanner/scan-loop.sh` in
  the monitoring repo (router `/system resource print` and every MikroTik
  KEV entry are new scanner legs), `docs/homelab-guide.md`.

## TR-019: Homelab data stays out of brain pages and LLM prompts

- **Phase**: specify
- **Status**: accepted
- **Decision (Y-statement)**: In the context of an assistant whose brain
  pages are embedded through OpenRouter and whose chat history is replayed
  to free LLM tiers, facing DHCP hostnames and HA entity names that are
  both private topology and attacker-chosen strings, we decided that
  homelab state lives in its own `homelab_state` table, the census is
  served on an internal network only the workflows container joins, and
  every `!home`/`!net` reply starts with `[homelab]`, a `_POISON_MARKERS`
  entry the proxy strips from forwarded history, to achieve deterministic
  answers that no model ever reads, accepting that the assistant cannot
  answer free-form questions about the network ("what's the TV's IP?")
  except through the commands.
- **Options weighed**: an inventory brain page so recall could answer
  questions. Rejected: the embeddings backfill would ship the topology to
  OpenRouter. Putting `scanjson` on `msp-network`. Rejected after review:
  OpenClaw sits there and its agents can `web_fetch`.
- **Negative consequences**: the marker filter only covers string content;
  if OpenClaw ever reformats stored assistant turns, the strip misses them.
- **Evidence**: `homelab_get`/`homelab_put` in `workflows/brain_db.py`,
  the `homelab-census` network in `docker-compose.yml`,
  `HOMELAB_REPLY_MARKER` and its proxy test.

## TR-018: HA actions: empty allowlist, hard-deny, out-of-band code

- **Phase**: specify
- **Status**: accepted
- **Decision (Y-statement)**: In the context of running HA services from
  Telegram, facing demonstrated indirect prompt injection into smart-home
  agents (a poisoned calendar title drove Google Home, arXiv 2508.12175)
  and an HA token that cannot be scoped per entity, we decided on a
  deterministic gateway the LLM never reaches: `!home do <service>
  <entity_id>` must be the whole message, the profile allowlist ships
  empty, `lock`, `cover`, `alarm_control_panel`, `valve`, `siren`,
  `camera` and HA's own system domains are denied in code, a 6-digit code
  bound to the exact `(service, entity_id)` goes to Telegram directly
  (never in the proxy reply), lives 120 s and works once, and 3 bad codes
  in 10 minutes lock confirmations for 10 minutes, to achieve actions that
  injected text cannot trigger, accepting two messages per action and no
  garage or lock control from chat at all.
- **Options weighed**: HA's `mcp_server` or the Assist API with exposed
  entities. Rejected for actuation: the MCP "Home Assistant" toolset can
  do anything and needs an admin; exposure scoping is coarse and has no
  confirmation step. Name-matched targets. Rejected: CaMeL-style
  separation (arXiv 2503.18813, 2506.08837) says plan from trusted input
  only, so targets are exact entity ids.
- **Borrowed rules**: PSD2 dynamic linking (approval bound to the literal
  action), rail interlocking (deny on any failure), the nuclear weak-link
  idea (repeated failure disables actuation instead of allowing guesses).
- **Negative consequences**: codes and lockout state are in memory, so a
  restart clears them. A non-admin `clawrange` HA user is recommended;
  HA's read-only group may not be selectable in its UI.
- **Evidence**: `workflows/homelab/actions.py`, `service.request_action`
  and `confirm_action`, `workflows/tests/test_homelab_actions.py`.

## TR-017: Homelab alerts: baseline first, debounce, flood collapse

- **Phase**: specify
- **Status**: accepted
- **Decision (Y-statement)**: In the context of a Telegram channel the
  operator reads on a phone, facing the top alert-fatigue complaints
  (per-device duplicates, `unavailable` floods after HA updates) and
  dozens of randomized phone MACs in the router's ARP table, we decided
  that the first good observation of each source learns a baseline and
  alerts nothing, a failed source is an outage (never "everything
  vanished"), randomized MACs are counted not alerted, HA entities alert
  only after 30 minutes `unavailable` (`unknown` is report-only), more
  than 10 at once collapse into one line, and batteries, pending updates
  and KEV history go to the daily report, to achieve near-zero routine
  alerts, accepting that a real outage of fewer than 30 minutes never
  pages.
- **Borrowed rules**: ISA-18.2 / IEC 62682 / EEMUA 191 (flood above 10
  alarms in 10 minutes; routine events are not alarms; zero chattering;
  every alarm has a response), via the ABB/IChemE KPI deck. HA's own
  battery tutorial batches low batteries daily.
- **KEV wording**: CISA's feed has no affected-version field, so a new
  MikroTik entry alerts with the running RouterOS version and "check
  MikroTik's advisory", never "vulnerable".
- **Deviation kept**: a stale census alerts on the very first pass;
  freshness is not a baseline.
- **Negative consequences**: a dead poller is silent. `/healthz/homelab`
  goes `degraded` after 15 minutes without a run and the admin Homepage
  shows it, but nothing outside the laptop watches the watcher until an
  always-on secure-zone host exists.
- **Evidence**: `workflows/homelab/findings.py` and
  `workflows/tests/test_homelab_findings.py`; research notes for this
  decision were gathered with tome (papers, code, discourse, TRIZ).

## TR-016: App factory and injected state instead of `from app import`

- **Phase**: execute
- **Status**: accepted
- **Decision (Y-statement)**: In the context of tests that had to set
  `BRAIN_DB_PATH` before anything imported `app` (importing it opened the
  brain and built the whole app), and of `heartbeat.py`, `chat_commands.py`
  and `generators.py` reaching back with about 20 function-local
  `from app import brain_db` / `app.state` lookups, we decided on a
  **Flask-style `create_app(brain_db=None)` factory plus dependency
  injection**:
  - `create_app` configures logging, opens the brain (`open_brain()`) when
    none is given, puts it on `app.state.brain_db` and mounts the routers.
    Routes get it through a `Depends(_brain)`; the scheduler through
    `Depends(_scheduler)`.
  - `llm_proxy.chat_completions` reads the brain, profile, scheduler and
    `profile_provider` off `request.app.state` and passes them down. The
    chat command and heartbeat functions take them as required arguments.
  - `!task` writes through `brain_db` with the `TaskCreate` model (now in
    `tasks_api.py`) instead of calling the route function, so chat and
    `POST /task` keep one validation rule and both record `source="user"`.
  - uvicorn serves the factory (`uvicorn --factory app:create_app` in
    `entrypoint.sh`), so `app.py` has no module-level app or brain and
    importing it opens nothing.
  - Tests get a fresh `brain_db`, `app` and `client` per test from
    `conftest.py` fixtures. The brain is a file copy of one migrated
    session template (about 1ms; migrating a new file is about 60ms), and
    the app (about 10ms to build) exists only for tests that name `app` or
    `client`. No conftest autouse fixture asks for them, so other tests pay
    nothing. That replaced a shared app whose tables were emptied after
    every test.
  `test_dependency_injection.py` fails if any non-test module imports
  `app`; `test_app_factory.py` proves two apps never share a brain;
  `test_fresh_app.py` proves each test gets its own and that no test
  module brings back a shared app or a module-level `TestClient`. The
  suite passes in reverse file order too; that run found scheduler
  startup errors leaking between tests, now reset per test in
  `conftest.py` like the other module globals.
  - With nothing shared, the suite runs under pytest-xdist: `make
    test-unit` (`PYTEST_WORKERS`, default `auto`), `make coverage`, the
    pre-push hook and CI pass `-n`. On a 16-core host the full run went
    from about 86s to 27-37s; `-n auto` is not in `addopts`, so a single
    test run from the shell starts no workers.
- **Options weighed**:
  - A: Move only the persona helpers out of `app.py`: two files, but the
    import-time DB and the back-imports stay. Rejected.
  - B: `runtime.py` holder that `create_app` sets and callers read: small,
    but still a global, only renamed. Rejected for DI.
  - C (chosen): factory and DI, served with `uvicorn --factory`.
  - Routes as `create_*_router(brain_db)` closures, like the brain and
    persona routers, were considered; `Depends` was chosen so route
    functions stay importable and the per-request root is the same
    `request.app.state` the chat path uses.
  - Test freshness: one shared app with a reset fixture (rejected: order
    coupling, and the reset had to track every table); rebinding each test
    module's `brain_db` global per test (rejected: implicit); an in-memory
    brain per test (2ms, but not the WAL file production runs); a template
    copy per test with lazy app and client fixtures (chosen). Moved with a
    one-off libcst codemod: tests and fixtures took the fixtures as
    trailing params (after any `@patch` mocks), helpers keyword-only ones.
- **Negative consequences**:
  - About 90 test call sites had to pass the brain explicitly.
  - Setup costs about 5s more over the suite (24.5s against 19.3s), almost
    all of it `create_app` for the ~350 tests that use an app.
    `conftest.py` still defaults `BRAIN_DB_PATH` to a temp path, as a
    guard so a stray `create_app()` never opens `/data`.
  - A running dev container on the old `uvicorn app:app` command fails to
    reload once `app.py` loses its module-level app; the entrypoint
    supervisor restarts it on the new command.
- **Reversibility**: HIGH. Internal refactor, no API or schema change.

## TR-015: Keep the proxy token out of what agents can read

- **Phase**: execute
- **Status**: accepted
- **Decision (Y-statement)**: In the context of `openclaw doctor` and
  `openclaw secrets audit` reporting `PROXY_AUTH_TOKEN` in plaintext in
  `openclaw.json` and in the gateway's auth-profile store, while `max-ops`
  runs unsandboxed with a `read` tool that could open any path the `node`
  user can, we decided on **four layers**:
  - **Reference, not value:** the template's proxy `apiKey` is an env
    SecretRef, and `render-config.js` never writes the token.
  - **Confined file tools:** `tools.fs.workspaceOnly: true` keeps every
    agent's `read` in its workspace, away from config, rollback copies and
    `/proc/*/environ`.
  - **One-time migration:** `openclaw/secrets-plan.json` scrubs the
    auth-profile copy, and the heartbeat job is recreated in the same step.
  - **Startup audit:** `sync-heartbeat.sh` runs `secrets audit --check` and
    fails the healthcheck on any finding.

  The aim is that no file or process an agent can reach yields the token,
  accepting a one-time manual step on existing installs.
- **Options weighed**:
  - **SecretRef alone.** Rejected: an unsandboxed `read` can still open
    `/proc/self/environ`, where the env ref resolves.
  - **Applying the plan on every start.** Rejected after it took prod's
    heartbeat down: 2026.11.6 pins an automation job to its inference
    account and stops the job when that profile is deleted ("was removed or
    signed out"). Recreating the profile under the same id does not help
    ("was replaced").
  - **Keeping the plaintext profile** behind `workspaceOnly`. Rejected: a
    single layer, and the audit would need an exception.
- **Negative consequences**:
  - The migration deletes the heartbeat job's run history.
  - The gateway process env still holds the token. That is by design for
    an env ref, and `workspaceOnly` is what keeps agents from it.
  - Rollback copies (`openclaw.json.bak.*`) and state tarballs made before
    the change hold the old value, so the change ended with a token
    rotation.
- **Evidence**: `workflows/tests/test_openclaw_template.py` (ref, fs
  confinement, plan), `tests/test_openclaw_scripts.py` (no token on disk,
  audit guard, the start command never applying the plan). On prod, a
  `max-ops` turn asked to read `openclaw.json` and `/proc/self/environ` got
  "Path escapes sandbox root" for both. The audit was clean (`plaintext=0,
  shadowed=0`), the recreated job and a `max` chat turn worked, and after
  rotation the old token got 401.

## TR-014: Income loop reuses the existing pipeline, approval-gated

- **Phase**: specify
- **Status**: accepted
- **Decision (Y-statement)**: In the context of letting the operator task
  ClawRange from Telegram with describing, researching and trying
  income techniques (first instance: a $50 crypto testbed), facing the
  risk of an assistant that chases or claims "reliable weekly returns" and
  acts on money unsupervised, we decided to **build no new pipeline**:
  description goes through the proxy chat with citation discipline,
  research through `research:tome:` tasks and the tome bridge, and the
  weekly reevaluation is an `income_review` generator that queues
  `[DRAFT] income: weekly strategy review` for the operator, to achieve a
  loop that cannot place a trade or change a strategy on its own,
  accepting that every step waits on the operator.
- **Constraints kept**: reviews benchmark against holding USDC and holding
  BTC, and every proposal cites sources. Nothing depends on live exchange
  keys. Out of scope: fixed-return strategies, leverage and derivatives,
  and any trade without a human approval step.
- **Options weighed**: a dedicated income service with its own scheduler
  and store. Rejected: the task queue, `[DRAFT]` convention, brain notes
  (`income:` prefix) and `/sched` already cover approval, memory and
  cadence.
- **Negative consequences**: the review is a draft for a human, so a week
  the operator does not claim it is a week with no review. The heartbeat
  leaves `[DRAFT] income:` tasks alone (`heartbeat._FOREIGN_TASK`); the
  stale-queue nudge lists them as waiting on the operator.
- **Changed since the spec**:
  - The code landed on `variable-config`, not a separate `income-loop`
    branch.
  - The strategy doc is `config/profiles/marketing/income-strategy.md`,
    not `docs/income-strategy.md`.
  - The review checklist is the profile's `income_review` block, with a
    generic default in `generators.py`.
- **Deferred**: an `!income` command family (status, review, propose) and
  a ccxt exchange adapter: balance reads first, a paper engine, then
  approval-gated orders with trade-only keys. Both wait on a funded
  account and about 4 weeks of review history.
- **Evidence**: `income_review_generator` in `workflows/generators.py`
  (registered in `GENERATORS`) and `workflows/tests/test_income_review.py`.

## TR-013: Persona config and approval-gated meta-learning

- **Phase**: specify
- **Status**: accepted
- **Decision (Y-statement)**: In the context of letting anyone who pulls
  the repo shape the assistant's persona and identity per use case, while
  the assistant also improves its own persona over time, facing the
  conflict between git-tracked config and agent-evolved runtime state, we
  decided on **three layers**: the profile's `assistant` and `identity`
  blocks as the git-tracked baseline, the brain's `persona_learnings`
  table as the live store with `config/profiles/<name>/learned.yaml` as
  its exportable projection, and a render that regenerates the base and
  accumulates approved learnings in a `## Learned` region. The aim is a
  persona that grows but stays reproducible, accepting a proposal queue
  the operator must work through.
- **Rules kept**:
  - No persona change without operator approval. Every proposal is a
    `[DRAFT]` task, the same posture as outward-facing actions.
  - Proposal sources all call `propose()`: `!persona` / `!learn`
    feedback, `POST /persona/reflect`, the `persona_reflect` generator,
    and outcome signals behind `PERSONA_SIGNAL_LEARNING=1`, off by
    default for false-signal risk.
  - Render is atomic (`mkstemp` in the target dir, fsync, `os.replace`).
    An unwritable target still records the approval; the response's
    `render` field gives each target's write result, and re-approving
    retries the render.
  - `export_overlay` and `seed_overlay` are inverse operations; a bad
    overlay is skipped at boot, never fatal.
  - No web UI, no new always-on service.
- **Options weighed**: persona only in git (does not evolve), only in
  runtime state (not reproducible), and letting the agent edit its own
  `SOUL.md` (ungated). The design takes the brain from the second and the
  growing `## Learned` region from the third, behind the approval gate.
- **Negative consequences**: approved learnings exist only in the brain
  until someone runs `make persona-export`, so a lost `brain.db` loses
  unexported ones. Workflows needs write access to the OpenClaw state dir
  to re-render on approval.
- **Changed since the spec**:
  - The runtime render mount is the whole state dir, not a narrow
    per-file mount: `os.replace` swaps the inode, and a single-file bind
    keeps pointing at the old one (`docker-compose.yml`).
  - `openclaw/identity.md` is a render artifact only and is not mounted;
    the agent reads `IDENTITY.md` from its workspace.
  - Reflection prompts go through `llm_proxy._llm_call`, per the proxy
    convention.
- **Evidence**: `workflows/persona.py`, `persona_learning.py` and
  `persona_api.py`, their tests, the `chief-of-staff` profile, and section
  10 of `docs/multi-tenant-guide.md` (the operator guide).

## TR-012: OpenClaw 2026.11.6 heartbeat as an ensured automation job

- **Phase**: execute
- **Status**: accepted
- **Decision (Y-statement)**: In the context of upgrading OpenClaw from
  2026.11.3 to 2026.11.6, facing the 2026.11.5 retirement of heartbeat
  config (`agents.*.heartbeat` is now rejected and Gateway startup no
  longer creates a heartbeat), we decided to **treat the heartbeat as an
  ordinary `agentTurn` automation job**. `openclaw doctor --fix` migrates
  an existing one. `sync-heartbeat.sh` finds it by its message prefix on
  every start, creates it on a fresh state dir, caps its tools to the
  `max-ops` allow list from `openclaw.json`, and loads `HEARTBEAT.md`
  into its scratch. The aim is to keep the repo as the source for the
  checklist and tool policy, accepting that cadence, active hours and
  delivery are now stored on the job in the state DB rather than in git.
- **Options weighed**: keeping the schedule in `openclaw.json` (no longer
  possible). Re-creating the job from the repo on every start: that would
  undo an operator's `automations edit` or `disable`, so an existing job is
  kept, including a disabled one. Having `render-config.js` drop the
  retired keys: that would hide a skipped doctor run, so it stops with an
  error naming the doctor command instead.
- **Negative consequences**:
  - Automation jobs prefix the message with `[cron:<id> <name>]`, so
    `llm_proxy` strips that tag before matching the heartbeat marker.
  - The quiet reply is `NO_REPLY`: an empty reply could surface as a
    missing-answer notice.
  - 2026.11.5+ installs provider plugins it infers from env (Perplexity
    from `OPENROUTER_API_KEY`) with npm, so the read-only container needs
    `npm_config_cache=/tmp/.npm`.
  - A world-writable state dir stops the gateway at start ("No safe
    OpenClaw temp root").
  - The retired `agents.defaults.heartbeat.every: "0m"` was what stopped
    2026.8+ giving every agent its own heartbeat DM, and no config can say
    that now. `sync-heartbeat.sh` guards the outcome instead: an enabled
    `heartbeat-*` job that is not ClawRange's marks the container
    unhealthy, naming it. It reports rather than disables, and it ignores
    other automations, because `max-ops` has the `cron` tool and the
    operator's own reminders must not trip it.
- **Evidence**: `tests/test_openclaw_scripts.py` (`TestSyncHeartbeat`,
  `test_retired_heartbeat_keys_stop_the_render`) and `TestGetLastUserMessage`
  in `workflows/tests/test_llm_proxy.py`. On the local install, doctor
  migrated the job and a forced run was intercepted by the proxy, finishing
  `ok (suppressed)`.

## TR-011: Marketing output is drafts for approval; nothing auto-posts

- **Phase**: specify
- **Status**: accepted
- **Decision (Y-statement)**: In the context of an assistant that finds
  threads, issues and content ideas for the operator's projects, facing the
  reputational cost of a bot posting off-tone or self-promotional replies
  in communities that ban them, we decided that **every outward-facing
  artifact is queued as a `[DRAFT]` task for the operator to approve and
  post by hand**, with product mentions only when directly relevant, to
  achieve useful-first participation, accepting a human in the loop on
  every post.
- **Options weighed**: automated replies and follow/engagement loops.
  Rejected: they trade account standing for volume.
- **Negative consequences**: throughput is bounded by the operator's
  attention. Drafts that are not reviewed go stale.
- **Evidence**: `content_idea` and `comment_draft` in
  `workflows/generators.py` only call `create_task`. No code path posts to
  Reddit, HN, GitHub or X.

## TR-010: One server-side research orchestrator, called as a tool

- **Phase**: specify
- **Status**: accepted
- **Decision (Y-statement)**: In the context of OpenClaw running a single
  agent, facing questions that need several sources compared and cited,
  we decided to **give that agent one rich tool (`POST /research`) that
  fans out across channels server-side, ranks with tome's scoring and
  flags confidence**, to achieve cited multi-source answers on one agent,
  accepting no parallel deep-dive agents inside the stack.
- **Options weighed**:
  - Lead/subagent research topology (Anthropic's multi-agent pattern):
    better on broad questions in their eval, but at roughly 15x the tokens
    of a single agent. Rejected for a budget-bounded personal stack. Heavy
    topics go to the operator's local Claude Code session through
    `scripts/tome_bridge.py` instead.
  - Let the model summarise raw search results. Rejected: models invent
    citations, so every claim must carry a fetched URL or DOI and
    single-source findings are flagged "needs verification".
- **Ranking**: base score plus authority bonuses (stars, HN and Reddit
  score, citations), +0.05 for recency within two years, capped at 1.0;
  each corroborating channel adds 0.05 up to 0.15
  (`_TRIANGULATION_CAP`, `workflows/research.py`). The triangulation bonus
  sets the confidence flag (`flag_confidence`).
- **Changed since**: the plan had three channels (Reddit, GitHub, web).
  The code adds `academic` (arXiv, Semantic Scholar) and `triz`, and
  defaults to `discourse`, `code` and `discourse_web`.
- **Negative consequences**: one slow channel delays the whole response;
  partial failures land in `errors` rather than failing the call.

## TR-009: APScheduler 3.x inside the workflows process

- **Phase**: specify
- **Status**: accepted
- **Decision (Y-statement)**: In the context of cron-driven scans and
  rundowns on a single host with a 128M workflows container, facing the
  cost of another service to run, we decided to **embed APScheduler 3.x
  (`AsyncIOScheduler`) in the FastAPI lifespan with an in-memory jobstore,
  rebuilding jobs from the brain's `schedules` table on every boot**, to
  achieve schedules managed over `/sched` with no extra container,
  accepting a hard single-uvicorn-worker requirement.
- **Options weighed**:
  - Celery beat or arq: both need Redis. Rejected as a second stateful
    service for a handful of jobs.
  - An external cron container calling the API: rejected, schedules would
    live outside the brain and `/sched` could not manage them.
  - FastAPI-Utils `@repeat_every`: no cron syntax, no persistence.
  - A Node scheduler: a second runtime to maintain.
- **Changed since**: the spec chose APScheduler 4 (alpha) with a
  SQLAlchemy datastore and named 3.x as the fallback. The code took the
  fallback (`apscheduler<4`). A serialising jobstore would try to persist
  the `brain_db` handle and its live sqlite connection, so the `schedules`
  table is the source of truth instead (`workflows/scheduler.py`
  docstring). Jobs run with `coalesce` and a 300 s misfire grace.
- **Negative consequences**: two uvicorn workers would fire every job
  twice. Scaling out means moving the scheduler to its own process first.

## TR-008: Brain is SQLite in the workflows container

- **Phase**: specify
- **Status**: accepted
- **Decision (Y-statement)**: In the context of a persistent knowledge store
  and task queue for one operator, facing an in-memory task list that was
  lost on every restart, we decided to **store pages, timeline, links,
  tags and tasks in SQLite inside the existing workflows container, with
  FTS5 for keywords and sqlite-vec for vectors merged by reciprocal rank
  fusion (k=60)**, to achieve hybrid search with no new service, accepting
  a single node with no replication and a less proven vector extension.
- **Options weighed**:
  - FTS5 only: no semantic search. Kept as the fallback when vectors are
    unavailable, not as the design.
  - PostgreSQL and pgvector as a separate service: more operations than
    one operator needs. Kept as the upgrade path if the brain outgrows a
    single file.
  - A hosted brain (gbrain on Supabase): a monthly bill and a Bun runtime.
- **Model**: rewritable "compiled" pages plus an append-only timeline and
  typed links. A content hash makes identical upserts no-ops, so they
  neither version nor re-embed. Chunks are 300 words with 50 overlap.
- **Changed since**: the spec embedded each page on write with one fixed
  model. The code embeds chunks from a 10-minute backfill job, with the
  model set by `EMBED_MODEL`/`EMBED_DIM` and one vec table per model, since
  vectors from different models cannot be compared. Planned query
  expansion, four-layer result dedup and `!remember`/`!recall` chat
  commands were never built.
- **Negative consequences**: every `BrainDB` call serialises on one
  `RLock`, and writes share one WAL file. A plain copy of a live
  `brain.db` can miss what is still in the `-wal` file, so back it up with
  the service stopped or with SQLite's `.backup`.

## TR-007: Keep the workflows container on root

- **Phase**: execute
- **Status**: accepted
- **Decision (Y-statement)**: In the context of persona renders that the
  workflows service writes into OpenClaw's state directory, facing root-owned
  0600 files the gateway (`runuser -u node`, uid 1000) could not read, we
  decided to **keep workflows on root and have each render copy the replaced
  file's mode and owner** (`persona._match_existing`: `fchmod` to the old
  mode, `fchown` to the old owner when running as root, 0644 for a new
  file), to achieve readable persona files without a migration, accepting
  that the service keeps root inside its container.
- **Options weighed**:
  - `user: "1000:1000"` on the workflows service (#16.c). Rejected for now:
    the service runs as root (no `user:` on the workflows service in either
    compose file, no `USER` in `workflows/Dockerfile`), so what it creates
    under the `./data/brain` bind mount, the brain database included, is
    root-owned. Switching would need a one-time ownership migration of that
    host directory first, or the database stops being writable.
  - Chmod every render to a fixed 0644. Rejected: it would loosen a file the
    operator tightened on purpose.
- **Negative consequences**: a compromise of the workflows process is root in
  its container. The container keeps `no-new-privileges` and a read-only
  root filesystem. Revisit with the ownership migration.

## TR-006: `/healthz` stays 200 while degraded

- **Phase**: execute
- **Status**: accepted
- **Decision (Y-statement)**: In the context of the workflows health probe,
  facing degraded states (broken brain query, no vector search, failed
  scheduler, unknown `CLAWRANGE_PROFILE`) that a restart cannot fix, we
  decided to **return 200 with `status: degraded` and a `problems` list**
  (#28.a, #28.c), to achieve an assistant that keeps answering while the
  operator fixes the cause, accepting that Docker reports the container
  healthy while it is degraded.
- **Options weighed**:
  - 503 when `problems` is non-empty. Rejected: the compose healthcheck and
    `workflows/entrypoint.sh` both use `urlopen`, which treats any non-200 as
    a failure. OpenClaw's `depends_on: condition: service_healthy` would keep
    the assistant from starting at all, and the entrypoint exits after
    `HEALTH_MAX_FAILS` consecutive failures, so `restart: unless-stopped`
    would loop the service over a bad profile or table that a restart
    leaves unchanged.
  - Teach the probes to parse `status`. Rejected for now: two probes to keep
    in step for no gain over reading the body.
  - Fail startup on an explicitly set but unknown profile. Rejected: same
    restart loop.
- **Negative consequences**: degraded state is visible only where someone
  looks: the `/healthz` body (problem names for anonymous callers, detail
  with the token), the boot Telegram alert (`Workflows started degraded`,
  which covers scheduler and profile errors, not db or vector search) and the
  logs.

## TR-005: Measure retrieval offline before tuning it

- **Phase**: execute
- **Status**: proposed
- **Decision (Y-statement)**: In the context of brain search quality, facing
  levers that the literature says help (RRF k and weights, contextual
  headers, chunk size, Matryoshka truncation), we decided to **gate changes on
  an offline eval** (`workflows/retrieval_eval.py`) and adopt only what clears
  a paired bootstrap, to achieve tuning backed by evidence, accepting that the
  fixture corpus is synthetic and small.
- **Evidence** (30 pages, 72 queries, `make retrieval-eval`):
  - Production hybrid scores hit@1 0.833 and MRR@10 0.900. hit@3 is 0.986,
    at the ceiling, so hit@1 and MRR carry the signal.
  - Keyword search ANDs every word, so it found the right page first for
    0.208 of questions. ORing the terms raised that to 0.792 (Δhit@1 +0.583,
    95% CI [+0.458, +0.708]). **Adopted** for hybrid's no-vector path only.
  - With vectors, no fused variant beat production: RRF k 10 to 100, keyword
    weight 0.5 or 2, depth 40, title headers and any-term fusion all have
    CIs spanning zero.
  - dim-512 is worse (ΔMRR -0.030, CI [-0.065, -0.002]); keep 1536.
    Offline truncation matches native 512 and 1024 vectors (cosine 1.0000).
  - chunk-150 is the most promising lever (deep-question hit@1 0.56 to 0.75,
    ΔMRR +0.022, CI [-0.016, +0.060]) but is not yet significant.
- **Options weighed**:
  - Adopt the research defaults directly: unmeasured on our data. Rejected.
  - LLM-generated golden queries: cheap at scale, but literature reports
    lexical-overlap and length bias. Hand-written, tagged queries for now.
  - Rerank via OpenRouter `/api/v1/rerank`: costs per query and needs the
    network; left for a live-only run.
- **Negative consequences**: The fixture is not real usage; a lever that
  wins here still needs a live check. Changing the chunking or header recipe
  in production would also need the recipe in the vector table's name, or
  old and new vectors would mix.
- **Reversibility**: HIGH. One default in `search_hybrid`.

## TR-004: Keep per-file dev mounts until the soul.md bind moves out of /app

- **Phase**: execute
- **Status**: superseded. The recommended option below shipped: the dev
  compose mounts `./workflows:/app:ro` and binds `openclaw/soul.md` at
  `/opt/soul.md` with `SOUL_MD_PATH=/opt/soul.md`, which
  `heartbeat._SOUL_MD_PATH` honors. The per-file mounts are gone. The
  `llm_proxy._SOUL_MD_PATH` named below is the pre-split location; it now
  lives in `workflows/heartbeat.py`.
- **Decision (Y-statement)**: In the context of the workflows dev hot-reload
  mounts, facing the trap that a per-file bind pins an inode and keeps serving
  pre-commit code after git or `ruff-format` replaces the file via
  `os.replace`, we decided to **keep the ~20 per-file mounts and the
  restart-on-commit ritual for now**, to achieve a working stack without
  growing an already red-zone branch, accepting that editing a mounted module
  through a rename still needs `docker compose up -d workflows`.
- **Evidence**: `./workflows:/app:ro` was tried and fails at container init:

      Error response from daemon: failed to create task for container:
      ... error mounting "<repo>/openclaw/soul.md" to rootfs
      at "/app/soul.md": create mountpoint for /app/soul.md mount:
      make mountpoint "/app/soul.md": read-only file system

  runc must create the `/app/soul.md` mountpoint before binding onto it, and
  with the whole tree mounted `:ro` that parent is read-only. `soul.md` is not
  in `workflows/`, so there is no existing file to bind over. The one-line
  swap does not exist.
- **Options weighed**:
  - `./workflows:/app` read-write (drop `:ro`): runc then creates the
    mountpoint *inside the host tree*, leaving a stray `workflows/soul.md`
    in the repo, plus root-owned `__pycache__`. Rejected.
  - Commit a placeholder `workflows/soul.md`: a fresh clone works only after
    someone recreates it, and it shadows `_SOUL_MD_PATH` (then in
    `llm_proxy`, now `heartbeat._SOUL_MD_PATH`) on host runs. Rejected.
  - Mount `./openclaw:/openclaw:ro` and make `_SOUL_MD_PATH` honor
    `SOUL_PATH` (which `app.py:90` already does), so nothing nests under
    `/app`. This also reconciles `app.py:93`, which renders to
    `/openclaw/soul.md` — a path with no bind mount today, so that render is
    invisible to the host. **Recommended, on main.** Confirmed live in the
    workflows startup log: `init_profile: persona render skipped: [Errno 30]
    Read-only file system: '/openclaw'` — the render does not merely miss the
    host, it fails outright against the container's read-only root.
- **Negative consequences**: A new `workflows/` module still needs a
  `docker-compose.yml` edit before `uvicorn --reload` can see it, and a
  rename-in-place on a mounted file still serves stale code until restart.
- **Reversibility**: HIGH. Config-only, plus one line in `heartbeat.py`
  (originally `llm_proxy.py`).

## TR-003: Regression-lock marketing seeds before deleting constants

- **Phase**: plan
- **Status**: accepted. The constants are deleted from `generators.py`;
  the seeds live only in `config/profiles/marketing/profile.yaml`, locked
  by `workflows/tests/test_profile.py` against
  `workflows/tests/golden_marketing_seeds.json`.
- **Decision (Y-statement)**: In the context of extracting `_DEFAULT_PROJECTS`
  /`_DEFAULT_SCHEDULES` from `generators.py` into `marketing/profile.yaml`,
  facing the risk of silently changing the live marketing assistant's seeded
  behavior, we decided to **write a seed-equivalence test RED against the new
  profile loader first (T03) and only delete the hardcoded constants once it is
  GREEN (T05)**, to achieve a provably behavior-preserving extraction,
  accepting the extra step of keeping both representations briefly in parallel.
- **Options weighed**: delete-and-reauthor in one step (faster, unverifiable,
  rejected). Keep constants forever and load profile only for new tenants
  (no real extraction, rejected). RED-lock then extract (chosen).
- **Negative consequences**: The marketing seeds exist in two places (code
  constant and YAML) for the duration of Sprint 0.
- **Reversibility**: HIGH, branch-local.

## TR-002: Minimal CRMAdapter interface and templated-SQL only

- **Phase**: specify
- **Status**: proposed
- **Decision (Y-statement)**: In the context of querying a pluggable CRM by
  natural language, facing both injection risk and the need for relational +
  time-series answers, we decided the LLM **selects a named, parameterized
  query template** (never emits SQL) and the `CRMAdapter` exposes a **minimal
  5-method interface** (`init`, `upsert`, `list`, `run_template`, `health`)
  executed read-only for queries, to achieve safety and swappability, accepting
  that arbitrary ad-hoc questions outside the template set are unsupported
  until a template is added.
- **Options weighed**: LLM-generated SQL (flexible, injection/correctness risk,
  rejected). Retrieve-then-summarize RAG (weak at aggregates/time-series,
  rejected as primary). Templated and LLM router (chosen).
- **Negative consequences**: New question shapes require a new template
  (a YAML edit). The REST adapter's `run_template` is a stub (server-side
  reports are out of scope this branch).
- **Reversibility**: HIGH. Interface and templates are branch-local.

## TR-001: Multi-tenant via hybrid declarative profile and connector registry

- **Phase**: brainstorm
- **Status**: proposed
- **Decision (Y-statement)**: In the context of making ClawRange
  pull-down-and-configure for a different business owner, facing a code-base
  with persona, seeds, and capabilities hardcoded to one tenant, we decided to
  **extract the tenant surface into a declarative YAML profile and add a
  code-level connector registry** (Source→Transform→Sink primitives and a
  pluggable `CRMAdapter`), with the current marketing setup becoming the
  `marketing` profile and a `lead-crm` profile shipping as the working
  reference, to achieve reuse without per-tenant Python edits, accepting a
  two-layer mental model (profile YAML and primitive registry) and a one-time
  extraction risk in the working marketing path.
- **Options weighed**:
  - A: Profile-as-data and connector registry (pure YAML): simple, but no home
    for the CRM/scrape primitives and no custom-logic escape hatch.
  - B: Profile-as-plugin (Python package per tenant): maximum power, but
    defeats the non-developer goal and adds tenant-code-loading security
    surface. Rejected.
  - C: Hybrid (chosen): operators compose in YAML, developers extend
    primitives in code. Subsumes A.
- **Negative consequences**:
  - Operators must learn a profile schema. Developers must learn the registry.
  - Extraction touches working marketing code → mitigated by seed-equivalence
    tests asserting byte-identical seeds before deleting the hardcoded
    constants.
  - The pluggable CRM and auth-pluggable scrape raise up-front abstraction cost →
    mitigated by minimal interfaces and a SQLite reference and REST stub.
- **Reversibility**: HIGH (RS ~0.80). All on a feature branch, git-revertible,
  no irreversible external side effect. War Room bypassed under Type-2,
  single-superior-approach, and explicit autonomy directive.
