# Risks and Open Questions

Companion to [SPEC.md](./SPEC.md) and [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md).
Every question raised at kickoff is either **decided with its reasoning** (§1) or **open
with what it is blocked on** (§3).

---

## 1. Decisions made, with reasoning

| # | Question | Decision | Reasoning |
|---|---|---|---|
| D-1 | Where does this run? | **GitHub Actions on a public repo** for compute and scheduling; **Vercel Hobby** for the dashboard only | Vercel Hobby cron is once per day at ±59 min precision — intraday polling is impossible on it. Cloudflare Workers free allows 10 ms CPU per invocation. Oracle Always Free is the only free always-on option but costs a sysadmin and carries a capacity lottery plus 7-day idle reclamation. A local Windows machine reproduces the exact failure the project exists to fix. Public repos get unlimited free Actions minutes; private gets 2,000/month. All verified 2026-08-05. |
| D-2 | How does private Discord community content get in? | **Dropped from v1 entirely** | No bot invite is available for the server in question. Automated reading with a personal account is a self-bot and violates Discord's Terms of Service. Such sources continue to be read manually, outside the system. See OQ-1. |
| D-3 | Which free data sources sustain the watchlist at this cadence? | **Alpaca Basic** for prices; **Finnhub** for news and earnings; **Marketaux** as news backup; **EDGAR** and **FRED** for events | Alpaca gives 200 req/min with a multi-symbol endpoint — 100 names of daily bars is ~3 calls. Price data is not the constraint; Finnhub news at 1 call per symbol is. Hence: **intraday runs fetch bars only.** Alpha Vantage (25/day) and Polygon (5/min) were rejected as unusable. |
| D-4 | Where is the line between mechanical triggers and judgment? | **Only deterministic rules can arm an alert.** The LLM is a readability layer that decides nothing | The engine computes every number and names every rule. Gemini receives a complete evidence packet and phrases it; a validator rejects any number, ticker, or date not in the packet; a deterministic template ships on any failure. Wrongness is bounded to prose, never to signals. No composite confidence score exists, because there is nothing to calibrate one against. |
| D-5 | What is the alert volume ceiling? | **2 digests + maximum 3 intraday alerts per day**, enforced in code | Noise is the failure mode being escaped. A 4th qualifying trigger is dropped, logged, and reported in the post-close digest rather than sent — which forces the ranking to genuinely rank instead of degrading into a firehose on volatile days. |
| D-6 | Event proximity — trigger or suppressor? | **Suppressor only.** It can demote, never generate | Asymmetric costs. A suppressor firing wrongly costs a missed setup; a suppressor absent costs a confident recommendation into a known binary event such as an earnings print. The second is worse. |
| D-7 | Intraday poll interval? | **15 minutes** | The underlying SIP data is 15 minutes delayed. Polling every 10 minutes refetches the same bar, buying zero freshness while spending quota and adding commits. |
| D-8 | Watchlist composition? | **Two separate lists** — hand-edited core, and a weekly agent-screened list from the >$1B US universe | Requested directly by the user. Both are scanned identically and tagged distinctly in every output, so provenance is never ambiguous. |
| D-9 | Real-time or delayed prices? | **15-minute-delayed SIP** | Free real-time is IEX-only — one venue carrying ~2.5% of US volume, which will miss level touches outright. Correct prices late beats wrong prices now. Stated in every alert. |

---

## 2. Risk register

### R-1 — Free-tier quota exhaustion or silent tier change
**Likelihood:** high over a 12-month horizon. Alpha Vantage cut its free tier from 500/day
to 25/day; Fly.io and Railway removed free tiers entirely; Render cut included egress.

**Impact:** scans fail silently and the user goes back to discovering things late — the
original problem, now with false confidence that something is watching.

**Mitigation:** `scripts/verify_quotas.py` prints each provider's live rate-limit headers
rather than asserting documented values, and is re-run on every change. Every source is
behind a one-file adapter in `sources/`, so a swap is one file. **Additionally: any run
that fails must notify Telegram** — a silent failure is the dangerous one. *(Add a
`failure()` step to every workflow that posts to Telegram; folded into Task 3.3.)*

**Residual:** accepted. Free tiers are not owed to anyone.

---

### R-2 — A data provider disappears or changes terms
**Likelihood:** moderate. Polygon rebranded to Massive in late 2025; providers restrict
free tiers to non-commercial use without warning.

