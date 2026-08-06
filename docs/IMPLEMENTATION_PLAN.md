# Market Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development`
> (recommended) or `superpowers:executing-plans` to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **This plan is not authorised to execute.** Per the project kickoff, execution begins in a
> later session on the user's explicit go-ahead. Nothing in `engine/`, `sources/`, `render/`,
> `delivery/`, `scripts/` or `dashboard/` exists yet, by design.

**Goal:** A single-user market agent that runs pre-market and post-close scans, arms
price-level alerts during the session, delivers them to Telegram under a hard daily
ceiling, and renders a sparse dashboard — entirely on free tiers.

**Architecture:** GitHub Actions on a public repo runs four scheduled Python workflows.
A pure `engine/` computes triggers from bars; `sources/` holds all network I/O; `render/`
turns computed evidence into prose via Gemini with a validator and a deterministic
fallback; `delivery/` sends Telegram messages. State is SQLite committed in-repo, and
Vercel serves a static dashboard built from committed JSON.

**Tech Stack:** Python 3.12, `pandas`, `pandas-market-calendars`, `requests`, `pytest`,
SQLite (stdlib `sqlite3`), GitHub Actions, Vercel Hobby, Telegram Bot API, Gemini Flash.

## Global Constraints

Every task's requirements implicitly include this section. Values are copied verbatim from
[SPEC.md](./SPEC.md).

- **Free tier only.** Any component requiring payment must be flagged and an alternative
  proposed — never adopted silently.
- **No personal data in the repository, ever.** No account details, position sizes, risk
  tolerance, community memberships, or personal facts in code, config, comments, commit
  messages, or documentation. The repository is public.
- **Secrets never touch a command line.** Session transcripts are permanent on-disk logs.
  Secrets live in GitHub Actions encrypted secrets and in a gitignored `.env` locally.
- **`engine/` is pure.** No network, no filesystem, no clock reads, no framework imports.
  Its tests are written first — this is the workspace TDD rule and it applies here and
  nowhere else in this codebase.
- **Alert ceiling: 2 digests + maximum 3 intraday alerts per day.** Enforced in code.
- **Data is 15-minute-delayed SIP.** Every alert states the timestamp of the bar it was
  computed from, never the send time.
- **The word "buy" appears in no output.**
- **Every verification script is committed to `scripts/`.** If it was worth running once
  against a live system, it is worth re-running after every change to that subsystem.
- **Cron minutes are `:15` or `:45`, never `:00`.** GitHub's scheduled queue backs up at
  the top of the hour.
- **Market-cap floor $1B and all rule thresholds live in `config/rules.yml`**, never as
  constants in code.

---

## Phase overview

Each phase is independently useful and independently verifiable. A phase is done when its
observable check produces its stated output — not when its code exists.

| Phase | Delivers | Observable check |
|---|---|---|
| 0 | Repo, secrets, live quota verification | `verify_quotas.py` prints a live row from every provider; `verify_secrets.py` exits 0 and finds no secret in the tree |
| 1 | Pure engine + tests | `pytest` green; `python -m engine.cli fixtures/bars_goog_ma150.json` prints the 150-day MA trigger |
| 2 | Data layer + SQLite state | `market.db` holds 250 daily bars for every core-watchlist name; a second run adds zero rows |
| 3 | **Post-close digest to Telegram** | A real digest arrives on the user's phone at ~17:15 ET on a trading day |
| 4 | Intraday armed levels + ceiling | An armed level fires within one poll of being touched; a 4th trigger is dropped and appears in the post-close digest |
| 5 | Gemini prose + validator | Alerts read as prose; an injected fabricated number is rejected and the template ships instead |
| 6 | Dashboard on Vercel | Live URL renders four blocks, item caps hold on a deliberately overloaded fixture |
| 7 | Weekly screened watchlist | Screened list regenerates on Sunday with the admitting rule recorded per entry |

**Phase 3 is the first phase with user-visible value.** If the project stalls after Phase 3,
the user still has a working daily digest. That ordering is deliberate.

---

## Phase 0 — Foundations and live verification

Nothing is built on a free tier that has not been confirmed to exist *today*.

### Task 0.1: Repository and Python skeleton

**Files:**
- Create: `.gitignore`, `pyproject.toml`, `README.md`, `config/.env.example`
- Create: `config/rules.yml`, `config/watchlist_core.yml`

