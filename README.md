# Market Agent

A single-user market agent. It runs a pre-market and a post-close scan, arms price-level
alerts that fire during the session, delivers them to Telegram under a hard daily ceiling,
and renders a deliberately sparse dashboard.

It recommends what to pay attention to. **It does not place trades, and the word "buy"
appears in no output.**

Everything runs on free tiers: GitHub Actions for compute and scheduling, Vercel Hobby for
the dashboard, Alpaca and Finnhub for data, Gemini Flash for prose, Telegram for delivery.

## Documentation

| Document | Contents |
|---|---|
| [docs/SPEC.md](docs/SPEC.md) | What this is, what it is not, and what "correct" means |
| [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) | Phased build order and the observable check that ends each phase |
| [docs/RISKS.md](docs/RISKS.md) | Decisions with their reasoning, risk register, open questions |

## Status

**Phases 0–4 done and verified against the live market. Phase 5 in progress.**

Two digests and the intraday poll run unattended on GitHub Actions. On 2026-08-07 five
armed levels were touched, exactly three alerts went out, and the other two were held
back at the ceiling and reported in that evening's digest — the ceiling working as
specified, against real prices.

Still to come: Gemini prose with a fabricated-number validator (Phase 5), the dashboard
(Phase 6), and the weekly screened watchlist (Phase 7). Until Phase 5 lands, alerts are
rendered by a deterministic template — which stays as the permanent fallback.

See the phase table in [the implementation plan](docs/IMPLEMENTATION_PLAN.md) for the
observable check behind each of those.

### Known operational limits

- **GitHub's free scheduled queue runs late** — measured 3h48m on one post-close run.
  A digest written for 17:15 ET can land after 21:00 ET. Nothing in this code can shorten
  it; see R-13 in [docs/RISKS.md](docs/RISKS.md).
- Combined with the 15-minute SIP delay, an intraday alert lands roughly 30 minutes after
  the touch. That is the design, not a defect. This is not a day-trading tool.

## Setup

```bash
python -m venv venv
venv\Scripts\activate          # Windows
pip install -e ".[dev]"

cp config/.env.example .env    # then fill it in
```

`.env` is gitignored. **Never pass a secret on a command line** — session transcripts are
permanent on-disk logs. See [docs/SPEC.md §9](docs/SPEC.md).

## Verification

Every script worth running once against a live system lives in `scripts/` and is committed,
so it can be re-run after every change to that subsystem.

```bash
python scripts/verify_secrets.py   # every key present; no key leaked into the tree
python scripts/verify_quotas.py    # every provider's free tier still exists, live
```

`verify_quotas.py` deliberately prints each provider's returned rate-limit headers rather
than asserting documented values. Its job is to detect the day a documented quota stops
being true.

## A note on the data

Prices are **15-minute-delayed consolidated (SIP)** data. Free real-time equity data is
IEX-only — a single venue carrying roughly 2.5% of US volume, which misses level touches
outright. Correct prices late beats wrong prices now.

Every alert states the timestamp of the bar it was computed from, never the time the
message was sent. This system cannot support day trading and does not claim to.
