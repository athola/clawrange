# PR Review: #11 Persona config and meta-learning, plus research-pulse scheduling

**Verdict: Changes requested. 2 blocking findings.**
Scope mode: standard. Reviewed `main..variable-config` (35 files, +3272/−32, 15 commits).
Version consistency check: N/A (no version manifest in repo).

## Scope compliance

Baseline: `docs/superpowers/specs/2026-06-26-persona-config-meta-learning-design.md`
plus the 9-task plan. PCM spec coverage is ~95%. The diff implements the system
end-to-end (identity block, `persona_learnings` table, `## Learned` overlay,
atomic `render_all`, propose/approve/reject, `/persona/*` router, `!persona`
proxy commands, reflect generator, `learned.yaml` round-trip, Max example).

**Spec deltas (in-scope, minor):**
1. `!learn` alias (spec §6) is not intercepted in `llm_proxy.py`: add it or strike it from the spec.
2. Telegram notice on propose (spec §6/§8) is not implemented. Drafts surface only via `!tasks`.
3. Setup-time render (spec §7/§8) does not write the workspace `SOUL.md`/`IDENTITY.md`. Only runtime approval or `POST /persona/render` does.

**Riders (out of spec scope, disclosed in PR body, acceptable):**
- `5b2a391` GLM 5.1→5.2 and Zhipu-backed `POST /v1/embeddings` route.
- `7a69e6b` `research_pulse` generator, marketing schedule, and HEARTBEAT rewrite.

Both are self-contained, tested, single commits. Ideal hygiene would have been
two separate PRs, but merging as-is is defensible for a single-operator repo.

## Blocking issues

### B1 (`workflows/app.py:147`): silent profile fallback can overwrite the live persona
`_current_profile()` catches any exception from `load_profile()` and substitutes
a hardcoded `Profile(name="starter", raw={"profile": "starter", "assistant": {}})`.
If `profile.yaml` has an error (including one triggered by this PR's new
`assistant.identity` validation), every `/persona/*` request resolves to the
bogus starter profile, and `/persona/render` or an approve then renders the
generic placeholder persona **over the tenant's real `soul.md`/`identity.md`
and bind-mounted workspace files**, with no error surfaced.
**Fix:** reuse the boot-validated `app.state.profile`, or let the exception
propagate as a 5xx. Note `test_persona_api.py` injects a fake profile provider,
so this path is never exercised by tests: add a regression test with the fix.

### B2 (`workflows/persona_learning.py:59-63`): `reject()` after `approve()` leaves stale rendered content
`reject()` flips DB status without re-rendering and has no guard against
rejecting an already-`approved` row. Reproduced empirically: approve (renders),
then reject: `render_fn` never fires again, so `soul.md`/`identity.md` still
contain the rejected content while the DB says it's gone.
**Fix:** re-render the surviving approved set when rejecting a previously
approved row, or enforce that reject is only valid for `pending` rows.

## Non-blocking improvements (in-scope)

| # | Location | Finding |
|---|----------|---------|
| N1 | `llm_proxy.py:638-651` | `_post_persona_propose` defaults `target="feedback"`, so bare `!persona` feedback renders under a `**feedback:**` heading instead of `General` (the API default). Align defaults. |
| N2 | `llm_proxy.py:650,659` | Unguarded `r.raise_for_status()` in `!persona` propose/reflect paths → raw 500 to Telegram on persona-API failure. Every sibling command (`_handle_task_command`) degrades to a friendly `_synthetic_response`. Wrap in try/except. |
| N3 | `generators.py` | `persona_reflect_generator` has zero tests despite commit `3920d1d` fixing a real bug there ("none" response). Add: `"none"`/empty → no proposal. Real suggestion → `propose(source="reflect")`. |
| N4 | `llm_proxy.py` | Embeddings upstream-failure branch untested: non-200 should map to same-status HTTPException, `httpx.HTTPError` → 502. Reverting that branch would return Zhipu error JSON as HTTP 200 into OpenClaw memory. |
| N5 | `persona.py` | `render_all`'s no-raise contract (unwritable target → `False`) is untested. If reverted to raising, approve would 500 after committing status, diverging DB and files. |
| N6 | `generators.py` | `seed_from_profile`'s boot-guard around a malformed `learned.yaml` is untested. No test seeds via a profile at all. |
| N7 | `tests/test_persona_learning.py:160-166` | Tautological test: signal-scan enabled path only asserts `isinstance(out, list)`: reverting the heuristic to `return []` passes. Assert a proposal with `source == "signal"`. |
| N8 | `tests/conftest.py` | `persona_learnings` missing from `_reset_brain_db` table list: latent cross-test state leak. |

## Suggestions

- `persona_learning.py:19-30`: bound `target` length (e.g. 80 chars). It is interpolated verbatim into rendered headings.
- `docker-compose.yml`: spec §7 called for a narrow mount of render files. The whole `data/openclaw-state` dir is mounted rw with no recorded rationale. Narrow or document.
- `openclaw/identity.md` is rendered but mounted into no container: dangling artifact. Mount RO or note it is human-readable only.
- `test_llm_proxy.py` case-insensitivity test: comment says casing is preserved but the assertion lowercases: drop `.lower()` or the comment.
- `.attune/mission-state.json` `closeout.verification` hard-codes point-in-time test counts that will rot.
- PR body says "16 commits". The branch has 15.

## Documentation quality

Slop scan across 7 changed markdown files: **zero banned-phrase hits**, scores 0–2/10.
Accuracy verified: all documented make targets (`persona`, `persona-export`,
`persona-demo`, `tome-bridge*`) and all 9 `/persona` endpoints match Makefile
and `persona_api.py` exactly. Non-blocking.

## PR hygiene

15 atomic commits mapping 1:1 to plan tasks, conventional format, no AI
attribution, no emoji. `.attune/` updates follow existing repo convention.
Untracked `.tome/` and `docs/research/` correctly excluded.

## Test suite assessment

43/43 new persona/research tests pass. Assertions are behavioral and
revert-sensitive in the core areas (approve→render, overlay round-trip,
research-pulse staleness/dedup, embeddings routing). Gaps are listed above
(N3–N8). Overall quality: strong.