**Interfaces:**
- Produces: `config/rules.yml` — the single source of every threshold. Keys:
  `ma_periods: [50, 150, 200]`, `ma_proximity_pct`, `range_break_days`,
  `volume_multiple`, `rsi_period: 14`, `rsi_overbought: 70`, `rsi_oversold: 30`,
  `earnings_suppress_days`, `intraday_alert_ceiling: 3`, `screened_list_max`,
  `market_cap_floor_usd: 1_000_000_000`.

- [ ] **Step 1:** `git init`; create the directory tree from SPEC.md §11 with `.gitkeep`
      files. Create the venv: `python -m venv venv` (workspace convention — per-project venv).
- [ ] **Step 2:** Write `.gitignore` containing at minimum `.env`, `venv/`, `__pycache__/`,
      `*.pyc`. Verify `.env` is ignored before any key is ever created.
- [ ] **Step 3:** Write `config/.env.example` listing **key names only, never values**:
      `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`, `FINNHUB_API_KEY`,
      `MARKETAUX_API_KEY`, `GEMINI_API_KEY`, `FRED_API_KEY`, `TELEGRAM_BOT_TOKEN`,
      `TELEGRAM_CHAT_ID`.
- [ ] **Step 4:** Write `config/rules.yml` with the keys above and a one-line comment per
      threshold explaining what it controls. No value tuned to any account.
- [ ] **Step 5:** Commit.

### Task 0.2: `verify_secrets.py`

**Files:**
- Create: `scripts/verify_secrets.py`

**Interfaces:**
- Produces: `verify_secrets() -> int` — exit code 0 when every name in `.env.example` is
  present in the environment **and** no value from `.env` appears anywhere in the tracked
  working tree.

- [ ] **Step 1:** Write the failing test: given a temp tree containing a file with a fake
      secret value, `scan_tree_for_values([...])` returns that file path.
- [ ] **Step 2:** Run it; expect FAIL (`scan_tree_for_values` not defined).
- [ ] **Step 3:** Implement: read `.env.example` for names, read env for values, walk
      `git ls-files` output, report any file containing any value. Never print the value
      itself — print only the file path and the variable name that leaked.
- [ ] **Step 4:** Run; expect PASS.
- [ ] **Step 5:** Commit.

### Task 0.3: `verify_quotas.py` — the phase gate

**Files:**
- Create: `scripts/verify_quotas.py`

**Interfaces:**
- Produces: a table on stdout, one row per provider: name, endpoint hit, HTTP status,
  one field of live data, and the rate-limit headers returned.

- [ ] **Step 1:** Implement one probe per provider — Alpaca daily bars for `AAPL`,
      Finnhub company news for `AAPL`, Marketaux `/news/all` for `AAPL`, Gemini a
      three-token completion, FRED a series metadata call, SEC EDGAR a submissions fetch
      with a descriptive User-Agent, Telegram `getMe`.
- [ ] **Step 2:** Print each provider's returned rate-limit headers verbatim. **Do not
      hardcode the documented quota** — the point of this script is to detect the day a
      documented quota stops being true.
- [ ] **Step 3:** Run it. **This is the Phase 0 observable check.**
- [ ] **Step 4:** Record the output as a dated block in `docs/RISKS.md` under R-1.
- [ ] **Step 5:** Commit.

**Phase 0 is done when:** `verify_quotas.py` shows a live 200 and real data from every
provider in SPEC.md §4.1, and `verify_secrets.py` exits 0 with an empty leak report.

> **If a provider fails here, stop and revise SPEC.md §4 before writing Phase 1.** A plan
> built on a tier that no longer exists is worse than no plan.

---

## Phase 1 — The engine (pure, test-first)

This is the only phase under the TDD rule, and the only code where a bug is silent: a wrong
indicator produces a plausible-looking alert that nobody questions.

### Task 1.1: Indicators

**Files:**
- Create: `engine/indicators.py`, `tests/engine/test_indicators.py`
- Create: `tests/fixtures/bars_goog.json` (250 daily bars, committed, real Alpaca output)

**Interfaces:**
- Produces:
  - `sma(closes: list[float], period: int) -> float | None` — `None` when fewer than
    `period` values.
  - `rsi(closes: list[float], period: int = 14) -> float | None` — Wilder's smoothing.
  - `volume_ratio(volumes: list[int], period: int = 20) -> float | None` — latest volume
    divided by the mean of the prior `period` volumes, excluding the latest.

