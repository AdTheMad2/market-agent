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

**Phase 2 finding (2026-08-06) — the news fallback covers far less than assumed.**
Marketaux's free tier returns 3 articles *per response*, not per symbol, and caps at 100
requests/day. A 50-symbol batch therefore carries news for at most 3 of those names and
the other 47 come back empty, indistinguishable from "no news". `sources/marketaux.py`
documents this at the top of the module. It is a genuine fallback for Finnhub only in the
sense that something arrives — not that coverage is preserved. Per-symbol requests would
restore coverage and exceed the daily cap at ~100 names.

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

**Known gap, introduced in Phase 2:** `scan_files_for_values` skips files it cannot decode
as text, and Phase 2 committed exactly such a file — `data/market.db`. The scanner's
guarantee is therefore "every tracked *text* file", not "every tracked file". The residual
risk is small (the schema holds prices, tickers, and delivery records, and every writer is
in `sources/store.py`), but the guarantee is narrower than it reads and a future table
holding a free-text field would sit in the blind spot.

---

### R-9 — Python 3.14 on the host, ahead of the scientific stack
**Likelihood:** already the case. The host runs **Python 3.14.0**; the plan assumed 3.12.

**Impact:** none in Phase 0 — it needs only `requests` and `pyyaml`, both of which
installed cleanly. The exposure is Phase 2, where `pandas-market-calendars` (and
transitively `pandas`) are needed for the trading-calendar check. Wheels for new CPython
releases can lag by months, and building from source on Windows is a bad afternoon.

**Mitigation:** `engine/` is deliberately pure Python with no pandas dependency, so the
indicator maths is unaffected either way. If `pandas-market-calendars` will not install,
the fallback is `exchange_calendars`, and failing that a hand-maintained US market holiday
list in `config/` — the data is a dozen dates a year and changes annually. Confirm the
install at the **start** of Phase 2, not the middle.

**Alternative if it bites:** pin the GitHub Actions runner to `python-version: "3.12"`,
which decouples CI from the local host entirely. The local machine only needs to run the
verification scripts.

**Resolved 2026-08-06.** The alternative was taken: all three workflows pin
`python-version: "3.12"` while the local venv stays on 3.14. Both are green.

---

### R-13 — GitHub's free scheduled queue runs late, by hours
**Likelihood:** already the case, measured.

**Impact:** the cron time in a workflow file is the earliest a run may start, not when it
starts. Measured on this project:

| Workflow | Cron | Actually ran | Lag |
|---|---|---|---|
| post-close | `15 21 * * 1-5` | 2026-08-07 01:03 UTC | 3h48m |
| pre-market | `15 13 * * 1-5` | 2026-08-07 14:19 UTC | 1h04m |
| intraday | `*/15 14-20 * * 1-5` | 14:54, 15:57, 17:00, 18:02, 19:16 UTC | ~1h between polls, not 15m |

The intraday row is the one that matters. A `*/15` cron delivered five polls in a
six-hour session instead of twenty-five. **The ceiling is unaffected** — it counts alerts,
not polls — but the phrase "within one poll of the touch" describes a poll that is
roughly hourly in practice, so a level touched just after a poll waits up to an hour on
top of the 15-minute SIP delay.

**Impact on the product, stated plainly:** this is a system that tells the user what
happened, up to an hour or so after it happened. It was never a day-trading tool
(SPEC.md §1), and this makes the floor concrete rather than changing the kind of thing it
is. The post-close digest arriving at 21:03 ET instead of 17:15 ET is the more annoying
half.

**Mitigation:** none available on the free tier, and the plan's existing `:15`/`:45` rule
is already the recommended mitigation — the lag is queue depth on GitHub's shared
scheduler, not something a workflow can opt out of. What *is* actionable: never write a
delivery-time promise into the prose. Every alert already states the bar timestamp it was
computed from rather than the send time (SPEC.md §5), which is exactly the guarantee that
survives this.

