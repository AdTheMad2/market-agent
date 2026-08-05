# Handoff: Market Agent — design phase + Phase 0 foundations
**Date:** 2026-08-05  **Session:** 20260805-1504  **Branch:** `phase-0-foundations` (repo created this session, base branch `master`)

## 1. Current State

✅ Brainstorm → spec → phased plan → risk register, all committed (`docs/`)
✅ Repo initialized, skeleton per SPEC §11, venv, `pyproject.toml`
✅ `scripts/verify_secrets.py` — 7 tests; leak detector proven end-to-end against a planted fake value
✅ `scripts/_dotenv.py` — 5 tests; exists so no key is ever typed on a command line
✅ `scripts/verify_quotas.py` — 7 live provider probes, written and running
🔄 **Phase 0 is NOT done.** Its observable check is "live 200 + real data from every provider." All seven currently report `SKIP` — no keys exist yet.
⬜ Phases 1–7 untouched. No `engine/`, no jobs, no dashboard.

5 commits, working tree clean, 12 tests passing.

## 2. Critical Context

- **Blocked on account registration.** Nothing else in Phase 0 can proceed. See §4.
- Phase 0 is a **hard gate**: if a provider fails its live probe, `docs/SPEC.md` §4 gets revised *before* Phase 1 starts. Do not skip ahead to the engine.
- Free tier only, whole stack. Any component needing payment must be flagged, not adopted.
- **Repo is intended to be public** (unlimited Actions minutes). Nothing personal may ever be committed — no positions, sizes, account details, community memberships. This was violated once already in the first spec draft and corrected; re-check any new prose.
- Secrets never on a command line. `.env` is gitignored and verified so.
- `master` has the docs commit; all Phase 0 code is on `phase-0-foundations`, unmerged. No remote exists yet.

## 3. Decisions Made

| Decision | Choice | Rationale | Rejected Alternatives |
|---|---|---|---|
| Compute + scheduling | GitHub Actions, public repo | Unlimited free minutes on public repos; 5-min minimum cron interval | Vercel Cron (Hobby = 1 run/day, ±59min — verified, disqualifying); Cloudflare Workers (10ms CPU/invocation); Oracle Always Free (sysadmin load, capacity lottery, 7-day idle reclaim); local Windows box (misses runs when off — the exact failure being fixed) |
| Dashboard hosting | Vercel Hobby, static, reads committed JSON | User has prior good experience; deploy-on-push; same price as alternative | GitHub Pages (equivalent cost, worse DX for this user) |
| Discord community ingestion | Dropped from v1 | No bot invite available; self-bot violates Discord TOS and risks the personal account | Manual paste, weekly batch export |
| Alert ceiling | 2 digests + max 3 intraday, enforced in code | Noise is the failure mode being escaped; 4th trigger is dropped, logged, reported in digest — forces the ranking to rank | 5/day, 1/day, uncapped-with-small-watchlist |
| Judgment boundary | Only deterministic rules arm alerts; Gemini phrases a pre-computed evidence packet, validator rejects any number/ticker/date not in it | Bounds wrongness to prose, never to signals | LLM ranks candidates (unverifiable, fails invisibly); LLM proposes setups |
| Event proximity | Suppressor only, never a trigger | Asymmetric cost: wrongly suppressing = missed setup; absent = confident recommendation into an earnings print | Trigger; omit entirely |
| Price data | 15-min-delayed SIP via Alpaca | Free real-time is IEX-only, ~2.5% of volume, misses level touches. Correct-late beats wrong-now | Alpaca IEX real-time; Finnhub WebSocket |
| Intraday poll | Every 15 min | Data is already 15-min delayed; 10-min polling refetches the same bar for zero freshness gain | 10 min, 5 min |
| Watchlists | Two: hand-edited core + weekly agent-screened >$1B | User request, verbatim | Single list; fully-screened; index seed |
| `.env` loader | 20 lines in `scripts/_dotenv.py` | A dependency that handles secrets is a dependency worth not having | python-dotenv |

## 4. Immediate Next Steps