- [x] **Step 1: Write the failing tests.** Hand-computed expected values, not values
      captured from the implementation — a test that asserts what the code already does
      proves nothing.

```python
def test_sma_returns_none_when_insufficient_data():
    assert sma([1.0, 2.0], period=5) is None

def test_sma_simple_case():
    assert sma([1.0, 2.0, 3.0, 4.0, 5.0], period=5) == 3.0

def test_rsi_all_gains_is_100():
    closes = [float(i) for i in range(1, 20)]
    assert rsi(closes, period=14) == 100.0

def test_volume_ratio_excludes_latest_from_baseline():
    # baseline mean of twenty 100s is 100; latest is 250
    assert volume_ratio([100] * 20 + [250], period=20) == 2.5
```

- [x] **Step 2:** Run `pytest tests/engine/test_indicators.py -v`. Expect FAIL —
      `ImportError: cannot import name 'sma'`.
- [x] **Step 3:** Implement the three functions. No pandas inside `engine/` — plain
      Python keeps the module pure and the failure modes obvious.
- [x] **Step 4:** Run; expect PASS.
- [x] **Step 5:** Commit `test: indicators` and `feat: sma, rsi, volume_ratio`.

### Task 1.2: Triggers

**Files:**
- Create: `engine/triggers.py`, `tests/engine/test_triggers.py`

**Interfaces:**
- Consumes: `engine.indicators.sma`, `rsi`, `volume_ratio`.
- Produces:
  - `@dataclass(frozen=True) Trigger` with fields `ticker: str`, `rule: str`,
    `level: float`, `price: float`, `distance_pct: float`, `volume_ratio: float | None`,
    `rsi: float | None`, `bar_timestamp: str`, `watchlist: str`.
  - `evaluate(ticker: str, bars: list[Bar], rules: Rules, armed: list[float]) -> list[Trigger]`
  - `rule` is one of the exact strings `"ma_proximity"`, `"ma_cross"`, `"range_break"`,
    `"armed_level"`, `"rsi_extreme"`. Later tasks match on these strings.

- [x] **Step 1: Write the failing tests**, one per rule plus the negative case for each.

```python
def test_ma_proximity_fires_within_threshold(rules):
    bars = bars_ending_at(price=100.4, sma_150=100.0)
    triggers = evaluate("GOOG", bars, rules, armed=[])
    assert [t.rule for t in triggers] == ["ma_proximity"]

def test_ma_proximity_silent_outside_threshold(rules):
    bars = bars_ending_at(price=110.0, sma_150=100.0)
    assert evaluate("GOOG", bars, rules, armed=[]) == []

def test_armed_level_fires_when_crossed(rules):
    bars = bars_ending_at(price=351.0, prev_close=349.0)
    triggers = evaluate("GOOG", bars, rules, armed=[350.0])
    assert triggers[0].rule == "armed_level"
    assert triggers[0].level == 350.0

def test_trigger_carries_bar_timestamp_not_now(rules):
    bars = bars_ending_at(price=351.0, prev_close=349.0, ts="2026-08-05T14:15:00-04:00")
    t = evaluate("GOOG", bars, rules, armed=[350.0])[0]
    assert t.bar_timestamp == "2026-08-05T14:15:00-04:00"
```

- [x] **Step 2:** Run; expect FAIL.
- [x] **Step 3:** Implement `evaluate`. Every threshold read from `rules`, none inline.
- [x] **Step 4:** Run; expect PASS.
- [x] **Step 5:** Commit.

### Task 1.3: Suppressors

**Files:**
- Create: `engine/suppressors.py`, `tests/engine/test_suppressors.py`

**Interfaces:**
- Produces: `apply(triggers: list[Trigger], context: SuppressionContext) -> list[Suppressed]`
  where `Suppressed` wraps a `Trigger` with `demoted: bool` and `reason: str | None`.
  `SuppressionContext` carries `earnings_dates: dict[str, date]`,
  `ex_dividend_dates: dict[str, date]`, `macro_events: list[date]`, `today: date`.

- [x] **Step 1: Write the failing tests.** The critical one is the asymmetry:

