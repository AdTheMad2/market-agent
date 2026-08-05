"""Confirm every provider's free tier still exists, against a live key.

This script does NOT assert documented quota numbers. Documented numbers go stale:
Alpha Vantage cut its free tier from 500/day to 25/day, Fly.io and Railway removed
free tiers outright, Polygon rebranded to Massive. The job here is to hit each
provider for real and print whatever rate-limit headers come back, so the day a
documented quota stops being true is the day this output changes.

Run:  python scripts/verify_quotas.py
Exit: 0 when every configured provider returned usable data
      1 when any configured provider failed
      (providers with no key are SKIPPED and do not fail the run, so this is
       useful while keys are still being collected)

Paste the output into docs/RISKS.md §4 with the date. That log is the record of
what was true, as opposed to what a vendor page claimed.

See docs/SPEC.md §4 and docs/IMPLEMENTATION_PLAN.md Task 0.3.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts._dotenv import load_env  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 20

# Header names worth surfacing. Providers disagree on capitalisation and spelling,
# so matching is case-insensitive and by substring.
RATE_LIMIT_HINTS = ("ratelimit", "rate-limit", "x-mbx", "retry-after", "quota")


@dataclass
class Probe:
    provider: str
    endpoint: str = ""
    status: int | None = None
    sample: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    outcome: str = "OK"  # OK | FAIL | SKIP
    note: str = ""


def rate_limit_headers(response: requests.Response) -> dict[str, str]:
    return {
        k: v
        for k, v in response.headers.items()
        if any(hint in k.lower() for hint in RATE_LIMIT_HINTS)
    }


def skip(provider: str, missing: str) -> Probe:
    return Probe(provider=provider, outcome="SKIP", note=f"{missing} not set")


def sip_end() -> str:
    """An `end` far enough in the past that free SIP data is permitted.

    Free consolidated data requires the window to end at least 15 minutes ago.
    The extra minute is slack against clock skew — a 15-minute-exact `end`
    intermittently 403s. Mirrors config/rules.yml data.sip_delay_minutes.
    """
    return (datetime.now(UTC) - timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")


def sip_start() -> str:
    """A `start` far enough back to span a holiday weekend.

    Without an explicit `start` Alpaca returns an empty `bars` list rather than
    defaulting to a sensible window — verified 2026-08-05, HTTP 200 with zero
    bars. Ten days covers any run of market closures.
    """
    return (datetime.now(UTC) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")


def probe_alpaca() -> Probe:
    key, secret = os.environ.get("ALPACA_API_KEY_ID"), os.environ.get("ALPACA_API_SECRET_KEY")
    if not (key and secret):
        return skip("Alpaca (bars, SIP)", "ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY")

    url = "https://data.alpaca.markets/v2/stocks/bars"
    params = {
        "symbols": "AAPL,MSFT",
        "timeframe": "1Day",
        "feed": "sip",
        "start": sip_start(),
        "end": sip_end(),
    }
    # No `limit`: Alpaca truncates from the start of the window, so a limit would
    # return the oldest bars in the range and the freshness check below would be
    # meaningless.
    p = Probe(provider="Alpaca (bars, SIP)", endpoint=url)
    try:
        r = requests.get(
            url,
            params=params,
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=TIMEOUT,
        )
        p.status, p.headers = r.status_code, rate_limit_headers(r)
        if r.ok:
            bars = r.json().get("bars", {}).get("AAPL", [])
            if bars:
                p.sample = f"AAPL {bars[-1]['t']} close={bars[-1]['c']}"
            else:
                p.outcome, p.note = "FAIL", "200 but no bars returned"
        else:
            p.outcome, p.note = "FAIL", r.text[:200]
    except Exception as exc:  # noqa: BLE001 — a probe must report, never crash the run
        p.outcome, p.note = "FAIL", f"{type(exc).__name__}: {exc}"
    return p


def probe_finnhub() -> Probe:
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return skip("Finnhub (company news)", "FINNHUB_API_KEY")

    url = "https://finnhub.io/api/v1/company-news"
    today = datetime.now(UTC).date()
    params = {
        "symbol": "AAPL",
        "from": str(today - timedelta(days=7)),
        "to": str(today),
        "token": key,
    }
    p = Probe(provider="Finnhub (company news)", endpoint=url)
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        p.status, p.headers = r.status_code, rate_limit_headers(r)
        if r.ok:
            items = r.json()
            p.sample = f"{len(items)} articles; latest: {items[0]['headline'][:60]}" if items else "0 articles"
        else:
            p.outcome, p.note = "FAIL", r.text[:200]
    except Exception as exc:  # noqa: BLE001
        p.outcome, p.note = "FAIL", f"{type(exc).__name__}: {exc}"
    return p


def probe_marketaux() -> Probe:
    key = os.environ.get("MARKETAUX_API_KEY")
    if not key:
        return skip("Marketaux (news)", "MARKETAUX_API_KEY")

    url = "https://api.marketaux.com/v1/news/all"
    p = Probe(provider="Marketaux (news)", endpoint=url)
    try:
        r = requests.get(
            url, params={"symbols": "AAPL", "limit": 1, "api_token": key}, timeout=TIMEOUT
        )
        p.status, p.headers = r.status_code, rate_limit_headers(r)
        if r.ok:
            body = r.json()
            found = body.get("meta", {}).get("found")
            p.sample = f"meta.found={found}"
            # Marketaux reports quota exhaustion in the body, not the status code.
            if "error" in body:
                p.outcome, p.note = "FAIL", str(body["error"])[:200]
        else:
            p.outcome, p.note = "FAIL", r.text[:200]
    except Exception as exc:  # noqa: BLE001
        p.outcome, p.note = "FAIL", f"{type(exc).__name__}: {exc}"
    return p


def probe_gemini() -> Probe:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return skip("Gemini (prose)", "GEMINI_API_KEY")

    # gemini-2.5-flash 404s for keys created after ~mid-2026 ("no longer available
    # to new users") while still appearing in ListModels — the listing is not a
    # reliable statement of what a given key may call. Verified 2026-08-05.
    model = "gemini-3.6-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    p = Probe(provider="Gemini (prose)", endpoint=url)
    try:
        r = requests.post(
            url,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": "Reply with the single word: ok"}]}],
                # Gemini 3.x reasons before answering and thinking tokens are billed
                # against maxOutputTokens. A 16-token budget returns HTTP 200 with an
                # empty candidate — 74 thought tokens and no text. Verified 2026-08-05.
                "generationConfig": {"maxOutputTokens": 256},
            },
            timeout=TIMEOUT,
        )
        p.status, p.headers = r.status_code, rate_limit_headers(r)
        if r.ok:
            body = r.json()
            # Thought parts precede the answer part; the reply is always last.
            parts = body["candidates"][0].get("content", {}).get("parts", [])
            text = parts[-1].get("text", "").strip() if parts else ""
            usage = body.get("usageMetadata", {})
            if not text:
                finish = body["candidates"][0].get("finishReason")
                p.outcome, p.note = "FAIL", f"200 but no text returned (finishReason={finish})"
            p.sample = f"{model} replied {text!r}; tokens={usage.get('totalTokenCount')}"
        else:
            p.outcome, p.note = "FAIL", r.text[:200]
    except Exception as exc:  # noqa: BLE001
        p.outcome, p.note = "FAIL", f"{type(exc).__name__}: {exc}"
    return p


def probe_fred() -> Probe:
    key = os.environ.get("FRED_API_KEY")
    if not key:
        return skip("FRED (macro)", "FRED_API_KEY")

    url = "https://api.stlouisfed.org/fred/series"
    p = Probe(provider="FRED (macro)", endpoint=url)
    try:
        r = requests.get(
            url,
            params={"series_id": "DFF", "api_key": key, "file_type": "json"},
            timeout=TIMEOUT,
        )
        p.status, p.headers = r.status_code, rate_limit_headers(r)
        if r.ok:
            series = r.json()["seriess"][0]
            p.sample = f"{series['id']}: {series['title'][:50]}"
        else:
            p.outcome, p.note = "FAIL", r.text[:200]
    except Exception as exc:  # noqa: BLE001
        p.outcome, p.note = "FAIL", f"{type(exc).__name__}: {exc}"
    return p


def probe_edgar() -> Probe:
    """SEC EDGAR needs no key, but its fair-use policy requires a descriptive
    User-Agent identifying the operator. Requests without one are blocked."""
    agent = os.environ.get("EDGAR_USER_AGENT")
    if not agent:
        return skip("SEC EDGAR (filings)", "EDGAR_USER_AGENT")

    url = "https://data.sec.gov/submissions/CIK0000320193.json"  # Apple Inc.
    p = Probe(provider="SEC EDGAR (filings)", endpoint=url)
    try:
        r = requests.get(url, headers={"User-Agent": agent}, timeout=TIMEOUT)
        p.status, p.headers = r.status_code, rate_limit_headers(r)
        if r.ok:
            body = r.json()
            recent = body["filings"]["recent"]
            p.sample = f"{body['name']}: latest {recent['form'][0]} on {recent['filingDate'][0]}"
        else:
            p.outcome, p.note = "FAIL", r.text[:200]
    except Exception as exc:  # noqa: BLE001
        p.outcome, p.note = "FAIL", f"{type(exc).__name__}: {exc}"
    return p


def probe_telegram() -> Probe:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return skip("Telegram (delivery)", "TELEGRAM_BOT_TOKEN")

    # The token is in the path by Telegram's design. It is never logged: the
    # endpoint is reported with the token redacted.
    url = f"https://api.telegram.org/bot{token}/getMe"
    p = Probe(provider="Telegram (delivery)", endpoint="https://api.telegram.org/bot<TOKEN>/getMe")
    try:
        r = requests.get(url, timeout=TIMEOUT)
        p.status, p.headers = r.status_code, rate_limit_headers(r)
        if r.ok and r.json().get("ok"):
            bot = r.json()["result"]
            p.sample = f"@{bot['username']} (id {bot['id']})"
        else:
            p.outcome, p.note = "FAIL", "authentication failed (token redacted from output)"
    except Exception as exc:  # noqa: BLE001
        p.outcome, p.note = "FAIL", f"{type(exc).__name__}: {exc}"
    return p


PROBES = (
    probe_alpaca,
    probe_finnhub,
    probe_marketaux,
    probe_gemini,
    probe_fred,
    probe_edgar,
    probe_telegram,
)


def main() -> int:
    # The Windows console defaults to cp1252 and mangles non-ASCII. This output is
    # meant to be pasted into docs/RISKS.md §4, so it has to survive the trip.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    load_env(REPO_ROOT / ".env")

    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    print(f"Provider verification — {stamp}\n")

    results = [probe() for probe in PROBES]

    for p in results:
        marker = {"OK": "  OK  ", "FAIL": " FAIL ", "SKIP": " SKIP "}[p.outcome]
        print(f"[{marker}] {p.provider}")
        if p.outcome == "SKIP":
            print(f"           {p.note}\n")
            continue
        print(f"           {p.endpoint}")
        print(f"           HTTP {p.status}")
        if p.sample:
            print(f"           data: {p.sample}")
        if p.headers:
            print("           rate-limit headers returned:")
            for k, v in sorted(p.headers.items()):
                print(f"             {k}: {v}")
        else:
            print("           rate-limit headers returned: none")
        if p.note:
            print(f"           note: {p.note}")
        print()

    ok = sum(1 for p in results if p.outcome == "OK")
    failed = [p.provider for p in results if p.outcome == "FAIL"]
    skipped = [p.provider for p in results if p.outcome == "SKIP"]

    print(f"{ok} OK, {len(failed)} failed, {len(skipped)} skipped (no key)")
    if skipped:
        print(f"  skipped: {', '.join(skipped)}")
    if failed:
        print(f"  FAILED:  {', '.join(failed)}")
        print("\n  A provider failing here is a spec problem, not a code problem.")
        print("  Revise docs/SPEC.md §4 before building on it.")
        return 1

    if skipped:
        print("\n  Phase 0 is not complete until no provider is skipped.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
