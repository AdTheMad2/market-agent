# Handoff: Market Agent — Phase 0 gate passed, Phase 1 started
**Date:** 2026-08-05  **Session:** 20260805-1420  **Branch:** `phase-0-foundations` (base `master`; no remote)

## 1. Current State

✅ **Phase 0 complete.** All 7 providers live: `verify_quotas.py` exits 0, 7 OK / 0 failed / 0 skipped
✅ All 9 secrets in `.env`, `verify_secrets.py` exits 0, no leak across 36 tracked files
✅ Telegram delivery confirmed end to end — real message received, not just `getMe`
✅ Security fix: secrets redacted from exception/response text before printing (`scripts/_redact.py`)
✅ **Task 1.1 done** — `sma`, `rsi`, `volume_ratio` + 250-bar GOOG fixture. TDD, tests committed red first
🔄 **Phase 1 in progress.** Task 1.2 (triggers) is next and untouched
⬜ Tasks 1.3 suppressors, 1.4 ranking, 1.5 CLI. Phases 2–7 untouched

8 commits, working tree clean, **39 tests passing**, branch unmerged and unpushed.

## 2. Critical Context

- **No remote exists.** All work is local on `phase-0-foundations`. Nothing is backed up anywhere.
- **Repo intended public** — nothing personal may ever be committed. Re-check any new prose.
- Phase 0's hard gate is passed, so Phase 1 is unblocked. `docs/SPEC.md` §4 no longer needs revision.
- **Gemini is intermittently unavailable.** One `verify_quotas.py` run this session exited 1 and the
  next exited 0 with no code change; `gemini-3.5-flash` returned a direct 503 "high demand" earlier.
  Phase 5 needs retry + fallback, not only the empty-candidate guard.
- Secrets never on a command line — `.env` only. This held all session.

## 3. Decisions Made

| Decision | Choice | Rationale | Rejected Alternatives |
|---|---|---|---|
| Gemini model | `gemini-3.6-flash` | `gemini-2.5-flash` returns 404 for new keys while still listed by `ListModels`. 3.5-flash 503'd, 2.0-flash 429'd on the same key, same minute — model choice is an availability choice | `gemini-2.5-flash` (dead), `gemini-flash-latest` (alias drifts under us, bad for reproducibility) |
| Redaction point | At the single output site, not per-probe | Per-probe redaction means the next probe added leaks by default | Redacting at each of the 13 note-assignment sites |
| Redaction placeholder | `<REDACTED:NAME>`, names the variable | Keeps the error diagnosable — you learn which credential failed without seeing it | Blanking to `***` |
| Delivery check | New `scripts/verify_delivery.py`, separate from `verify_pipeline.py` | `verify_pipeline.py` is Task 4.2 and needs an engine that does not exist. Verifying the last hop alone lets a future pipeline failure be told apart from a delivery failure | Waiting for Phase 4; a one-off uncommitted curl |
| Indicator commits | Two commits, tests first and intentionally red | Makes TDD order visible in history rather than asserted in a message | One squashed commit |
| RSI on flat prices | Returns 50, not 100 | Zero average loss is the same arithmetic as all-gains; the obvious guard fires an overbought alert on a symbol that has not moved | Returning 100; raising |
| Fixture tests on real bars | Assert bounds, not hand-computed values | A mean lies between its window's min and max — cheap guard against an off-by-one window without pinning to a snapshot | Hand-computing 250-bar expectations; skipping real-data tests |

## 4. Immediate Next Steps

1. **Task 1.2 — triggers.** `engine/triggers.py`, `tests/engine/test_triggers.py`. Five rules, exact
   strings `ma_proximity`, `ma_cross`, `range_break`, `armed_level`, `rsi_extreme` — later tasks match
   on them. Positive **and** negative case each. Every threshold from `config/rules.yml`, none inline.
   Tests first, committed red. See `docs/IMPLEMENTATION_PLAN.md:195`.
2. Task 1.3 suppressors — the asymmetry test is the critical one: a suppressor has no code path that
   appends to the list (`docs/IMPLEMENTATION_PLAN.md:239`).
3. Task 1.4 ranking, Task 1.5 CLI. **Phase 1 observable check:** `python -m engine.cli
   tests/fixtures/bars_goog.json` prints the 150-day MA trigger, no network anywhere in the run.