**Impact:** total loss of price data if Alpaca is the casualty.

**Mitigation:** `sources/alpaca.py` is a single file behind a stable internal shape.
Documented fallback order: **Alpaca → Twelve Data (800 credits/day) → yfinance
(unofficial, emergency only)**. `yfinance` scrapes undocumented Yahoo endpoints and has
broken for days at a time; it is a bridge, never a destination.

**Residual:** a multi-day outage is survivable; the user simply reads the market manually,
as today.

---

### R-3 — GitHub cron delay makes alerts late
**Likelihood:** certain. Scheduled workflows are explicitly best-effort with no timing SLA;
5–30 minute delays are documented and common, and runs can be **dropped entirely** under
load.

**Impact:** an armed level alert lands 30–45 minutes after the touch when combined with the
15-minute data delay.

**Mitigation:** cron minutes at `:15`/`:45`, never `:00`. Every workflow carries
`workflow_dispatch` for manual recovery. Tolerable **only** because target holding periods
are multi-day and the data is already delayed — this is stated in the alert itself so the
lateness is never a surprise.

**If it becomes intolerable:** the documented escalation is an external scheduler
(cron-job.org, free) calling the `workflow_dispatch` API, which uses a different queue and
fires within seconds. Not built in v1 — extra moving part, extra token to hold.

**Residual:** accepted, with the escalation path written down.

---

### R-4 — Scheduled workflows silently disabled
**Likelihood:** low-moderate. GitHub disables scheduled workflows on **public** repositories
after 60 days with no repository activity, with no email and no log entry.

**Impact:** the system stops, silently, and the user does not find out until they notice
digests stopped arriving.

**Mitigation:** the workflows themselves commit `data/` daily, which should constitute
activity. **This is an assumption and must be confirmed empirically**, not trusted. A
dead-man's-switch (a free Healthchecks.io ping from each digest run, alerting on absence)
is the cheap insurance and should be added in Phase 3 if confirmation is not obtained.

---

### R-5 — Alert fatigue
**Likelihood:** moderate. The stated ceiling is untested against real market days.

**Impact:** the user stops reading alerts, and the project has failed even while working.

**Mitigation:** the ceiling is enforced in code, not by convention. Dropped triggers are
counted and reported, so **the digest reveals whether the ceiling is too low** — if it
reports 8 dropped items daily, the rules are too loose and want tightening rather than the
ceiling raising.

**Review trigger:** if the user ignores alerts for a week, or the dropped count exceeds 5
per day for a week, revisit the rule thresholds — not the ceiling.

---

### R-6 — Being wrong about a setup
**Likelihood:** certain, repeatedly. Technical triggers are not predictions.

**Impact:** money. This is the risk that actually costs something.

**Mitigation:** the system emits no score it cannot defend. Every item names the rule that
fired and the numbers that made it fire. There is no composite conviction score. The word
"buy" appears nowhere. The LLM cannot introduce a claim, and the validator enforces that
mechanically rather than by prompt instruction. **Suppressors demote setups into binary
events.** The framing throughout is "worth your attention", never "worth your money".

**Residual:** irreducible. The system surfaces attention; the user makes every decision.

---

### R-7 — Repository growth from committed state
**Likelihood:** moderate. Up to 26 intraday runs per trading day, each potentially
committing a binary SQLite file.

**Impact:** a repository that becomes slow to clone within a year.

**Mitigation:** intraday runs commit **only when state actually changed**. If growth
becomes a problem, the documented escalation is to move `market.db` to a GitHub Actions
cache with a nightly committed snapshot.

---

### R-8 — Public repository leaks something personal
**Likelihood:** low, impact permanent.

**Impact:** anything committed to a public repo must be assumed permanently public,
regardless of later deletion.

**Mitigation:** `scripts/verify_secrets.py` scans the tracked tree for any value present in
the environment and fails the build on a hit. The no-personal-data rule is a Global
Constraint in the plan, so it is in front of every implementing agent on every task. The
watchlist holds **tickers only** — no sizes, no entries, no account state.

---

## 3. Open questions

