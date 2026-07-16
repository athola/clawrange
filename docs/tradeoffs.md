# Tradeoffs Journal

Decisions, the alternatives weighed, and what was given up. Newest first.

## Active Index

| ID | Phase | Status | Decision |
|----|-------|--------|----------|
| TR-003 | plan | proposed | Regression-lock marketing seeds (RED equivalence test) before deleting constants |
| TR-002 | specify | proposed | Minimal CRMAdapter interface and templated-SQL only (no LLM raw SQL) |
| TR-001 | brainstorm | proposed | Multi-tenant via hybrid declarative profile and connector registry |

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

## TR-003: Regression-lock marketing seeds before deleting constants

- **Phase**: plan
- **Status**: proposed
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
