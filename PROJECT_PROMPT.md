# Market Agent — Project Kickoff Prompt

Paste the block below into a fresh Claude Code session with `projects/market project/` as the working directory.

---

```
<role>
Senior engineer who has built retail-scale market data and alerting systems, working
with a solo maintainer who is not a professional trader. Explicit anti-goal: this is a
single-user, free-tier, boring system. No microservices, no Kubernetes, no ML pipeline,
no broker integration in v1. If a design choice exists to "scale later," cut it.
</role>

<context>
The user is a part-time investor who loses money and misses entries/exits because he
cannot watch the market daily. Concrete failure he wants eliminated: Google traded near
its 150-day moving average on 2026-07-31 and he found out too late.

User profile — this is CONTEXT ONLY. Do not hardcode any of these numbers, account
details, or personal facts into code, config, or committed files. They exist so you
size the design correctly, nothing more.
  - Trades US exchanges only, cash account (no same-day buy+sell), no day trading
  - Swing trades and long positions
  - Mid-low risk tolerance, wealth-building horizon, self-taught but competent at
    technical analysis
  - Never trades companies under $1B market cap
  - Member of the "Micha Stocks" Discord community — reads it, finds the setups useful

Hard constraints:
  - FREE TIER ONLY across the entire stack — data, hosting, storage, notifications.
    Any component that requires payment must be flagged and an alternative proposed.
  - Alert delivery channel is Telegram.
  - The user CANNOT invite a bot into the Micha Stocks Discord server. He has a personal
    account there and nothing more. Treat automated reading of that server as an OPEN
    LEGAL/TOS PROBLEM, not a solved input — self-bots violate Discord's Terms of Service.
  - Single user. No auth, no multi-tenancy, no user table.
  - Windows 11 host machine; the user's workspace conventions live in the parent
    CLAUDE.md.

What "done" looks like to the user: opportunities get surfaced with enough lead time to
act on them, and alerts are actionable rather than noise. His stated bar — reasonably
anticipate and catch opportunities before they happen, and stop discovering them after
the fact.
</context>

<the_system_he_wants>
An always-present market agent that runs a pre-market scan and a post-close scan, and in
between sets price-level alerts that fire during the day so nothing needs continuous
watching. Worked example of the intended output, in his words:

  "Google is interesting from a technical standpoint if it goes past 350, because
   [reasons]" — and then an alert armed at 350 for the rest of the session.

The agent recommends what to pay attention to. It does not place trades.

Feature surface he described, in rough priority order:
  1. Multi-source synthesis — technical analysis, market news, Micha Stocks Discord
  2. Telegram alerts on defined conditions
  3. Watchlist recommendations with specific trigger levels, mirrorable into TradingView
     on his phone
  4. A dashboard: daily relevant news, interesting setups with the technical reasoning,
     an upcoming major-events calendar
  5. Portfolio tracking via an Interactive Brokers API — explicitly deferred, TODO only

Watchlist sizing is undecided: roughly 100 names watched closely, plus a periodic broader
US-market screen. Whether compute or API rate limits make that a real constraint is a
research question, not an assumption.

Design principle he stated directly and cares about most: the entire point of this project
is to REDUCE the chaos of the market. A dashboard that data-dumps or overstimulates is a
failed dashboard. Sparse and organized beats comprehensive.
</the_system_he_wants>

<how_to_work>
Use the superpowers skills, in order, and stop where told.

STEP 1 — superpowers:brainstorming.
Interrogate the requirements above before designing anything. Surface the disagreements
and the unknowns rather than papering over them. At minimum, resolve with the user:
  - Where this runs. He has no preference and wants the options explored: always-on cloud
    free tier, a scheduled GitHub Action, a local machine that is only sometimes on, or
    something else. Compare them on: does it survive the pre-market/post-close schedule,
    free-tier limits, and setup + maintenance cost for one person.
  - How Micha Stocks Discord content actually gets in, given no bot invite is possible.
    Manual paste, a self-hosted export, or dropping it from v1 entirely are all legitimate
    answers. Do not propose a self-bot as if TOS were a footnote.
  - Which free market data sources can realistically sustain the chosen watchlist size, at
    the chosen scan cadence, without hitting rate limits. Name specific providers and
    specific quota numbers.
  - Where the line sits between "agent surfaces mechanical technical triggers" and "agent
    exercises judgment about what matters." He asked for recommendations; recommendations
    that are wrong are worse than no recommendations. Propose how v1 stays honest about
    its own confidence.
  - What the alert volume ceiling is per day, since noise is the failure mode he is
    explicitly trying to escape.

STEP 2 — superpowers:writing-plans.
Produce, under `docs/` in this directory:
  - A spec: the problem, non-goals, v1 feature scope with everything else named and
    deferred, the data sources with their quotas, the alerting contract (what triggers,
    what a message contains, what the ceiling is), and the dashboard's information
    hierarchy.
  - An implementation plan: phased, each phase independently useful and independently
    verifiable, with the observable check that says a phase is done.
  - The chosen stack and directory layout, with one sentence of justification per choice
    that names what it was chosen over.
  - An explicit risks and open-questions register — free-tier quota exhaustion, data
    provider disappearing, Discord ingestion, alert fatigue, and being wrong about
    a setup.

STEP 3 — STOP.
Write no application code. Create no `src/`, no scripts, no dependency installs, no
scaffolding. Documentation and directory structure only. Execution begins in a later
session, on the user's explicit go-ahead.
</how_to_work>

<grounding>
Read the workspace `CLAUDE.md` at `C:\Users\ofir2\workspace\CLAUDE.md` before planning —
it defines the directory conventions, the venv and secrets rules, and a set of working
rules (deploy checklists at plan time, secrets never on the command line, verification
scripts committed) that this project's plan must comply with.

Verify claims about free-tier quotas, API availability, and hosting limits against current
sources rather than recalling them. Provider free tiers change often and a plan built on a
tier that no longer exists is worse than no plan. Distinguish clearly in the spec between
what you verified this session and what you assumed.
</grounding>

<done_when>
  - `docs/` holds a spec and a phased implementation plan that a reader could hand to a
    different engineer.
  - Every open question in <how_to_work> is either answered with a decision and its
    reasoning, or listed in the open-questions register with what it is blocked on.
  - The repository contains zero lines of application code.
  - Every stated data source has a named free tier and a stated quota number.
  - Nothing from the user profile appears anywhere in the committed files.
</done_when>
```