### OQ-1 — Private Discord community ingestion
**Status:** dropped from v1 by decision D-2.
**Blocked on:** a legitimate ingestion path. Either (a) the server admins grant a bot
invite, or (b) an official export/API surface appears. **A self-bot is not an option and
will not be proposed** — it violates Discord's Terms of Service and risks the user's
personal account.
**Revisit when:** either condition changes. Until then this is not a v1 gap; it is a
feature with no legal implementation.

---

### OQ-2 — Free source for a US >$1B market-cap universe
**Status:** open. **This is the largest unverified assumption in the spec.**
**Blocked on:** Phase 0 verification. Candidates to test: Finnhub `/stock/symbol` plus
per-symbol market cap (expensive at 1 call per symbol against 60/min), Nasdaq's public
screener CSV, SEC company-facts bulk data.
**Fallback if none is free:** the screened watchlist seeds from index constituents
(S&P 500 + Nasdaq 100) instead of a true market-cap screen. This narrows discovery to
large caps only.
**Blocks:** Phase 7 Step 1 explicitly. Phases 0–6 are unaffected.

---

### OQ-3 — Do daily bot commits prevent the 60-day workflow auto-disable?
**Status:** open, assumed yes.
**Blocked on:** 60 days of live operation, or a definitive statement in GitHub's docs.
**Cheap insurance meanwhile:** a Healthchecks.io free dead-man's-switch pinged by each
digest run, which alerts on *absence* rather than on failure. Fold into Phase 3 if the
assumption cannot be confirmed cheaply.

---

### OQ-4 — Is the ceiling of 3 intraday alerts right?
**Status:** open by design; unanswerable before live data.
**Blocked on:** two weeks of real operation. The dropped-item count in the post-close
digest is the instrument that answers it.
**Note:** if the count is consistently high, the correct response is **tighter rules**, not
a higher ceiling. Raising the ceiling treats the symptom the project exists to cure.

---

### OQ-5 — Telegram rate limits at this volume
**Status:** assumed a non-issue at ~5 messages per day.
**Blocked on:** nothing — confirmed by `scripts/verify_quotas.py` in Phase 0.

---

### OQ-6 — Repository visibility
**Status:** decided public (D-1), for unlimited Actions minutes.
**Worth re-confirming with the user before the repo is created**, since it is effectively
irreversible for anything already pushed. The alternative is a private repo at 2,000
minutes/month, which fits the two daily digests comfortably but leaves little headroom for
intraday polling.

---

## 4. Provider verification log

Re-run `scripts/verify_quotas.py` and append a dated block here whenever it runs.

### 2026-08-05 — verified from vendor documentation and current sources (not yet from live API calls)

| Provider | Finding |
|---|---|
| Alpaca Basic | 200 historical requests/min; no daily cap; history since 2016; real-time is IEX only; SIP available historically with `end` ≥15 min in the past |
| Finnhub Free | 60 calls/min; company news 1 year; earnings calendar 1 month forward; WebSocket 50 symbols; personal use only |
| Marketaux Free | 100 requests/day |
| Alpha Vantage Free | 25 requests/day — **rejected as unusable** |
| Polygon / Massive Basic | 5 calls/min — **rejected** |
| Twelve Data Basic | 800 credits/day at 8 credits per time-series call — fallback only |
| Gemini Free | 1,500 requests/day, 10 RPM, 1M context, no card, no expiry; free-tier prompts may be used for training |
| Groq Free | llama-3.1-8b 14,400/day; llama-3.3-70b 1,000/day; no training on submitted data |
| GitHub Actions | Unlimited minutes on public repos; 2,000/month on Free private; 5-minute minimum cron interval; scheduled runs best-effort, may be delayed or dropped |
| Vercel Hobby cron | **1 run per day maximum, ±59 minute precision**; more frequent expressions fail at deploy |
| Cloudflare Workers Free | 100,000 requests/day; **10 ms CPU per invocation**; 5 cron triggers per account |
| Render Free | Sleeps after 15 min idle, 30–60 s cold start; keep-alive pingers actively blocked; free Postgres expires at 30 days |
| Fly.io / Railway | **No free tier for new accounts** |
| Oracle Always Free | 4 ARM OCPU / 24 GB / 200 GB block storage / 10 TB egress; frequent `out of host capacity`; reclaimed after 7 days below 20% utilisation |

**Everything above is vendor-documented, not yet confirmed against a live key.** Phase 0
exists to close that gap, and its output belongs in this section.
