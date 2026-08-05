# Market Agent — Specification

**Status:** approved design, not yet implemented
**Date:** 2026-08-05
**Scope of this document:** what the system is, what it is not, what it runs on, and what
"correct" means. The phased build order lives in [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md).
Risks and unresolved questions live in [RISKS.md](./RISKS.md).

---

## 1. Problem

Market hours cannot be watched continuously. Opportunities are therefore discovered after
they have already played out — a stock trades into a technically meaningful level, and the
fact is noticed days later.

The system exists to move discovery **before** the move rather than after it, and to do so
without requiring anyone to watch a screen.

The failure mode being escaped is *not* "insufficient information." It is the opposite:
market information is infinite and the user is drowning in it. **A dashboard that
data-dumps is a failed dashboard.** Sparse and organized beats comprehensive. Every design
decision below resolves ties in favour of less output.

### 1.1 Definition of success

1. A technically meaningful level is surfaced with enough lead time to act on it.
2. Alerts are rare enough that the arrival of one is itself information.
3. Nothing in the system requires continuous attention to function.

### 1.2 Definition of failure

1. More than the ceiling of alerts in a day (see §6.3) — noise defeats the purpose.
2. A recommendation stated with confidence that the evidence does not support.
3. A missed scan that goes unnoticed.

---

## 2. Non-goals

Named explicitly so that later sessions do not quietly re-admit them.

| Not building | Why |
|---|---|
| Order placement / broker integration | The agent recommends attention, never action. Deferred indefinitely. |
| Portfolio tracking (Interactive Brokers API) | Explicitly deferred by the user. TODO only. |
| Discord community ingestion | No bot invite is available for the server in question; automated reading with a personal account is a self-bot and violates Discord's Terms of Service. **Dropped from v1 entirely.** See RISKS.md OQ-1. |
| Multi-user, auth, user table | Single user. No login exists anywhere in the system. |
| Sub-15-minute price precision | Free consolidated data is 15 minutes delayed. See §4.3. |
| Options, futures, crypto, non-US exchanges | Out of scope. US equities and ETFs only. |
| Backtesting engine | The rule set is small and inspectable. A backtester is a second project. |
| Day-trading signals | Target holding periods are multi-day. Intraday precision is neither achievable on free data (§4.3) nor wanted. |
| Any paid service | Hard constraint. Any component requiring payment must be flagged, not adopted. |

---

## 3. Architecture

Scheduling and hosting are **separate concerns handled by different platforms**, because no
single free tier does both well.

```
                    ┌──────────────────────────────────┐
                    │  GitHub Actions (public repo)    │
                    │  unlimited free minutes          │
                    │                                  │
   market data ────►│  fetch → engine → rank → render  │────► Telegram Bot API
   news / events    │         (pure)                   │      (alerts + digests)
                    │            │                     │
                    └────────────┼─────────────────────┘
                                 │ commits JSON + SQLite
                                 ▼
                        ┌────────────────┐
                        │  the repo      │
                        └────────┬───────┘
                                 │ push triggers deploy
                                 ▼
                        ┌────────────────┐
                        │ Vercel (Hobby) │  static dashboard
                        └────────────────┘
```

Properties this buys: no server to maintain, no always-on process, no database service,
no secret ever on a command line, and a complete audit trail — every scan's inputs and
outputs are a commit.

### 3.1 Schedule

All crons are UTC. Code checks a US market calendar before doing work, so holidays and
DST shifts are handled in code rather than in cron expressions.

| Workflow | Cron (UTC) | US Eastern | Purpose |
|---|---|---|---|
| `premarket.yml` | `15 13 * * 1-5` | ~09:15 ET | Pre-market digest, arm levels for the session |
| `intraday.yml` | `*/15 14-20 * * 1-5` | 09:30–16:00 ET | Check armed levels, fire alerts |
| `postclose.yml` | `15 21 * * 1-5` | ~17:15 ET | Post-close digest, re-arm, rebuild dashboard |
| `weekly_screen.yml` | `15 6 * * 0` | Sunday | Regenerate the screened watchlist |

