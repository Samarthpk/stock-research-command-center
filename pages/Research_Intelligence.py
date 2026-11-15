"""Additive Streamlit page for US SEC EDGAR & India NSE/BSE Exchange Disclosures + News + Optional AI Review."""
import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamlit as st
import intelligence
if getattr(intelligence, '_INTELLIGENCE_VERSION', None) != '4.2':
    intelligence = importlib.reload(intelligence)
from intelligence import build_research, llm_evidence_review

try:
    st.set_page_config(page_title='Research Intelligence', layout='wide')
except Exception:
    pass
st.title('🔎 Research Intelligence — US SEC EDGAR, India NSE/BSE Disclosures & News')
st.caption('Source-backed research · US SEC EDGAR (10-K/10-Q/8-K) & India BSE/NSE (SEBI LODR / IND-AS) · no automated trades')


def secret(name):
    try:
        return st.secrets.get(name) or os.getenv(name, '')
    except Exception:
        return os.getenv(name, '')


qp_intel_mkt = str(st.query_params.get('market', '')).strip().upper()
qp_intel_sym = str(st.query_params.get('run_intel', '')).strip().upper()

col_m, col_t = st.columns([1, 2])
with col_m:
    market_label = st.selectbox(
        'Market Jurisdiction',
        options=['US (NYSE / NASDAQ — SEC EDGAR)', 'India (NSE / BSE — SEBI LODR / IND-AS)'],
        index=1 if qp_intel_mkt in ('IN', 'INDIA') else 0,
    )
    market_code = 'IN' if market_label.startswith('India') else 'US'
with col_t:
    default_sym = qp_intel_sym or ('RELIANCE' if market_code == 'IN' else 'AAPL')
    ticker = st.text_input('Stock ticker', value=default_sym).strip().upper()

sec_identity = secret('SEC_USER_AGENT')
if market_code == 'US' and not sec_identity:
    st.warning('SEC_USER_AGENT not configured: provide a descriptive app name and your contact email in Streamlit Secrets to enable US SEC EDGAR.')
elif market_code == 'IN':
    st.info('Indian equity selected: US SEC EDGAR logic is disabled. Using company-specific BSE/NSE disclosure verification and India news discovery.')

if st.button('Run research', type='primary') or (qp_intel_sym and st.session_state.get('evidence_research', {}).get('ticker') != qp_intel_sym):
    with st.spinner(f'Retrieving {market_code} company-specific news and regulatory disclosure evidence...'):
        result = build_research(
            ticker,
            user_agent=sec_identity,
            include_sec=bool(sec_identity) if market_code == 'US' else True,
            market=market_code,
        )
        st.session_state['evidence_research'] = result

if 'evidence_research' in st.session_state:
    r = st.session_state['evidence_research']
    st.write(
        '**Ticker:**', r['ticker'],
        '  **Market:**', r.get('market', 'US'),
        '  **Currency:**', r.get('currency', 'USD'),
        '  **Research status:**', r['assessment'],
    )
    st.caption('Generated UTC: ' + r['generated_at'])
    for err in r['errors']:
        st.error(err)
    st.subheader(f"News discovery ({r.get('market', 'US')} company-filtered pipeline — headline-only)")
    if not r['news']:
        st.info('No verified company-specific headlines fetched')
    for item in r['news']:
        st.markdown(f"- [{item['title']}]({item['url']}) — {item['published']}")

    if r.get('market') == 'IN':
        st.subheader('Verified Primary India BSE / NSE Regulatory Filings (SEBI LODR Official Documents)')
    else:
        st.subheader('US SEC EDGAR Filing Evidence (10-K / 10-Q / 8-K)')
    primary_filings = r.get('sec', {}).get('filings', [])
    if not primary_filings:
        st.info('No direct official exchange filing documents retrieved in automated feed.')
    for item in primary_filings:
        st.markdown(f"- [{item['title']}]({item['url']}) — filed/published {item['published']}")

    secondary_mentions = r.get('sec', {}).get('secondary_disclosure_mentions', [])
    if secondary_mentions:
        st.subheader('Company-Specific Secondary Disclosure Mentions (Unverified News — Not Primary Filings)')
        for item in secondary_mentions:
            st.markdown(f"- [{item['title']}]({item['url']}) — published {item['published']}")

    st.subheader('Verified Reported Accounting / Fundamental Facts (never fabricated)')
    st.json(r.get('fundamentals', {}).get('metrics', {}))
    if r.get('fundamentals', {}).get('warning'):
        st.caption(r['fundamentals']['warning'])
    if r.get('missing_data'):
        st.subheader('Missing / Unverified Data (Explicitly Disclosed)')
        for m_item in r['missing_data']:
            st.markdown(f'- ⚠️ {m_item}')
    if r.get('unsupported_conclusions'):
        st.subheader('Unsupported Conclusions (Explicitly Withheld)')
        for u_item in r['unsupported_conclusions']:
            st.markdown(f'- 🚫 {u_item}')
    st.caption('Period lengths and accounting standards (US-GAAP vs IND-AS) differ; these facts do not establish valuation or investability.')
    st.download_button('Download evidence JSON', __import__('json').dumps(r, indent=2), 'research_evidence.json', 'application/json')
    st.divider()
    st.subheader('Optional AI research review')
    if not secret('GROQ_API_KEY'):
        st.info('Set GROQ_API_KEY in Streamlit Secrets to enable AI synthesis. Without it, only sourced evidence is shown.')
    elif st.button('Analyze collected evidence with AI'):
        review = llm_evidence_review(r, secret('GROQ_API_KEY'))
        if review['status'] == 'UNVERIFIED_LLM_SYNTHESIS':
            st.warning('LLM interpretation — verify every claim using primary sources')
            st.write(review['analysis'])
        else:
            st.error(review.get('error', 'AI analysis unavailable'))
