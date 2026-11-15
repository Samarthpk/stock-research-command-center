"""Passive dashboard for unattended US, India, and Global scans. Does NOT trigger provider calls on page load."""
from pathlib import Path
import importlib
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamlit as st
import global_markets
if getattr(global_markets, '_GLOBAL_MARKETS_VERSION', None) != '4.1':
    global_markets = importlib.reload(global_markets)
from global_markets import format_price

try:
    st.set_page_config(page_title='Autonomous Mission Control', layout='wide')
except Exception:
    pass
st.title('🛰️ Autonomous Mission Control — Global Intelligence')
st.caption(
    'Scheduled post-market evidence research for US (NYSE/NASDAQ in USD $) and India (NSE/BSE in INR ₹). '
    'Historical data; no live trading, no guaranteed signals.'
)

report_dir = Path(__file__).resolve().parents[1] / 'reports' / 'autonomous'

view_choice = st.radio(
    'Mission Report Scope',
    options=['All Markets (Consolidated Global)', 'US Market (NYSE / NASDAQ)', 'India Market (NSE / BSE)'],
    horizontal=True,
)

file_map = {
    'All Markets (Consolidated Global)': ('global_latest.json', 'global_daily_brief.md'),
    'US Market (NYSE / NASDAQ)': ('us_latest.json', 'us_daily_brief.md'),
    'India Market (NSE / BSE)': ('india_latest.json', 'india_daily_brief.md'),
}
json_name, md_name = file_map[view_choice]
path = report_dir / json_name
md_path = report_dir / md_name

if not path.exists() and (report_dir / 'latest.json').exists():
    path = report_dir / 'latest.json'

if not path.exists():
    st.warning(
        f'No scheduled mission report ({json_name}) has been published yet. '
        'Run the GitHub Actions workflow or wait for a scheduled post-close run. '
        'This page is passive and does not launch agents automatically.'
    )
    st.stop()

try:
    data = json.loads(path.read_text(encoding='utf-8'))
except (OSError, ValueError):
    st.error('Stored research report could not be read. Check workflow artifacts/logs.')
    st.stop()

st.write(
    '**Mission:**', data.get('mission_id', 'unknown'),
    '  **Market Scope:**', data.get('market', 'US'),
    '  **Overall Status:**', data.get('status', 'unknown'),
)
st.write(
    '**Started (UTC):**', data.get('started_at', 'unknown'),
    '  **Completed (UTC):**', data.get('finished_at', data.get('updated_at', 'unknown')),
)

# Requirement 5: Identify COMPLETE, PARTIAL, and FAILED research separately
outcome_counts = data.get('outcome_counts') or {}
outcomes_by_symbol = data.get('outcomes_by_symbol') or {}
if not outcome_counts:
    outcome_counts = {'COMPLETE': 0, 'PARTIAL': 0, 'FAILED': 0}
    outcomes_by_symbol = {'COMPLETE': [], 'PARTIAL': [], 'FAILED': []}
    for r in data.get('results', []):
        out = r.get('research_outcome') or (
            'COMPLETE' if r.get('quality') == 'PRIMARY_EVIDENCE_PRESENT' and r.get('price', {}).get('status') == 'OK'
            else ('PARTIAL' if r.get('quality') in ('PRIMARY_EVIDENCE_PRESENT', 'PARTIAL_EVIDENCE') or r.get('price', {}).get('status') == 'OK' else 'FAILED')
        )
        outcome_counts[out] = outcome_counts.get(out, 0) + 1
        outcomes_by_symbol.setdefault(out, []).append(r.get('ticker', '?'))

st.subheader('Research Outcome Breakdown (COMPLETE / PARTIAL / FAILED)')
oc1, oc2, oc3 = st.columns(3)
oc1.metric(
    'COMPLETE Research',
    outcome_counts.get('COMPLETE', 0),
    help='Verified primary filings + verified company-specific financial metrics + verified historical OHLCV price series.',
)
oc1.caption('Symbols: ' + (', '.join(outcomes_by_symbol.get('COMPLETE', [])) or 'None'))
oc2.metric(
    'PARTIAL Research',
    outcome_counts.get('PARTIAL', 0),
    help='Some evidence retrieved (e.g. news or filings or price), but primary financial evidence or price is incomplete.',
)
oc2.caption('Symbols: ' + (', '.join(outcomes_by_symbol.get('PARTIAL', [])) or 'None'))
oc3.metric(
    'FAILED Research',
    outcome_counts.get('FAILED', 0),
    help='All data/evidence sources failed or returned empty.',
)
oc3.caption('Symbols: ' + (', '.join(outcomes_by_symbol.get('FAILED', [])) or 'None'))

rows = []
for rec in data.get('results', []):
    p = rec.get('price', {})
    sig = rec.get('signal', {})
    currency = rec.get('currency') or p.get('currency') or 'USD'
    close_val = p.get('close')
    rows.append({
        'Ticker': rec.get('ticker'),
        'Market': rec.get('market', 'US'),
        'Exchange': rec.get('exchange', 'NYSE/NASDAQ'),
        'Currency': currency,
        'Research Outcome': rec.get('research_outcome', 'PARTIAL'),
        'Ranking': sig.get('label', 'INSUFFICIENT_EVIDENCE'),
        'Evidence Quality': rec.get('quality'),
        'As-Of Date': sig.get('as_of') or p.get('as_of'),
        'Close (Native)': format_price(close_val, currency) if close_val is not None else 'N/A',
        'Trend': p.get('trend'),
        'Citations': len(sig.get('evidence_citations', [])),
        'Price Status': p.get('status'),
        'AI Status': rec.get('ai', {}).get('status'),
    })