1. **Register the free accounts** (none needs a card): Alpaca (data-only, no funding), Finnhub, Marketaux, Gemini via AI Studio, FRED. Telegram bot via @BotFather, then chat ID via `getUpdates`. EDGAR needs no key — just set `EDGAR_USER_AGENT` to `market-agent <email>`.
2. `cp config/.env.example .env` and fill it in. **Do not paste keys into chat.**
3. Run `python scripts/verify_secrets.py` — expect exit 0, empty leak report.
4. Run `python scripts/verify_quotas.py` — **this is the Phase 0 gate.** Expect zero SKIPs.
5. Paste that output into `docs/RISKS.md` §4 as a dated block. That log is the record of what was true, versus what a vendor page claimed.
6. If any provider fails: revise `docs/SPEC.md` §4 before writing any Phase 1 code.
7. Only then start Phase 1 (pure engine, tests first — see `docs/IMPLEMENTATION_PLAN.md`).

## 5. Key Files

| File | What changed | Why |
|---|---|---|
| `docs/SPEC.md` | Created | Problem, non-goals, architecture, quota table, engine rules, alert contract, LLM boundary, verified-vs-assumed split |
| `docs/IMPLEMENTATION_PLAN.md` | Created | 8 phases, TDD steps, deploy checklist with ✅/⬜ per surface |
| `docs/RISKS.md` | Created, then R-9 + verification log appended | 9 decisions, 9 risks, 6 open questions |
| `scripts/verify_secrets.py` | Created | Required-name check + tracked-tree leak scan |
| `scripts/verify_quotas.py` | Created | The Phase 0 gate |
| `scripts/_dotenv.py` | Created | Keys off the command line |
| `config/rules.yml` | Created | Every threshold; none in code |
| `config/.env.example` | Created | Key names only, never values |

## 6. Patterns & Gotchas

- **Host runs Python 3.14.0**, plan assumed 3.12. Harmless now (`requests`, `pyyaml` install fine). Risk lands in Phase 2 if `pandas-market-calendars` has no 3.14 wheel. `engine/` is pure Python by design so the maths is insulated; CI runner can be pinned to 3.12. Logged as R-9. **Confirm this install at the START of Phase 2, not the middle.**
- Windows console is cp1252 and mangles em dashes. `verify_quotas.py` calls `sys.stdout.reconfigure(encoding="utf-8")` because its output gets pasted into docs.
- Leak reports must never echo the leaked value — that turns the report into a second leak. `scan_files_for_values` returns `(path, varname)` only, and there is a test asserting the value is absent from `repr()`.
- Values under 8 chars are not scanned for leaks — they match half the tree and report nothing useful.
- Binary files are skipped in the leak scan, not raised on. A committed SQLite DB is expected in this repo.
- Alpaca free SIP requires `end` ≥15 min in the past; **16 minutes is used** as slack — a 15-minute-exact `end` intermittently 403s.
- GitHub cron minutes must be `:15`/`:45`, never `:00` — the scheduled queue backs up at the top of the hour, and runs can be dropped entirely under load.
- Finnhub bills **1 call per symbol** for news. Price data is not the constraint, news is. Intraday runs fetch bars only; there is a guard planned for this in Task 2.3.

## 7. Open Questions

- **OQ-2 (largest):** no verified free source for a US >$1B market-cap universe. Blocks Phase 7. Fallback is index constituents, which narrows discovery to large caps. Candidates to test: Finnhub `/stock/symbol`, Nasdaq screener CSV, SEC company-facts bulk.
- **OQ-6:** public repo is effectively irreversible once pushed. Decided public for unlimited minutes — worth re-confirming before the repo is created remotely.
- **OQ-3:** do daily bot commits prevent GitHub's 60-day auto-disable of scheduled workflows on public repos? Assumed yes, unverified. Cheap insurance is a Healthchecks.io dead-man's-switch.
- **OQ-4:** is a ceiling of 3 intraday alerts right? Unanswerable before ~2 weeks of live data. If the dropped count runs high, tighten the rules — do not raise the ceiling.

## 8. Quality Gate
- [x] All sections populated (no [TODO] placeholders)
- [x] No secrets or API keys in content
- [x] All referenced files exist
- [x] Next steps are specific
- [x] Decisions table has rationale filled

Score: 5/5  |  Status: READY