```python
def test_suppressor_never_creates_a_trigger(context):
    assert apply([], context) == []

def test_earnings_within_window_demotes_but_does_not_remove(context_with_earnings_in_3d):
    result = apply([goog_trigger], context_with_earnings_in_3d)
    assert result[0].demoted is True
    assert "earnings" in result[0].reason
    assert result[0].trigger == goog_trigger  # still present, still visible
```

- [x] **Step 2:** Run; expect FAIL.
- [x] **Step 3:** Implement. A suppressor may set `demoted` and `reason`. It has no code
      path that appends to the list.
- [x] **Step 4:** Run; expect PASS.
- [x] **Step 5:** Commit.

### Task 1.4: Ranking and ceiling

**Files:**
- Create: `engine/ranking.py`, `tests/engine/test_ranking.py`

**Interfaces:**
- Produces: `rank(suppressed: list[Suppressed], already_sent_today: int, ceiling: int) -> tuple[list[Suppressed], list[Suppressed]]`
  returning `(to_send, dropped)`. Order: `armed_level` first, then core watchlist before
  screened, then by `distance_pct` ascending, then by `volume_ratio` descending. Demoted
  items sort last within their group.

- [x] **Step 1: Write the failing tests.**

```python
def test_ceiling_is_absolute(five_triggers):
    to_send, dropped = rank(five_triggers, already_sent_today=0, ceiling=3)
    assert len(to_send) == 3
    assert len(dropped) == 2

def test_ceiling_accounts_for_alerts_already_sent(three_triggers):
    to_send, dropped = rank(three_triggers, already_sent_today=2, ceiling=3)
    assert len(to_send) == 1
    assert len(dropped) == 2

def test_armed_levels_outrank_everything(mixed_triggers):
    to_send, _ = rank(mixed_triggers, already_sent_today=0, ceiling=1)
    assert to_send[0].trigger.rule == "armed_level"
```

- [x] **Step 2:** Run; expect FAIL.
- [x] **Step 3:** Implement. Dropped items are returned, never discarded — Phase 3's
      digest reports them.
- [x] **Step 4:** Run; expect PASS.
- [x] **Step 5:** Commit.

### Task 1.5: Engine CLI

**Files:**
- Create: `engine/cli.py`

- [x] **Step 1:** Implement `python -m engine.cli <bars.json>` — loads a fixture, runs
      `evaluate` → `apply` → `rank`, prints the result as a table. Reads a file, so it
      lives at the edge of `engine/`; it imports nothing new.
- [x] **Step 2:** Run against `tests/fixtures/bars_goog_ma150.json`. **This is the Phase 1
      observable check** — it must print the 150-day MA trigger. (`bars_goog.json`'s last
      close sits 11.13% above its 150-day SMA, outside `ma_proximity_pct: 1.0`, so it fires
      nothing; the `_ma150` fixture is the same real bars truncated to the first session
      where price sits within the threshold.)
- [x] **Step 3:** Commit.

**Phase 1 is done when:** `pytest` is green and the CLI prints a real trigger from the
committed `tests/fixtures/bars_goog_ma150.json` fixture, with no network access anywhere
in the run.

---

## Phase 2 — Data layer and state

### Task 2.1: Alpaca bars source

**Files:**
- Create: `sources/alpaca.py`, `tests/sources/test_alpaca.py`

**Interfaces:**
- Produces:
  - `daily_bars(tickers: list[str], days: int) -> dict[str, list[Bar]]`
  - `intraday_bars(tickers: list[str], minutes: int = 15) -> dict[str, list[Bar]]`
  - Both request `feed=sip` with `end` set to **`now - 16 minutes`**. The extra minute is
    slack against clock skew; a 15-minute-exact `end` intermittently 403s.

- [x] **Step 1:** Write tests against a recorded HTTP fixture, not the live API — the live
      probe belongs in `scripts/verify_quotas.py`. Assert the request URL carries
      `feed=sip` and an `end` at least 15 minutes in the past.
- [x] **Step 2:** Run; expect FAIL.
- [x] **Step 3:** Implement, batching symbols into the multi-symbol endpoint and following
      `next_page_token` until exhausted.
- [x] **Step 4:** Run; expect PASS.
- [x] **Step 5:** Commit.

### Task 2.2: SQLite state

**Files:**
- Create: `sources/store.py`, `tests/sources/test_store.py`, `data/schema.sql`

