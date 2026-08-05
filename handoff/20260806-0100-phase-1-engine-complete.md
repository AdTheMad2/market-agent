# Handoff: Market Agent — Phase 1 engine complete
**Date:** 2026-08-06  **Session:** 20260806-0100  **Branch:** `phase-0-foundations` (base `master`; no remote)

## 1. Current State

✅ **Phase 1 complete.** Tasks 1.2–1.5 all done, 12 new commits (`ecedc2e..ee6f6af`)
✅ `engine/triggers.py` — `Bar`, `Rules`, `Trigger`, `evaluate()`; five rules, every threshold from `config/rules.yml`
✅ `engine/suppressors.py` — demote-only; the "no code path appends" asymmetry tested directly
✅ `engine/ranking.py` — sort + hard ceiling; conserves input (`to_send + dropped` = every item exactly once)
✅ `engine/cli.py` — `evaluate → apply → rank`, reads a fixture, no network
✅ **Phase 1 observable check passes:** `python -m engine.cli tests/fixtures/bars_goog_ma150.json` prints
   `GOOG ma_proximity level=336.57 price=335.76 dist=0.24% bar=2026-07-29T04:00:00Z`
✅ Whole-phase review done (nemesis) + one fix wave + scoped re-review + one residual closed
⬜ Phases 2–7 untouched. Phase 2 (Alpaca source + SQLite state) is next.

**88 tests passing**, working tree clean, branch unmerged and unpushed.

## 2. Critical Context

- **No remote exists.** Everything is local on `phase-0-foundations`. Nothing is backed up anywhere.
  This is now 20 commits of unbacked work — worth resolving before Phase 2 grows it further.
- **Repo intended public** — nothing personal may ever be committed.
- Phase 2 must **confirm the Python 3.14 install at the START, not the middle** (`docs/RISKS.md` R-9).
- Gemini remains intermittently unavailable (Phase 0 finding, unchanged). Phase 5 needs retry + fallback.
- Per-task reviews were switched OFF mid-session by the user (token cost). One whole-phase review at the
  end replaced them, and it worked — it caught a real ranking defect. Keep that shape for Phase 2.
- SDD scratch (ledger, briefs, per-task reports) lives in the gitignored
  `.superpowers/sdd/IMPLEMENTATION_PLAN/`. Useful next session, but not versioned — do not rely on it.

## 3. Decisions Made

