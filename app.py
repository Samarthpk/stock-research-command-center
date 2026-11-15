import os
import sys
from pathlib import Path
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import init_db,watchlist,set_watchlist,run,latest,report,get_api_key

st.set_page_config(page_title='Stock Research Command Center',page_icon='📈',layout='wide')
init_db()
st.title('📈 Stock Research Command Center')
st.caption('Research prototype • daily closing data (not real-time) • no automated trading')
with st.sidebar:
    st.header('Control Panel')
    watch=watchlist()
    tickers=st.text_area('Watchlist (comma separated)',value=', '.join(watch),height=100)
    if st.button('Save watchlist'):
        try:
            set_watchlist(tickers.split(','));st.success('Watchlist saved');st.rerun()
        except ValueError as e:st.error(str(e))
    key=get_api_key()
    st.write('Market data API:', 'Configured ✅' if key else 'Missing key ⚠️')
    st.caption('Get a key at alphavantage.co/support; configure it in Streamlit Secrets or an environment variable, not source code.')

st.subheader('Agent desk')
cols=st.columns(4)
for col,name,desc in zip(cols,['Market Data Agent','Trend Agent','Risk Agent','Report Agent'],['Fetches daily price series','MA20 / MA50 + momentum','Recent realized volatility','Evidence-backed daily brief']):
    with col:
        st.markdown(f'**{name}**')
        st.caption(desc)

if st.button('▶ Run research agents',type='primary',disabled=not bool(key)):
    with st.spinner('Fetching and analyzing live API responses...'):
        result=run(key=key)
    st.session_state['latest_run']=result
    if result['errors']:st.warning('Some agents could not get valid data; failed symbols are not scored.')
    if result['results']:st.success(f"Fetched {len(result['results'])} ticker(s) successfully")

rows=latest()
if rows:
    st.subheader('Watchlist signals — latest available trading session')
    frame=pd.DataFrame(rows)
    st.dataframe(frame[['symbol','asof','close','change20','ma20','ma50','volatility','stance']],use_container_width=True,hide_index=True,
    column_config={'change20':st.column_config.NumberColumn('20-session return %',format='%.2f%%'),'volatility':st.column_config.NumberColumn('Annualized vol %',format='%.1f%%')})
    st.caption('Signal labels describe historical technical trends. They are not buy/sell calls. Historical stored results may be stale; check the as-of date.')
    for r in rows:
        with st.expander(f"{r['symbol']}: {r['stance']} — {r['asof']}"):
            st.write(r['rationale']);st.markdown(f"[Data-method documentation]({r['source']})")
else:
    st.info('No verified market-data results yet. Configure an API key, then click Run research agents.')

if 'latest_run' in st.session_state:
    r=st.session_state['latest_run']
    if r['errors']:
        st.subheader('Live fetch errors')
        st.json(r['errors'])
    content=report(r)
    st.download_button('Download latest research brief (.md)',content,file_name='stock_research_brief.md',mime='text/markdown')

st.divider()
st.info('Free Alpha Vantage access is typically limited to 25 requests/day and end-of-day data. A six-stock daily refresh uses up to six requests. Market headlines, filings, valuation, live intraday monitoring, and AI-written theses are future integrations, not implemented agents in this prototype.')