Scheduled runs are placed at **:15 and :45, never :00**. GitHub's scheduled-event queue
backs up at the top of every hour; off-peak minutes are serviced faster. Every workflow
also carries `workflow_dispatch` so a missed run can be triggered by hand without a push.

GitHub cron is best-effort with no timing SLA; 5–30 minute delays are documented and
common. This is tolerable here **only because** the underlying data is already 15 minutes
delayed (§4.3) and the target holding period is multi-day. See RISKS.md R-3.

### 3.2 Why these platforms

Each choice names what it was chosen over.

| Choice | Over | Reason |
|---|---|---|
| **GitHub Actions** (compute + scheduling) | Vercel Cron | Vercel Hobby cron is capped at **once per day** with **±59 minute** precision; expressions running more often fail at deploy time. Intraday polling is impossible on it. *(verified 2026-08-05)* |
| | Cloudflare Workers Cron | Free plan allows **10 ms CPU per invocation**. Computing indicators across 100+ symbols does not fit. *(verified 2026-08-05)* |
| | Oracle Cloud Always Free VM | Genuinely always-on and the only free option supporting a live WebSocket, but costs a sysadmin: OS patching, backups, an instance-creation capacity lottery, and reclamation after 7 days below 20% utilisation. Rejected on maintenance cost for a solo maintainer. |
| | Render / Fly.io / Railway | Fly.io and Railway no longer have free tiers. Render free services sleep after 15 minutes idle and Render actively blocks keep-alive pingers. *(verified 2026-08-05)* |
| | The user's Windows machine | Zero setup, but every scan is missed while the machine is off — reproducing the exact failure this project exists to fix. |
| **Public repo** | Private repo | Public repos get **unlimited** free Actions minutes; private gets 2,000/month, which intraday polling would consume most of. Nothing personal is ever committed (§9), so public costs nothing. Secrets live in encrypted Actions secrets, never in the tree. |
| **Vercel Hobby** (dashboard only) | GitHub Pages | Equivalent on price (both free). Chosen for the deploy-on-push workflow and frontend tooling the maintainer already knows. Hobby is non-commercial-use-only, which this is. |
| **SQLite committed in-repo** | Postgres (Supabase/Neon free tier) | No service to provision, no connection string to hold, no free-tier database expiry to be surprised by, and every state change is a reviewable diff. A single-user daily-bar store is megabytes. |
| **Python** | TypeScript | The indicator maths, the market-calendar handling and the data-provider SDKs are all better served in Python, and it matches the workspace's other analysis projects. |

---

## 4. Data sources

Every source below has a named free tier and a stated quota.

### 4.1 Verified this session (2026-08-05)

| Source | Free tier quota | Used for | Card required |
|---|---|---|---|
| **Alpaca Market Data — Basic** | 200 requests/min, no daily cap; history since 2016; multi-symbol bars endpoint; up to 10,000 data points per page | Daily and 15-minute OHLCV bars — the primary price source | No (data-only account) |
| **Finnhub — Free** | 60 calls/min | Company news (1 year), earnings calendar (1 month forward) | No |
| **Marketaux — Free** | 100 requests/day | Ticker-tagged news with sentiment; secondary//backup to Finnhub | No |
| **Google Gemini API — Free tier** | 1,500 requests/day, 10 RPM, 1M context, no expiry | Rendering prose only (§5.3) | No |
| **Telegram Bot API** | No published cap for this volume | Alert and digest delivery | No |
| **SEC EDGAR (RSS + JSON)** | Unlimited, no key; fair-use User-Agent required | 8-K events, Form 4 insider filings | No |
| **FRED** | Free API key | Macro release calendar | No |