**Interfaces:**
- Produces: tables `bars(ticker, ts, o, h, l, c, v, PRIMARY KEY(ticker, ts))`,
  `armed_levels(ticker, level, armed_on, active)`,
  `sent_alerts(id, ticker, rule, level, bar_ts, sent_at)`.
  Functions `upsert_bars`, `armed_for(ticker)`, `record_sent`, `sent_count_today()`.

- [x] **Step 1:** Write the failing test — **idempotency is the property that matters**:

```python
def test_upsert_bars_is_idempotent(tmp_db):
    upsert_bars(tmp_db, "GOOG", ten_bars)
    upsert_bars(tmp_db, "GOOG", ten_bars)
    assert row_count(tmp_db, "bars") == 10
```

- [x] **Step 2:** Run; expect FAIL.
- [x] **Step 3:** Implement with `INSERT ... ON CONFLICT DO UPDATE`. GitHub cron can
      double-fire; a non-idempotent write corrupts the history silently.
- [x] **Step 4:** Run; expect PASS.
- [x] **Step 5:** Commit.

### Task 2.3: News, earnings, events

**Files:**
- Create: `sources/finnhub.py`, `sources/marketaux.py`, `sources/edgar.py`, `sources/fred.py`

- [x] **Step 1:** Implement `company_news(tickers, since)` (Finnhub), `earnings_calendar(days)`
      (Finnhub), `ticker_news(tickers)` (Marketaux, fallback), `recent_filings(tickers)`
      (EDGAR, with a descriptive User-Agent as fair use requires), `macro_calendar(days)` (FRED).
- [x] **Step 2:** Enforce in code that news functions raise if called from an intraday
      context. **SPEC.md §4.2: intraday fetches bars only.** A guard is cheaper than a
      rediscovered quota burn.
- [x] **Step 3:** Commit.

### Task 2.4: Backfill script

**Files:**
- Create: `scripts/backfill.py`

- [x] **Step 1:** Implement: read `config/watchlist_core.yml`, fetch 250 daily bars for
      every name, upsert into `data/market.db`.
- [x] **Step 2:** Run it. **Phase 2 observable check:** every core name has 250 bars, and
      an immediate second run adds zero rows.
- [x] **Step 3:** Commit, including `data/market.db`.

**Phase 2 is done when:** the backfill is idempotent against a live provider and the
database is committed.

---

## Phase 3 — Post-close digest to Telegram *(first user-visible value)*

### Task 3.1: Telegram delivery

**Files:**
- Create: `delivery/telegram.py`, `tests/delivery/test_telegram.py`

**Interfaces:**
- Produces: `send(text: str, dry_run: bool = False) -> bool`. Markdown-escapes ticker
  symbols and numbers so a `.` in a price never breaks the message.

- [x] **Step 1:** Write the failing test for escaping — `escape_md("GOOG 350.00")` must
      not emit unbalanced Markdown.
- [x] **Step 2:** Run; expect FAIL.
- [x] **Step 3:** Implement. Token from environment only; **never as an argument**.
- [x] **Step 4:** Run; expect PASS.
- [x] **Step 5:** Commit.

### Task 3.2: Template renderer

**Files:**
- Create: `render/template.py`, `tests/render/test_template.py`

**Interfaces:**
- Produces: `render_alert(s: Suppressed) -> str` and `render_digest(items, dropped) -> str`,
  both matching the message contract in SPEC.md §6.2 exactly.

- [x] **Step 1:** Write the failing tests — assert the bar timestamp is present, that the
      string `"buy"` is absent (case-insensitive), and that a dropped-item count appears in
      the digest when `dropped` is non-empty.
- [x] **Step 2:** Run; expect FAIL.
- [x] **Step 3:** Implement. This renderer is the permanent fallback for Phase 5 — it is
      not scaffolding and must not be deleted later.
- [x] **Step 4:** Run; expect PASS.
- [x] **Step 5:** Commit.

### Task 3.3: The post-close job

**Files:**
- Create: `jobs/postclose.py`, `.github/workflows/postclose.yml`

- [x] **Step 1:** Implement the job: market-calendar check → fetch bars → fetch news and
      earnings → engine → rank → render → send → commit `data/`.
- [x] **Step 2:** Write the workflow. `cron: "15 21 * * 1-5"`, plus `workflow_dispatch`,
      plus `permissions: contents: write` so the job can commit its state.
