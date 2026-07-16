# PR Fix: #11 Persona config and meta-learning, plus research-pulse scheduling

Addresses the review in `reviews/pr-11-review.md` (2 blocking, 8
non-blocking, 6 suggestions, 3 spec deltas). Fix commits
`63046d6..a6b3cde` (5 commits).

## Reconciliation

| Triage ID | Category | Disposition | Evidence |
|-----------|----------|-------------|----------|
| B1 silent profile fallback | Blocking | Fixed | `63046d6`: fail closed, boot-profile fallback, 2 regression tests |
| B2 reject-after-approve stale render | Blocking | Fixed | `cbdd2c9`: reject re-renders surviving approved set, unit and API regression tests |
| N1 target default mismatch | Non-blocking | Fixed | `d9ac465`: default `target=""`, defaults pinned together by test |
| N2 unguarded raise_for_status | Non-blocking | Fixed | `d9ac465`: degrades to friendly synthetic response, 2 tests |
| N3 reflect generator untested | Non-blocking | Fixed | `4th commit` (`test(persona)`): 4 tests incl. `None.` regression |
| N4 embeddings failure untested | Non-blocking | Fixed | `d9ac465`: non-200→same status, transport→502 |
| N5 render_all no-raise untested | Non-blocking | Fixed | `test(persona)` commit: `{"soul": False}` asserted |
| N6 seed boot-guard untested | Non-blocking | Fixed | `test(persona)` commit: malformed learned.yaml survives boot |
| N7 tautological signal test | Non-blocking | Fixed | `cbdd2c9`: asserts `source == "signal"` proposal and below-threshold case |
| N8 conftest missing persona_learnings | Non-blocking | Fixed | `test(persona)` commit: table added to reset list |
| S1 bound target length | Suggestion | Fixed | `cbdd2c9`: `TARGET_MAX = 80` and limit tests |
| S2 whole-dir mount rationale | Suggestion | Documented | `a6b3cde`: os.replace breaks single-file bind mounts |
| S3 identity.md dangling | Suggestion | Documented | `a6b3cde`: noted human-readable render artifact, deliberately unmounted |
| S4 case test comment/assert mismatch | Suggestion | Fixed | `d9ac465`: asserts preserved casing, mixed-case input |
| S5 mission-state hardcoded counts | Suggestion | Skipped | Closeout record is a point-in-time snapshot by design |
| S6 PR body commit count | Suggestion | Fixed | PR body updated to 20 commits / ~3.6k insertions |
| Spec delta 1 `!learn` alias | Spec | Fixed | `d9ac465`: intercepted as `!persona` alias and test |
| Spec delta 2 Telegram notice on propose | Spec | Issue #12 | https://github.com/athola/clawrange/issues/12 |
| Spec delta 3 setup-time workspace render | Spec | Issue #13 | https://github.com/athola/clawrange/issues/13 |

## Validation

- `python3 -m pytest workflows/tests/`: **658 passed** (637 baseline and 21 new)
- `ruff check` and `ruff format --check`: clean
- TDD discipline: B1, B2, N1, N2, `!learn`, and S1 each demonstrated a
  failing test before the fix (revert-sensitivity proven by construction)

Manual/live checklist items from the review test plan (Telegram round-trip,
tome-bridge pulse, live embeddings) were **not run**: they need running
containers and live keys. Unchanged behavior is covered by unit seams.