st.dataframe(rows, use_container_width=True, hide_index=True)
st.caption(
    'INVESTIGATE / WATCH / CAUTION / INSUFFICIENT_EVIDENCE are evidence-completeness and technical screening rankings, '
    'not buy/sell instructions. No company is ever ranked without verified primary filings, official financial metrics, and historical price data.'
)

for rec in data.get('results', []):
    sig = rec.get('signal', {})
    currency = rec.get('currency') or rec.get('price', {}).get('currency') or 'USD'
    close_fmt = format_price(rec.get('price', {}).get('close'), currency)
    outcome = rec.get('research_outcome', 'PARTIAL')
    header = (
        f"[{rec.get('market', 'US')}] {rec.get('ticker', '?')} ({currency}) — "
        f"Outcome: {outcome} | Ranking: {sig.get('label', '?')} — Close: {close_fmt} (As-Of: {sig.get('as_of') or 'N/A'})"
    )
    with st.expander(header):
        st.markdown(f"**Research Outcome:** `{outcome}` | **Evidence Quality:** `{rec.get('quality', 'INSUFFICIENT_EVIDENCE')}`")
        st.markdown(f"**Signal Rationale:** {sig.get('reason', 'N/A')}")
        st.markdown(f"**As-Of Date:** `{sig.get('as_of') or 'N/A'}`")
        st.markdown(f"**Uncertainty Disclosure:** {sig.get('uncertainty', 'Historical end-of-day evidence only.')}")

        pillars = rec.get('five_pillar_summary') or rec.get('pillars') or {}
        if pillars:
            st.markdown('**5-Pillar Evidence Summary**')
            p_cols = st.columns(5)
            for col, (p_key, p_title) in zip(
                p_cols,
                [
                    ('technical', '1. Technical'),
                    ('fundamental', '2. Fundamental'),
                    ('valuation', '3. Valuation'),
                    ('sentiment', '4. Sentiment'),
                    ('risk', '5. Risk'),
                ],
            ):
                p_data = pillars.get(p_key, {})
                with col:
                    st.markdown(f"**{p_title}**")
                    st.caption(f"Status: `{p_data.get('status') or p_data.get('data_quality_tier') or 'N/A'}`")
                    st.json(p_data)

        citations = sig.get('evidence_citations', [])
        if citations:
            st.markdown('**Verified Evidence Citations**')
            for cit in citations:
                st.markdown(
                    f"- **[{cit.get('type', 'source')}]** [{cit.get('title', 'Citation')}]({cit.get('url', '#')}) "
                    f"(As-Of: `{cit.get('as_of', 'unknown')}`)"
                )

        st.write('**Agent execution**')
        st.json(rec.get('agents', {}))
        st.write('**Evidence — company-specific discovered headlines (not independently verified)**')
        for n in rec.get('news', []):
            st.markdown(f"- [{n.get('title', 'Source')}]({n.get('url', '#')}) — {n.get('published', 'unknown')}")

        filing_header = (
            '**Verified Primary Indian Exchange Filings (Official BSE / NSE / SEBI Documents)**'
            if rec.get('market') == 'IN'
            else '**Verified Primary US SEC EDGAR Filings (10-K / 10-Q / 8-K)**'
        )
        st.write(filing_header)
        primary_filings = rec.get('filings', {}).get('filings', [])
        if not primary_filings:
            st.caption('No direct official exchange filing documents retrieved in this automated run.')
        for f in primary_filings:
            st.markdown(f"- [{f.get('title', 'Filing')}]({f.get('url', '#')}) — {f.get('published', 'unknown')}")

        secondary_mentions = rec.get('filings', {}).get('secondary_disclosure_mentions', [])
        if secondary_mentions:
            st.write('**Company-Specific Secondary Disclosure Mentions (Unverified Media Sources — Not Counted as Primary Filings)**')
            for m in secondary_mentions:
                st.markdown(f"- [{m.get('title', 'Mention')}]({m.get('url', '#')}) — {m.get('published', 'unknown')}")

        st.write('**Verified Reported Accounting / Fundamental Facts (never fabricated)**')
        st.json(rec.get('fundamentals', {}).get('metrics', {}))
        if rec.get('fundamentals', {}).get('warning'):
            st.caption(rec['fundamentals']['warning'])

        ai = rec.get('ai', {})
        if ai.get('status') == 'UNVERIFIED_LLM_SYNTHESIS':
            st.warning('Unverified model-generated interpretation: inspect original sources before relying on it')
            st.write(ai.get('analysis', ''))

dl_col1, dl_col2 = st.columns(2)
with dl_col1:
    st.download_button(
        f'Download {json_name}',
        data=json.dumps(data, indent=2),
        file_name=json_name,
        mime='application/json',
    )
with dl_col2:
    if md_path.exists():
        st.download_button(
            f'Download {md_name}',
            data=md_path.read_text(encoding='utf-8'),
            file_name=md_name,
            mime='text/markdown',
        )