**Watch for:** lag long enough that a post-close run crosses midnight UTC, which would
file the digest under the following day and make `sent_count_today` see a fresh day. The
2026-08-07 01:03 UTC run *already did this* — it recorded the 2026-08-06 session's digest
against day `2026-08-07`. Harmless for a digest, which does not consume the intraday
ceiling; it would not be harmless for an intraday alert, and intraday runs stop at
20:00 UTC so they cannot reach midnight even with this lag.

---

### R-14 — Gemini's free tier: bursts are refused, and thinking eats the answer
**Likelihood:** already the case, measured 2026-08-07 building Phase 5.

**Impact:** two distinct failures that both present as "the model said nothing".

**Thinking tokens bill against `maxOutputTokens`.** A real evidence packet costs
**842 thought tokens**. At the 512 the client shipped with, the model returned HTTP 200
and a sentence cut off mid-timestamp — `"As of 2026-07-29T04:00"`. The validator accepted
it, correctly: truncation removes content rather than inventing it, so nothing downstream
could tell a fragment from a finished thought. There is no way to switch thinking off;
`thinkingConfig.thinkingBudget: 0` is an HTTP 400 on `gemini-3.6-flash`.

**Bursts are rate-limited.** A digest asks for up to `dashboard.max_setups` (8) phrasings
back to back and most were answered with 429.

**Mitigation, both in `render/gemini.py`:** the budget is 2048 and `finishReason` must be
`STOP`, so a truncated reply is discarded rather than delivered; calls are spaced
`PACE_SECONDS = 4.0`, which costs a job already hours behind GitHub's scheduler (R-13)
nothing worth counting. A 429 that still gets through degrades that one entry to the
template and is not retried — this provider was already found intermittently unavailable
in Phase 0, and the design treats that as the normal condition rather than as an incident.

**What this does not threaten:** the alert. Every failure here costs prose and nothing
else. `render/__init__.py` ships the Phase 3 template on any of them, which is why
Phase 5 was built as a layer over a working path rather than as a replacement for it.

**Watch for:** the day the free tier's daily cap, rather than its per-minute rate, is the
binding limit. Current usage is roughly 8 calls per digest × 2 digests + up to 3 intraday
= ~19 a day. A larger watchlist scales the digest half linearly.

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
**Status:** **closed 2026-08-06.** Public, at
`github.com/AdTheMad2/market-agent`, for unlimited Actions minutes. Confirmed with the
user before creation, since it is effectively irreversible for anything already pushed.
The alternative — a private repo at 2,000 minutes/month — fits the two daily digests
comfortably but leaves little headroom for intraday polling.

The standing consequence outlives the decision: **everything committed here is
permanently public**, including `data/market.db`, which a bot rewrites several times a
day. Nothing personal may enter the repository, and `config/watchlist_core.yml` carries
that warning at the top of the file because it is the one a human hand-edits.

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

### 2026-08-05 — `scripts/verify_quotas.py` written, run with no keys

All seven providers reported `SKIP` (no key set), which is the expected result and confirms
the script degrades gracefully rather than crashing while keys are still being collected.
**No live verification has happened yet.** The next block in this section must be a run
where nothing is skipped.

`scripts/verify_secrets.py` exits 1 (nine required names unset, one optional unset) with an
empty leak report across 25 tracked files. Its leak detector was confirmed working
end-to-end against a planted fake value: exit 2, correct file named, value absent from the
report.

### 2026-08-05 12:37 UTC — first live run, all keys present. **Phase 0 gate passed.**

`scripts/verify_secrets.py` exits 0: all nine required names set, `TELEGRAM_TEST_CHAT_ID`
unset and falling back, no secret value found in any of 33 tracked files.

`scripts/verify_quotas.py` exits 0 — **7 OK, 0 failed, 0 skipped.**