- [ ] **Step 3:** Trigger it manually via `workflow_dispatch` and confirm a message
      arrives. **Blocked on the repo existing on GitHub with its Actions secrets set —
      both still ⬜ on the deploy checklist.** Verified as far as it can be locally on
      2026-08-06: `python -m jobs.postclose --dry-run --db <scratch>` ran the whole chain
      against the live providers and printed a well-formed digest.
- [ ] **Step 4:** Let it fire on schedule. **Phase 3 observable check: a digest arrives on
      the phone at ~17:15 ET on a trading day, unprompted.**
- [ ] **Step 5:** Commit.

### Task 3.4: Pre-market job

**Files:**
- Create: `jobs/premarket.py`, `.github/workflows/premarket.yml`

- [x] **Step 1:** Implement: same pipeline, plus writing the day's armed levels into
      `armed_levels` (`store.arm_level`, `source="job"`).
      **No counter reset exists or should.** `store.sent_count_today` derives the count
      from the UTC date of the rows in `sent_alerts`, so a new day resets it by
      construction; the only way to "reset" a derived count is deleting rows, which would
      also destroy the `last_sent` cooldown history the same table serves. This step read
      "resetting the daily sent counter" until Phase 2 built the counter — corrected
      2026-08-06, Phase 2 review.
      Record the two digests with `kind="digest"` so they do not consume the intraday
      ceiling, and note that a level disarmed at post-close is re-armed by this job for as
      long as it remains in `config/watchlist_core.yml` — whichever job owns removal owns
      it in the YAML, not in the table.
- [x] **Step 2:** `cron: "15 13 * * 1-5"` plus `workflow_dispatch`.
- [ ] **Step 3:** Confirm a pre-market digest arrives and `armed_levels` is populated.
      **Blocked on the same two ⬜ checklist rows as Task 3.3 Step 3.** The arming half is
      covered locally: `tests/jobs/test_digest.py` asserts the whole lifecycle — pre-market
      arms from the YAML, post-close disarms what fired, and the next pre-market re-arms
      anything still listed. `config/watchlist_core.yml` currently declares no levels, so a
      live run will report `armed 0 level(s)` until one is added.
- [ ] **Step 4:** Commit.

**Phase 3 is done when:** two digests arrive per trading day without anyone touching
anything. **If the project stops here, it is still useful.**

---

## Phase 4 — Intraday armed levels and the ceiling

### Task 4.1: The intraday job

**Files:**
- Create: `jobs/intraday.py`, `.github/workflows/intraday.yml`

