# Phase 3 — Autonomous Stock Research (additive patch)

## What this actually automates

- Scheduled weekday *after-close* research runs on GitHub Actions (22:30 UTC). Not tick-level real-time monitoring, and not market holiday aware.
- Separate data, headline discovery, SEC filing and company facts, optional Groq synthesis, source-presence quality validation, and reporting stages with per-stage statuses.
- Dated run ID, checkpoints, partial-outage handling, evidence artifacts, and latest report committed for Streamlit display.
- Screening categories (`INVESTIGATE`, `WATCH`, `CAUTION`, `INSUFFICIENT_EVIDENCE`) are heuristics, **not** personalized buy/sell recommendations.
- No broker permissions, no automated orders, and no guaranteed profit signals.

## Integration in existing GitHub repo

1. **Do not overwrite** existing `app.py`, `engine.py`, or the already-passing rate-limited/cached historical-data implementation.
2. Copy `autonomous_agents.py`, `intelligence.py` (if absent), `pages/Autonomous_Mission_Control.py`, new tests, and `.github/workflows/autonomous-research.yml`. Keep existing Phase 2 `pages/Research_Intelligence.py` if desired.
3. If prior `research-daily.yml` exists, **disable/remove** it to prevent duplicated data collection and quota use.
4. Test: `python -m unittest discover -s tests -v`. Inspect any problems; do not push if tests fail.
5. GitHub **Settings → Secrets and variables → Actions → Secrets**:
   - `SEC_USER_AGENT`: e.g. `StockResearch contact you@your-domain.com` (real reachable contact; required for SEC endpoints).
   - `ALPHAVANTAGE_API_KEY`: optional if wanting price trend with the autonomous research; daily six-symbol fetch can cost 6 requests and is separate from Streamlit cache.
   - `GROQ_API_KEY`: optional, only when wanting actual LLM-authored evidence interpretation. Keys are never committed.
   - Repository **Variable** `RESEARCH_WATCHLIST`: optional, comma-separated tickers.
6. **Actions → Autonomous Post-Close Equity Research → Run workflow** once. Inspect logs and artifact. The workflow requests `contents:write` solely to commit `reports/autonomous/latest.json` and `status.json` for the dashboard.
7. Verify the repo's `main` branch now contains `reports/autonomous/latest.json`. Streamlit will load the new `Autonomous Mission Control` page after the GitHub commit/redeploy. GitHub branch protection may prevent the report commit; if so, artifacts are still saved but the dashboard does not auto-update.
8. Avoid manual Streamlit refresh API calls; the scheduled report is gathered independently of app page visits.

## Known limitations / critical caveats

- **Not verified against live APIs in this package**. Unit tests are deterministic mocks; full cloud workflow requires deployment and real credentials.
- This report data is **committed to repository history**. In a public repository, evidence results and tracked watchlist will be public. Only use public market research and **never** include personal portfolio holdings, contact details or confidential notes in outputs.
- Google News RSS gives headline links; article claims are **not validated**. The validator confirms source *presence*, not truth or cross-source corroboration. `PRIMARY_EVIDENCE_PRESENT` means SEC filings and some XBRL facts exist, **not** that a bullish case is established.
- XBRL facts may reflect different periods. Results are tagged with dates and must not be compared as if annual/quarterly are equal.
- Many ETFs (SPY) do not have SEC corporate XBRL facts, so they can remain `PARTIAL_EVIDENCE`. This is expected.
- GitHub Actions schedules are best-effort (can be delayed or skipped); GitHub workflow may be disabled after prolonged inactivity, depending on repository configuration.
- Market-data calls are paced at least 1.7 seconds per ticker **within the Action run**, not across separate systems. Alpha Vantage daily quota is shared with manual Streamlit calls. This is **not a fully quota-coordinated solution**; long-term requires a shared durable store or higher-quality data source.
- This patch uses Groq only if securely configured; output remains unverified and does not execute tools.
- This is automated **post-close public research**, not 24/7 live AI agents or a production institutional investment adviser. Reliable intraday alerts require a continuously running worker and licensed streaming market feed.