4. Then Phase 2 — **confirm the Python 3.14 install at the START, not the middle** (R-9).

## 5. Key Files

| File | What changed | Why |
|---|---|---|
| `scripts/_redact.py` | Created | Strips live secrets from any printed text |
| `tests/test_redact.py` | Created, 10 tests | Realistic case is a requests exception carrying the URL |
| `scripts/verify_quotas.py` | Alpaca `start` added, `limit` removed, Gemini model + token budget, redaction at output | Phase 0 gate |
| `scripts/verify_delivery.py` | Created | `getMe` is not delivery |
| `engine/indicators.py` | Created | `sma`, `rsi` (Wilder), `volume_ratio` |
| `tests/engine/test_indicators.py` | Created, 17 tests | Hand-computed, arithmetic in comments |
| `tests/fixtures/bars_goog.json` | Created | 250 real SIP bars, 2025-08-06→2026-08-04, suite needs no network |
| `docs/RISKS.md` | §4: two dated verification blocks | Record of what was true vs what vendors claimed |
| `docs/SPEC.md` | §13 reconciled, §12 script table | Two "verified" items proved wrong |
| `docs/IMPLEMENTATION_PLAN.md` | Deploy checklist marked per surface | All local surfaces ✅ |

## 6. Patterns & Gotchas

- **A probe must exercise the property relied on, not one adjacent to it.** Three instances in one
  day: Telegram `getMe` reported OK while `TELEGRAM_CHAT_ID` held the *bot's own id* (a plausible
  10-digit number from a `getMe` response) — every alert would have gone nowhere with the gate green.
  Gemini returned 200 with an empty candidate. Alpaca returned 200 with zero bars. Authentication is
  not delivery, a 200 is not data, a listed model is not a callable one. Full table in `RISKS.md` §4.
- **`requests` exceptions embed the full request URL.** Telegram's token is a path segment; FRED and
  Marketaux keys are query params. Any network error printed a live credential, into output that
  `RISKS.md` publishes by design. Reproduced against a real token before fixing — substring check,
  not eye inspection. Redaction is now at the output choke point; keep it there.
- **Gemini 3.x bills thinking tokens against `maxOutputTokens`.** A 16-token budget returned 200 with
  74 thought tokens and no text — silent failure shaped like success. Read the answer from the *last*
  part; thought parts precede it.
- **Alpaca returns 200 with an empty `bars` list when `start` is omitted** rather than defaulting to a
  window. `limit` truncates from the *start* of the range, so it returns the oldest bars, not newest.
- **Telegram will not disclose a chat ID until the user messages the bot first.** `getUpdates` returns
  zero updates before that, which is how the wrong ID got used.
- Windows console is cp1252. A Telegram display name is arbitrary Unicode and raises
  `UnicodeEncodeError` rather than degrading. Reconfigure stdout to UTF-8 in anything printing
  user-supplied strings.
- The GOOG fixture is a snapshot ending 2026-08-04. Bounds tests stay valid; regenerating changes
  which specific triggers fire. The file records its own fetch parameters.

## 7. Open Questions

- **OQ-6 — repo goes public.** Effectively irreversible, and it is what buys unlimited Actions
  minutes. No remote exists yet, so this is still a free decision. **Needed before Actions secrets.**
- `TELEGRAM_TEST_CHAT_ID` is unset, so this session's test message went to the live alert chat. Must
  be a separate chat before Phase 4, or a dry run is indistinguishable from a real alert.
- **OQ-2 (largest, unchanged):** no verified free source for a US >$1B market-cap universe. Blocks
  Phase 7. Candidates untested: Finnhub `/stock/symbol`, Nasdaq screener CSV, SEC company-facts bulk.
- **OQ-3:** do daily bot commits prevent GitHub's 60-day auto-disable of scheduled workflows?
  Assumed yes, unverified.
- **OQ-4:** is a ceiling of 3 intraday alerts right? Unanswerable before ~2 weeks of live data. If the
  dropped count runs high, tighten the rules — do not raise the ceiling.

## 8. Quality Gate
- [x] All sections populated (no [TODO] placeholders)
- [x] No secrets or API keys in content
- [x] All referenced files exist
- [x] Next steps are specific
- [x] Decisions table has rationale filled

Score: 5/5  |  Status: READY