| Decision | Choice | Rationale | Rejected Alternatives |
|---|---|---|---|
| Phase 1 observable-check fixture | New `tests/fixtures/bars_goog_ma150.json`: the same real bars sliced to 2026-07-29 | The plan asserted `bars_goog.json` fires the 150-day MA trigger. It does not — its last close (375.35) is 11.13% above SMA150 (337.77). The 2026-07-29 session closed 335.76 against SMA150 336.57 = 0.239%, a genuine proximity | Lowering `ma_proximity_pct` to force a hit (tuning a threshold to pass your own test); regenerating the fixture over the network; declaring Phase 1 done on a failing check |
| Ranking urgency | Breach rules (`armed_level`, `ma_cross`, `range_break`, `rsi_extreme`) get urgency 0.0; `ma_proximity` uses `distance_pct` | `distance_pct` is a % of a *price* for MA rules but a % of the *RSI point scale* for `rsi_extreme` — comparing them in one sort is a unit error that buried every RSI trigger, and made a *more* extreme RSI sort *worse* | Normalising RSI distance onto a price-like scale (still two meanings in one field); leaving it and documenting the bias |
| `Trigger.distance_pct` type | Plain `float`; call sites skip constructing a `Trigger` when `_distance_pct` returns `None` | A `Trigger` with no distance is unusable to every downstream consumer. Widening to Optional pushed the guard onto Phase 2+ and had already left `cli.py`'s formatter able to `TypeError` | `float | None` with `None`-guards at each consumer |
| Negative `already_sent_today` | Raise `ValueError` | It can only come from a caller bug in Phase 2/4 state. Returning an empty `to_send` makes that bug look exactly like a quiet market — silent, undiagnosable | Silently yielding no alerts (the original behaviour) |
| Demoted `armed_level` vs clean trigger | Armed still wins; behaviour kept, now tested and documented in the sort key's docstring | The user armed that level deliberately; it should still be reported, carrying its demotion reason | Making demotion outrank armed primacy |
| Rules loading | `Rules.from_mapping` / `SuppressorRules.from_mapping` take an already-parsed mapping; YAML is read only by `engine/cli.py` | Keeps `engine/` pure — the constraint the whole phase exists to protect | `engine/` reading `config/rules.yml` itself |
| `Bar`/`Rules` location | Both in `engine/triggers.py` | The plan authorises no other Phase 1 module; a `models.py` would have been unrequested scope | A separate `engine/models.py` |
| Review cadence | One whole-phase review, then one fix wave, then a scoped re-review | User instruction mid-session: per-task reviews were burning tokens for their value | Per-task review after each of 1.2–1.5 (the skill's default) |

## 4. Immediate Next Steps

1. **Decide the remote/public question (OQ-6)** before Phase 2 adds more unbacked commits.
2. **Phase 2, Task 2.1 — Alpaca bars source.** `sources/alpaca.py`, `tests/sources/test_alpaca.py`.
   Tests run against a recorded HTTP fixture, never the live API. Assert the request carries `feed=sip`
   and an `end` at least 15 minutes in the past. `docs/IMPLEMENTATION_PLAN.md` Task 2.1.
   **Confirm the Python 3.14 install first** (R-9).
3. Task 2.2 SQLite state (`sources/store.py`, `data/schema.sql`). Phase 2 observable check:
   `market.db` holds 250 daily bars for every core-watchlist name and a second run adds zero rows.
4. Run Phase 2 the same way: subagent per task, no per-task review, one review at phase end.

## 5. Key Files

| File | What changed | Why |
|---|---|---|
| `engine/triggers.py` | Created | `Bar`, `Rules`, `Trigger`, `evaluate()` — five rules |
| `engine/suppressors.py` | Created | `SuppressorRules`, `SuppressionContext`, `Suppressed`, `apply()` |
| `engine/ranking.py` | Created | `rank()` — order, urgency, hard ceiling, nothing discarded |
| `engine/cli.py` | Created | The edge: reads the fixture and `config/rules.yml`, prints the table |
| `tests/engine/test_triggers.py` | Created | Positive + negative per rule, `bar_timestamp` asserted explicitly |
| `tests/engine/test_suppressors.py` | Created | Window boundaries; the never-appends asymmetry |
| `tests/engine/test_ranking.py` | Created | Ceiling absolute, budget accounting, conservation, urgency ordering |
| `tests/fixtures/bars_goog_ma150.json` | Created | First 246 bars of the 250-bar fixture; ends where SMA150 proximity genuinely holds |
| `docs/IMPLEMENTATION_PLAN.md` | Task 1.5 + phase table renamed the check fixture; Phase 1 steps ticked | The plan's fixture claim was false and is now corrected in place |

## 6. Patterns & Gotchas

- **A plan can be wrong about data it never fetched.** Phase 1's done-criterion named a fixture that
  cannot satisfy it — the plan was written before the bars existed. When an observable check fails,
  check the check before touching the code, and never tune a threshold to make your own test pass.
- **A field that means different things per branch will be compared across branches eventually.**
  `distance_pct` held a price percentage and an RSI-point percentage. Nothing crashed; the ordering was
  just quietly wrong — this phase's stated failure mode, a plausible-looking alert nobody questions.
- **Widening a type to Optional is a change to every consumer**, not a local fix. The `_distance_pct`
  guard was correct in isolation and left `cli.py` able to `TypeError` on a formatter line.
- **Four subagents writing blind to each other produce coherent modules only if the controller hands
  each one the previous module's exact interface.** The dispatch prompts carried the signatures; no
  integration mismatch appeared at the CLI.
- **Reviewers without an execution tool cannot verify arithmetic over 150 real numbers.** The review
  flagged the fixture's SMA as unverified — correctly. The controller recomputed and ran the CLI. Give
  a reviewer exec access, or expect to close that gap yourself.
- Both the whole-phase review and the fix wave died once to a session limit mid-run. State survived
  because every task committed before returning — the loss was one review, not any work.

## 7. Open Questions

- **OQ-6 — repo goes public.** Still undecided, still free (no remote exists), and now blocking a
  backup for 20 commits of work. **Needed before Actions secrets.**
- `TELEGRAM_TEST_CHAT_ID` still unset — a dry run is indistinguishable from a real alert. Must be a
  separate chat before Phase 4.
- **OQ-2 (largest, unchanged):** no verified free source for a US >$1B market-cap universe. Blocks
  Phase 7. Untested candidates: Finnhub `/stock/symbol`, Nasdaq screener CSV, SEC company-facts bulk.
- **OQ-3:** do daily bot commits prevent GitHub's 60-day auto-disable of scheduled workflows? Assumed
  yes, unverified.
- **OQ-4:** is a ceiling of 3 intraday alerts right? Unanswerable before ~2 weeks of live data.
- Should `range_break`'s non-positive-level guard stay? It is unreachable with real prices (a prior
  high/low of positive closes), kept for type coherence and documented as such rather than fake-tested.

## 8. Quality Gate
- [x] All sections populated (no [TODO] placeholders)
- [x] No secrets or API keys in content
- [x] All referenced files exist
- [x] Next steps are specific
- [x] Decisions table has rationale filled

Score: 5/5  |  Status: READY
