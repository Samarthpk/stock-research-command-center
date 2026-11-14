# Stock Research Command Center — v0.1

**Status:** runnable local research prototype. Not hosted here, not continuously monitoring markets, not an AI/LLM decision system, not investment advice.

## Features
- Editable watchlist (max 20 symbols)
- Real Alpha Vantage daily close data when configured
- Technical research modules: trend, momentum, historical volatility, brief generation
- Explicit data-as-of dates and API failure logging
- SQLite persistence and downloadable Markdown brief
- Script for daily execution with operating-system scheduler

## Install and run
```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
export ALPHAVANTAGE_API_KEY='YOUR_KEY'  # Windows PowerShell: $env:ALPHAVANTAGE_API_KEY='YOUR_KEY'
streamlit run app.py
```
Access the local URL Streamlit prints, normally `http://localhost:8501`.
Get an API key from https://www.alphavantage.co/support/ . Keep keys outside Git and screenshots. Do not expose an unauthenticated deployment to the public internet.

## Scheduled daily report
After setting the same API key in the scheduler's environment, invoke `python daily.py` once each trading day. Example Linux cron (run after the US closing data is available):
```
30 20 * * 1-5 cd /path/to/stock_research_command_center && /path/to/.venv/bin/python daily.py
```
This example is in the system's local timezone, **not necessarily New York time**; adapt for DST. Do not use the cron example without setting correct timezone, logging and credentials. Generated reports appear under `reports/`.

## Constraints
- Alpha Vantage free access currently advertises 25 requests/day; using six symbols costs up to six calls per full refresh. Exceeding limits returns a failure, which is shown, not replaced with invented quotes.
- Daily price series is not real-time. Quote freshness is recorded; stale entries remain clearly dated.
- Trend signals are *descriptive*, not stock recommendations. No brokerage connection or trades.
- `report` records the provider's method documentation rather than a specific raw response audit trail; for production implement raw data snapshots, source traceability and news/SEC evidence.
- For genuine autonomous AI agents add a hosted worker, LLM provider, licensed market and news feeds, fundamental validation, guardrails, observability, and authenticated hosting.

## Tests
`python -m unittest discover -s tests -v`
