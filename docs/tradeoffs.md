# Tradeoffs Journal

Decisions, the alternatives weighed, and what was given up. Newest first.

## Active Index

| ID | Phase | Status | Decision |
|----|-------|--------|----------|
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
lived in `docs/archive/` (removed; see `git log -- docs/archive`). Each was
checked against the code when it was written here, and the
**Changed since** lines record where the shipped design moved away from
the plan.

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