```
[  OK  ] Alpaca (bars, SIP)   HTTP 200  AAPL 2026-08-04T04:00:00Z close=309.38
                              X-Ratelimit-Limit: 200, Remaining: 199
[  OK  ] Finnhub (news)       HTTP 200  249 articles for AAPL
                              X-Ratelimit-Limit: 60, Remaining: 59
[  OK  ] Marketaux (news)     HTTP 200  meta.found=126259
                              x-ratelimit-limit: 30, Remaining: 29
[  OK  ] Gemini (prose)       HTTP 200  gemini-3.6-flash replied 'ok'; tokens=89
                              no rate-limit headers
[  OK  ] FRED (macro)         HTTP 200  DFF: Federal Funds Effective Rate
[  OK  ] SEC EDGAR (filings)  HTTP 200  Apple Inc.: latest 10-Q on 2026-07-31
[  OK  ] Telegram (delivery)  HTTP 200  getMe authenticated
```

**Verified, not assumed:** free-tier SIP bars are genuinely available to a data-only
Alpaca account — the §4 assumption that free real-time is IEX-only but free *delayed* data
is full SIP holds. Finnhub advertises 60/min and returns exactly that header. Marketaux
returns 30 as its rate-limit header, distinct from the documented 100/day request cap;
these measure different windows and both need respecting.

**Two spec-level findings, both from this run:**

1. **`gemini-2.5-flash` is dead for new keys.** It returns HTTP 404 — "no longer available
   to new users" — while *still being listed by* `ListModels`. The model listing is not a
   reliable statement of what a given key may call; only a live `generateContent` is.
   Switched to `gemini-3.6-flash`. `gemini-3.5-flash` returned 503 (high demand) and
   `gemini-2.0-flash` returned 429 (quota) on the same key at the same moment, so the
   choice of model is also a choice of availability, not only of quality.
2. **Gemini 3.x bills thinking tokens against `maxOutputTokens`.** A 256-token budget
   yielded 74 thought tokens and 1 output token; the original 16-token budget returned
   HTTP 200 with an empty candidate and no text at all. Any future prose call must budget
   for thinking or it will fail silently as a success. `render/gemini.py` must treat an
   empty candidate as a failure and fall back to the deterministic template (Phase 5).

Neither was a provider or quota problem — both were probe bugs that a "does it 200?" check
would have passed. Recorded because the same two mistakes are latent in Phase 5.

Also fixed: the Alpaca probe sent no `start`, and Alpaca answers HTTP 200 with an empty
`bars` list rather than defaulting to a window. `limit` was removed too — it truncates from
the *start* of the range, so the probe was checking the oldest bar while claiming freshness.

### 2026-08-05 — Telegram delivery verified, and what the gate had missed

`scripts/verify_delivery.py` exits 0: HTTP 200, `message_id=3`, delivered to the private
chat. **This is the check that closes the Telegram row in the deploy checklist.**

It failed the first time, and the failure is the point. `verify_quotas.py` had been
reporting Telegram **OK** throughout, because it calls `getMe` — which authenticates the
*token* and says nothing about the destination. `TELEGRAM_CHAT_ID` had been filled in with
the bot's own ID (copied from a `getMe` response, and a plausible-looking 10-digit number),
so every alert this project ever sent would have gone nowhere while the gate stayed green.
The real send returned `403 Forbidden: the bot can't send messages to the bot`.

Root cause of the wrong ID: `getUpdates` returned zero updates because the bot had never
been messaged, so there was no chat ID to read and the nearest available number was used.
Telegram will not disclose a chat ID until the user initiates contact.

**Rule this establishes, and R-1's real shape:** a probe must exercise the property being
relied on, not a property adjacent to it. Authentication is not delivery, a 200 is not
data, and a listed model is not a callable one. All three appeared in a single day:

| Probe | Reported | Actually true |
|---|---|---|
| Telegram `getMe` | OK | Messages went to the bot itself |
| Gemini `generateContent` | 200 | Empty candidate, no text |
| Alpaca `/bars` | 200 | Zero bars |

`verify_delivery.py` now names this specific 403 and explains it, and reconfigures stdout
to UTF-8 — a Telegram display name is arbitrary Unicode and the cp1252 console raises
`UnicodeEncodeError` rather than degrading (see also R-9's encoding note).

`TELEGRAM_TEST_CHAT_ID` remains unset, so the test message went to the live alert chat.
Acceptable now; it must be set before Phase 4's dry runs, or a dry run will be
indistinguishable from a real alert.