**Rejected sources and why:** Alpha Vantage (25 requests/**day** — a single pass over the
watchlist exhausts it); Polygon/Massive free (5 calls/min); Twelve Data free (800
credits/day at 8 credits per time-series call ≈ 100 calls); `yfinance` (unofficial scraping
of Yahoo endpoints, breaks without warning — usable as an emergency fallback, never as the
primary source).

### 4.2 Quota arithmetic

The stated watchlist target is ~100 closely-watched names plus a periodic broad screen.

| Job | Calls | Against quota |
|---|---|---|
| Daily bars, 100 names, 250 bars each | ~25,000 data points ≈ **3 paged Alpaca calls** | 200/min — irrelevant |
| Intraday bars, 100 names, 1 poll | **1–3 Alpaca calls** | 200/min — irrelevant |
| Intraday polling, full session | 26 polls × 3 = **~78 calls/day** | irrelevant |
| Company news, 100 names | **100 Finnhub calls ≈ 2 min** | 60/min — fine twice daily |
| Weekly screen, ~2,000 names | grouped/paged bars, **tens of calls** | fine |

**Price data is not the constraint. News is.** Finnhub bills one call per symbol, so a
full news sweep costs ~2 minutes of wall time. Therefore: **intraday runs fetch bars only
and never touch news.** News is fetched exactly twice a day, in the two digest runs.

### 4.3 The delayed-data decision

Free real-time equity data means one of two things, and neither is real-time consolidated
tape:

- **Alpaca free real-time = IEX only.** IEX is a single venue carrying roughly 2.5% of US
  volume. Its prints are a poor proxy for "the price" and will miss level touches entirely.
- **Alpaca free SIP (all US exchanges, 100% of volume)** is available on historical
  endpoints provided the requested window ends **at least 15 minutes ago**.

**Decision: poll 15-minute-delayed SIP.** Correct prices, late; rather than wrong prices,
now.

**Stated consequence, to be repeated in the alert message itself:** a level touched at
10:00 ET surfaces at roughly 10:15–10:30 ET, and GitHub cron delay can extend that. This
system cannot support day trading and does not claim to; it targets multi-day holding
periods. Every alert carries the timestamp
of the bar it was computed from, not the time the message was sent.

---

## 5. The engine

### 5.1 Boundary

`engine/` is **pure**: no network, no filesystem, no clock reads, no framework. It takes
bars and configuration in, and returns triggers out. Everything else — fetching, storing,
sending, rendering — lives outside it.

This is the one part of the codebase under the workspace TDD rule: **its tests are written
first.** It is also the only part where a bug is silent, because a wrong indicator produces
a plausible-looking alert.

### 5.2 Triggers

Only a trigger can arm an alert or place a name on the watchlist. There are four, all
deterministic and all independently testable.

| Trigger | Definition | Rationale |
|---|---|---|
| **MA proximity / cross** | Price within *X*% of the 50/150/200-day simple moving average, or crossing it | The originating case: a mega-cap trading near its 150-day MA, noticed too late |
| **Range break** | Break of an *N*-day high or low, **or** of a level armed by hand ("GOOG past 350") | Makes the user's own worked example work end to end |
| **Volume anomaly** | Session volume vs 20-day average, and volume at the moment of a level touch | Separates a real break from a drift; computed from bars already fetched |
| **RSI(14)** | Overbought > 70, oversold < 30 | User-requested. Reported as context on every trigger, and able to fire alone at an extreme |

All thresholds (*X*, *N*, RSI bounds, volume multiple) are configuration, not constants in
code. Defaults are stated in config with a comment; none are tuned to any account.

### 5.3 Suppressors

A suppressor **can only demote or silence** a trigger. It can never create one.

| Suppressor | Effect |
|---|---|
| Earnings within *N* days | Demote; never present a level as a setup into an earnings print |
| Ex-dividend date imminent | Demote; the gap is mechanical, not technical |
| Macro release imminent (FRED calendar) | Demote index-correlated names |
| Market closed / half day | Suppress entirely |

Asymmetry is deliberate. A suppressor firing wrongly costs a missed setup. A suppressor
absent costs a confident recommendation into a known binary event. The second is worse.

### 5.4 Confidence, stated honestly

The system emits no score it cannot defend. Each surfaced item carries:

- **which rule fired**, by name;
- **the numbers that made it fire**, verbatim;
- **the timestamp of the bar** it was computed from;
- **any suppressor** in effect.

There is no composite "conviction score", because there is nothing to calibrate it against.
The word "buy" appears nowhere in any output.

---

## 6. Alerting contract

### 6.1 What triggers a message

- **Digests** — pre-market and post-close, on every trading day, unconditionally.
- **Intraday alerts** — only when an armed level is touched, or a trigger fires on a name
  in either watchlist, subject to the ceiling.

### 6.2 What a message contains

Every alert, without exception:

```
$TICKER — <rule that fired>
Level:      <the level>            Price: <price> (bar 14:15 ET, 15-min delayed)
Distance:   <% from level>
Volume:     <x.x>× 20-day average
RSI(14):    <value>
Note:       <one paragraph, see §7>
Caution:    <suppressor, if any>
Watchlist:  core | screened
```

The bar timestamp is mandatory and never replaced by the send time.

### 6.3 The ceiling

**2 digests + a maximum of 3 intraday alerts per day.** Enforced in code, not by
convention.

When a 4th trigger qualifies, it is **dropped, logged, and reported in the post-close
digest** — never sent. This forces the ranking to genuinely rank rather than degrade into
a firehose on volatile days. The count resets at the pre-market run.

Ranking, when more than three qualify: manually-armed levels first (the user asked for
those explicitly), then core watchlist over screened, then by strength of the numbers
(distance to level, then volume multiple).

---

## 7. The LLM boundary

**Gemini Flash decides nothing.** It is a readability layer and nothing else.

The pipeline:

1. The engine produces an **evidence packet**: every number already computed, every rule
   already fired by name, every headline already fetched with its URL and timestamp.
2. Gemini receives that packet and a single instruction — phrase it for a human. It is
   given no tools, no search, no market access, and no latitude to add a fact.
3. A **validator** inspects the output and rejects it if any numeric token, ticker, or
   date appears that was not in the input packet.
4. On rejection, quota exhaustion, or API failure, a **deterministic template** ships
   instead. The alert is always delivered; only its prose degrades.

Chosen over Groq (weaker models for coherent technical prose, though it does not train on
submitted data) and over a paid model (violates the free-tier constraint). The
provider sits behind a thin interface so swapping is a config change.

**Accepted trade-off:** Google may use free-tier prompts for model training. Acceptable
here because prompts contain only public market data — no positions, no account details,
no personal facts (§9).

---

## 8. Watchlists

Two lists, always distinguished in output so their provenance is never ambiguous.

| List | Source | Maintenance |
|---|---|---|
| **Core** — `config/watchlist_core.yml` | Hand-edited by the user. Tickers only. | Manual, whenever the user wants |
| **Screened** — `data/watchlist_screened.json` | Regenerated weekly from the >$1B-market-cap US universe against the same rule set in §5.2 | Automatic; each entry records the rule that admitted it and the date |

Both are scanned daily and identically. The screened list is capped at a fixed size so it
cannot grow into noise; when full, entries are replaced by strength, not appended.

Market-cap floor: **$1B**, in configuration. It is a general universe-quality filter, not a
personal preference, and is documented as configurable.

---

## 9. What must never be committed

The user profile is design context only. **No account details, position sizes, risk
tolerance, community memberships, or personal facts appear in any file in this
repository** — in code, config, comments, commit messages, or documentation. The
repository is public; this is enforced by review, and the assumption should be that
anything committed is permanently public.

Secrets — Alpaca keys, Finnhub key, Marketaux key, Gemini key, Telegram bot token and chat
ID — live in **GitHub Actions encrypted secrets** and in a gitignored `.env` for local
runs. Per the workspace working rules, **a secret is never passed on a command line**,
because session transcripts are permanent on-disk logs.

---

## 10. Dashboard

Static. Reads committed JSON. No backend, no database, no auth, no client-side fetching of
market data.

**Information hierarchy** — this order, top to bottom, is the design:

1. **Today's setups** — what fired, at what level, with the reasoning. The reason to open
   the page.
2. **Relevant news** — filtered to watchlist names and macro. Not a news feed.
3. **Upcoming events** — earnings and macro releases, next 10 days.
4. **Armed levels** — what is currently being watched for, so the user can mirror them
   into TradingView by hand.

Each block has a **hard item cap enforced when the JSON is generated**, not when it is
rendered. The dashboard cannot data-dump on a busy day because the data file it reads is
physically incapable of containing a dump. This is the single most important property of
the dashboard and is not negotiable in implementation.

Levels are displayed in a form that is trivially copied into TradingView on a phone.

---

## 11. Repository layout

```
market project/
├── docs/
│   ├── SPEC.md                     ← this document
│   ├── IMPLEMENTATION_PLAN.md      ← phased build order
│   └── RISKS.md                    ← risks + open questions register
├── .github/workflows/
│   ├── premarket.yml
│   ├── intraday.yml
│   ├── postclose.yml
│   └── weekly_screen.yml
├── config/
│   ├── watchlist_core.yml          ← hand-edited tickers
│   ├── rules.yml                   ← thresholds, caps, ceiling
│   └── .env.example                ← key names only, never values
├── engine/                         ← PURE. no I/O. tests written first.
│   ├── indicators.py               ← MA, RSI, volume ratio
│   ├── triggers.py                 ← the four rules
│   ├── suppressors.py              ← earnings/dividend/macro/calendar
│   └── ranking.py                  ← ceiling enforcement + ordering
├── sources/                        ← all network I/O lives here, nowhere else
│   ├── alpaca.py
│   ├── finnhub.py
│   ├── marketaux.py
│   ├── edgar.py
│   └── fred.py
├── render/
│   ├── evidence.py                 ← builds the packet
│   ├── gemini.py                   ← prose only
│   ├── validator.py                ← rejects invented numbers
│   └── template.py                 ← deterministic fallback
├── delivery/
│   └── telegram.py
├── scripts/                        ← every verification script, committed
│   ├── verify_quotas.py
│   ├── verify_secrets.py
│   └── verify_pipeline.py
├── data/                           ← committed state
│   ├── market.db                   ← SQLite: bars, armed levels, sent ledger
│   ├── watchlist_screened.json
│   └── dashboard/*.json            ← what Vercel reads
├── dashboard/                      ← static site deployed by Vercel
└── tests/
```

The `engine/ | sources/ | render/ | delivery/` split exists so that the only code that can
be wrong-but-silent (`engine/`) is also the only code that is trivially testable — no
mocking, no network, no clock.

---

## 12. Verification

Per the workspace working rules, **every script worth running once against a live system is
committed to `scripts/`**, so it can be re-run after every change to that subsystem.

| Script | Confirms |
|---|---|
| `verify_quotas.py` | Every provider's free tier still exists and still returns data at the documented limit |
| `verify_secrets.py` | Every required secret is present, and no secret appears in the working tree |
| `verify_pipeline.py` | End-to-end dry run: fetch → engine → rank → render → **Telegram test chat**, no live alert sent |

A phase is not done because code exists. It is done when the observable check in
IMPLEMENTATION_PLAN.md produces its stated output.

---

## 13. Verified vs assumed

Distinguished deliberately, because a plan built on a free tier that no longer exists is
worse than no plan.

**Verified against current sources on 2026-08-05:** Alpaca Basic limits and the IEX-vs-SIP
distinction; Finnhub 60/min free tier; Marketaux 100/day; Gemini free tier 1,500/day at 10
RPM; Groq free-tier limits; Vercel Hobby cron restrictions (once daily, ±59 min); Cloudflare
Workers free 10 ms CPU; GitHub Actions free minutes and public-repo unlimited minutes;
GitHub cron best-effort delay behaviour; Render/Fly/Railway free-tier status; Oracle Always
Free specifications and idle-reclaim policy; Alpha Vantage 25/day.

**Assumed, not verified — must be confirmed during Phase 0:**

- Telegram Bot API has no rate limit that matters at ~5 messages/day.
- SEC EDGAR fair-use policy permits this request volume with a proper User-Agent.
- A free source exists for a US >$1B market-cap universe listing suitable for the weekly
  screen. **If none does, the screened watchlist degrades to an index-constituent seed
  list.** This is the largest unverified assumption in the document — see RISKS.md OQ-2.
- Committing SQLite on every scheduled run stays within reasonable repository growth over
  a year.
- GitHub's 60-day inactivity auto-disable of scheduled workflows on public repositories is
  averted by the daily commits the workflows themselves make. **To be confirmed
  empirically**, not assumed — see RISKS.md R-4.