- [ ] **Step 1:** Implement: market-open check → fetch **bars only** → engine → rank with
      `already_sent_today=sent_count_today()` → send → `record_sent` → `record_dropped` for
      everything the ceiling rejected (the post-close job is a separate process and cannot
      see `rank`'s in-process `dropped` list) → commit only if state changed.
      Three constraints Phase 2 built and this job must honour:
      **(a)** wrap the whole body in `sources.intraday_run()` — the news guard is armed by
      that context and by nothing else, and a forgotten wrapper fails silently. Assert it
      in `tests/jobs/`.
      **(b)** compare intraday prices against levels, do **not** feed 1-minute bars to
      `engine.triggers.evaluate` as a daily series: RSI(14) over fifteen one-minute closes
      returns a plausible number that is pure noise and would consume a ceiling slot.
      **(c)** if intraday bars are persisted at all, pass `timeframe=store.INTRADAY`.
- [ ] **Step 2:** Write the workflow: `cron: "*/15 14-20 * * 1-5"` plus `workflow_dispatch`.
      Add `concurrency: group: intraday, cancel-in-progress: false` so a delayed run cannot
      overlap the next and double-send.
- [ ] **Step 3:** Arm a level deliberately close to the current price. Confirm the alert
      arrives within one poll of the touch.
- [ ] **Step 4:** Arm five levels that all trigger. **Phase 4 observable check: exactly 3
      alerts arrive; the other 2 appear in that evening's post-close digest as dropped.**
- [ ] **Step 5:** Commit.

### Task 4.2: `verify_pipeline.py`

**Files:**
- Create: `scripts/verify_pipeline.py`

- [ ] **Step 1:** Implement an end-to-end dry run — fetch → engine → rank → render →
      Telegram **test chat**, sending no live alert and writing no state.
- [ ] **Step 2:** Run it; confirm the test chat receives the dry-run output.
- [ ] **Step 3:** Commit. Re-run after every change to any of these subsystems.

---

## Phase 5 — Gemini prose and the validator

### Task 5.1: Evidence packet

**Files:**
- Create: `render/evidence.py`, `tests/render/test_evidence.py`

**Interfaces:**
- Produces: `build_packet(s: Suppressed, news: list[Headline]) -> dict` — a JSON-safe dict
  containing every computed number, every rule name, and every headline with its URL and
  timestamp. **Nothing reaches the model that is not in this packet.**

- [ ] **Step 1:** Write the failing test — every numeric field on the `Trigger` appears in
      the packet, and the packet contains no key not derived from inputs.
- [ ] **Step 2:** Run; expect FAIL. **Step 3:** Implement. **Step 4:** Run; expect PASS.
- [ ] **Step 5:** Commit.

### Task 5.2: The validator *(write this before the Gemini client)*

**Files:**
- Create: `render/validator.py`, `tests/render/test_validator.py`

**Interfaces:**
- Produces: `validate(prose: str, packet: dict) -> bool` — `False` if any numeric token,
  ticker, or date in `prose` is absent from `packet`.

- [ ] **Step 1: Write the failing tests.** The adversarial case is the point:

```python
def test_rejects_fabricated_number(packet):
    assert validate("GOOG is 3% above its 150-day MA of 412.50", packet) is False

def test_accepts_prose_using_only_packet_values(packet):
    assert validate("GOOG is 0.4% from its 150-day MA of 100.00", packet) is True

def test_rejects_fabricated_ticker(packet):
    assert validate("GOOG and MSFT both sit near support", packet) is False
```

- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3:** Implement. Tokenise numbers, uppercase ticker-shaped words, and dates;
      compare against a flattened set of packet values.
- [ ] **Step 4:** Run; expect PASS.
- [ ] **Step 5:** Commit.

> The validator is written **before** the client on purpose. Building the generator first
> creates pressure to loosen the check until the generator's output passes.

### Task 5.3: Gemini client

**Files:**
- Create: `render/gemini.py`, `render/llm.py` (provider interface)

**Interfaces:**
- Produces: `class LLM(Protocol): def phrase(self, packet: dict) -> str | None` and a
  `GeminiLLM` implementation. `render/__init__.py` exposes
  `narrate(s, news) -> str` which calls the LLM, validates, and falls back to
  `render/template.py` on rejection, quota exhaustion, timeout, or any exception.

- [ ] **Step 1:** Write the system prompt: the model receives the packet and is instructed
      to phrase it for a human, add no facts, introduce no numbers, and make no
      recommendation. Give it the four rule names and what each means.
- [ ] **Step 2:** Implement `narrate` with the fallback chain. **A failure here degrades
      prose; it never blocks an alert.**
- [ ] **Step 3:** Test the fallback by pointing the client at an invalid key and confirming
      the templated alert still ships.
- [ ] **Step 4:** **Phase 5 observable check:** alerts read as prose; injecting a
      fabricated number into a mocked response causes the template to ship instead.
- [ ] **Step 5:** Commit.

---

## Phase 6 — Dashboard

### Task 6.1: Capped JSON output

**Files:**
- Create: `render/dashboard_json.py`, `tests/render/test_dashboard_json.py`

**Interfaces:**
- Produces: `write_dashboard(setups, news, events, armed, caps) -> None` writing
  `data/dashboard/{setups,news,events,armed}.json`.

- [ ] **Step 1: Write the failing test** — the property that defines the dashboard:

```python
def test_caps_are_enforced_at_write_time_not_render_time(tmp_path):
    write_dashboard(setups=[t] * 50, news=[], events=[], armed=[], caps={"setups": 5})
    assert len(json.loads((tmp_path / "setups.json").read_text())) == 5
```

- [ ] **Step 2:** Run; expect FAIL. **Step 3:** Implement. **Step 4:** Run; expect PASS.
- [ ] **Step 5:** Commit.

### Task 6.2: Static site

**Files:**
- Create: `dashboard/` (static site reading the committed JSON)

- [ ] **Step 1:** Build the four blocks in the SPEC.md §10 order: today's setups →
      relevant news → upcoming events → armed levels. Levels rendered in a form trivially
      copied into TradingView on a phone.
- [ ] **Step 2:** **Chain the `frontend-design` skill before styling.** Workspace rule:
      two previous projects shipped frontends that read as generic because this was
      skipped.
- [ ] **Step 3:** Connect the repo to Vercel Hobby. Build command outputs the static site;
      pushes from the Actions bot trigger deploys.
- [ ] **Step 4:** **Phase 6 observable check:** the live URL renders all four blocks, and
      a deliberately overloaded fixture still renders within the caps.
- [ ] **Step 5:** Commit.

---

## Phase 7 — Weekly screened watchlist

### Task 7.1: Universe and screen

**Files:**
- Create: `sources/universe.py`, `jobs/weekly_screen.py`,
  `.github/workflows/weekly_screen.yml`

- [ ] **Step 1:** Resolve **RISKS.md OQ-2 first** — confirm a free source for a US
      >$1B-market-cap universe. If none exists, fall back to index constituents and record
      that decision in SPEC.md §8. Do not proceed on an assumption here.
- [ ] **Step 2:** Implement the screen: run the Phase 1 engine over the universe, keep the
      top `screened_list_max` by rule strength, record the admitting rule and date per
      entry.
- [ ] **Step 3:** Workflow at `cron: "15 6 * * 0"` plus `workflow_dispatch`.
- [ ] **Step 4:** **Phase 7 observable check:** `data/watchlist_screened.json` regenerates
      with a rule recorded for every entry, and Monday's digest tags core versus screened
      names distinctly.
- [ ] **Step 5:** Commit.

---

## Deploy checklist

Written at plan time, per the workspace working rule — a partially-applied change is worse
than an unapplied one. ✅ = done by the implementing agent; ⬜ = requires the user.

| Surface | Action | Confirms it is live | Who |
|---|---|---|---|
| GitHub repo | Create **public** repo, push | Repo visible; Actions tab present | ⬜ |
| Alpaca | Create data-only account, generate keys | `verify_quotas.py` returns bars | ✅ 2026-08-05 |
| Finnhub / Marketaux / FRED | Register, obtain keys | `verify_quotas.py` returns 200 for each | ✅ 2026-08-05 |
| Gemini | Obtain AI Studio key | `verify_quotas.py` returns a completion | ✅ 2026-08-05 |
| Telegram | Create bot via BotFather, obtain chat ID | `verify_delivery.py` exits 0 and the message arrives | ✅ 2026-08-05 |
| Actions secrets | Add all 9 names from `.env.example` **except `TELEGRAM_TEST_CHAT_ID`** — including `EDGAR_USER_AGENT`, which is not a credential but which `sources/edgar.py` raises without | A `workflow_dispatch` run succeeds | ⬜ |
| Actions permissions | Set workflow `contents: write` | A scheduled run commits `data/` | ✅ 2026-08-06, in both workflow files |
| Workflows | Confirm all four are enabled after first push | Actions tab lists four scheduled workflows | ⬜ — two exist (`premarket`, `postclose`); `intraday` is Phase 4 and `weekly_screen` is Phase 7 |
| Vercel | Import the repo, Hobby plan, set build output | Live URL renders the dashboard | ⬜ |
| Vercel | Confirm deploy-on-push from the Actions bot | A scan commit produces a new deployment | ⬜ |
| Local | `venv` created, `.env` populated and gitignored | `verify_secrets.py` exits 0 | ✅ 2026-08-05 |

---

## Self-review against the spec

- SPEC §3 architecture → Phases 3, 4, 6 (Actions, Telegram, Vercel).
- SPEC §4 data sources and quotas → Tasks 0.3, 2.1, 2.3.
- SPEC §4.3 delayed data → Task 2.1 Step 1 asserts the 15-minute `end` offset.
- SPEC §5 engine, triggers, suppressors, confidence → Phase 1, all four tasks.
- SPEC §6 alerting contract and ceiling → Tasks 1.4, 3.2, 4.1.
- SPEC §7 LLM boundary → Phase 5, validator written before the client.
- SPEC §8 two watchlists → Task 0.1 (core), Phase 7 (screened).
- SPEC §9 no personal data, secrets → Task 0.2, Global Constraints.
- SPEC §10 dashboard hierarchy and caps → Task 6.1 (caps at write time), 6.2 (order).
- SPEC §11 layout → Task 0.1.
- SPEC §12 verification scripts → Tasks 0.2, 0.3, 4.2.
- SPEC §13 assumptions → Phase 0 gate; OQ-2 blocks Phase 7 Step 1.

No spec section is unimplemented.
