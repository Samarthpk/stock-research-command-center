# Stock Research Command Center — Phase 4: Global Autonomous Stock Intelligence

**Status:** runnable local and Streamlit Cloud research platform supporting **US (NYSE / NASDAQ in USD `$`)** and **India (NSE / BSE in INR `₹`)**. Historical closing data and primary regulatory disclosures only; **no automated broker trading** and **not investment advice**.

## Features (Phase 1–4)
- **Multi-Market Selection (`All Markets` / `US` / `India`)**:
  - **US Watchlist Default**: `AAPL, MSFT, NVDA, AMZN, GOOGL, SPY` (NYSE / NASDAQ, native `USD $`).
  - **India Watchlist Default**: `RELIANCE, TCS, HDFCBANK, INFY, ICICIBANK` (NSE / BSE, native `INR ₹`).
  - **Strict Identifier Separation**: NSE symbols (`RELIANCE`), BSE scrip codes (`500325`), Alpha Vantage BSE tickers (`RELIANCE.BSE`), and ISINs (`INE002A01018`) are explicitly mapped and never treated as interchangeable.
- **Market-Specific Calendars, Holidays & Time Zones**:
  - **US**: `America/New_York` (`09:30–16:00 ET`) with automatic EST/EDT Daylight Saving Time handling and NYSE/NASDAQ 2025–2026 holiday calendar.
  - **India**: `Asia/Kolkata` (`09:15–15:30 IST`) with NSE/BSE 2025–2026 holiday calendar.
- **Rate-Limited Market Data & SQLite Caching**:
  - Thread-safe `RequestScheduler` (`>= 1.5s` between live Alpha Vantage requests), bounded exponential backoff on temporary/ambiguous rate limits, immediate stop on confirmed daily quota exhaustion, and local SQLite caching with staleness detection.
  - Built-in verification helpers for Indian `.BSE` symbol support (`verify_bse_symbol_support`) and separate `NIFTY 50` / `SENSEX` benchmark availability checks (`research_indian_benchmarks`).
- **Jurisdiction-Aware Regulatory & News Intelligence**:
  - **US Equities**: SEC EDGAR `10-K`, `10-Q`, `8-K` filings and US-GAAP XBRL company facts (`SEC_USER_AGENT` required), plus US news discovery (`gl=US`).
  - **Indian Equities**: US SEC EDGAR logic is strictly disabled for Indian companies. Uses Indian corporate disclosure discovery (`IND-AS` / SEBI LODR Reg 30, 33 & 34) with direct links to official BSE and NSE corporate filing portals, plus India news discovery (`gl=IN`).
- **5-Pillar Evidence Summaries & Auditable Rankings**:
  - Synthesizes **Technical**, **Fundamental**, **Valuation**, **Sentiment**, and **Risk** pillars into `INVESTIGATE`, `WATCH`, `CAUTION`, or `INSUFFICIENT_EVIDENCE` rankings.
  - Every ranking displays its `as_of` date, verified `evidence_citations`, and explicit `uncertainty`. Prices, earnings, valuations, and citations are never fabricated.
- **Unattended Post-Close Workflows & Persistent Reports**:
  - Separate weekday GitHub Actions schedules for India (`30 10 * * 1-5` UTC / `16:00 IST`) and US (`30 22 * * 1-5` UTC) with `--enforce-session-lock` to prevent duplicate runs in the same market session.
  - Persists separate US (`us_latest.json`, `us_daily_brief.md`), India (`india_latest.json`, `india_daily_brief.md`), and Consolidated Global (`global_latest.json`, `global_daily_brief.md`, `latest.json`) reports for the passive **Autonomous Mission Control** dashboard page.

## Install and run
```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export ALPHAVANTAGE_API_KEY='YOUR_KEY'
export SEC_USER_AGENT='StockResearchCommandCenter/0.4 your-email@example.com'
streamlit run app.py
```

## Autonomous Post-Close CLI
```bash
# Run US, India, or All Markets post-close mission
python autonomous_agents.py --market US --enforce-session-lock
python autonomous_agents.py --market IN --enforce-session-lock
python autonomous_agents.py --market ALL
```

## Tests
```bash
python -m unittest discover -s tests -v
```
