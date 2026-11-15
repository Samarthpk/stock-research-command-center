import importlib
import os
import sys
import time
from pathlib import Path
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine
if getattr(engine, '_ENGINE_VERSION', None) != '2':
    engine = importlib.reload(engine)
from engine import (
    init_db,
    watchlist,
    set_watchlist,
    run,
    latest,
    report,
    get_api_key,
    MIN_REQUEST_INTERVAL,
)

st.set_page_config(page_title='Stock Research Command Center', page_icon='📈', layout='wide')
init_db()

if 'is_running' not in st.session_state:
    st.session_state['is_running'] = False
if 'last_run_epoch' not in st.session_state:
    st.session_state['last_run_epoch'] = 0.0

st.title('📈 Stock Research Command Center')
st.caption('Research prototype • daily closing data (not real-time) • no automated trading')

with st.sidebar:
    st.header('Control Panel')
    watch = watchlist()
    tickers = st.text_area('Watchlist (comma separated)', value=', '.join(watch), height=100)
    if st.button('Save watchlist'):
        try:
            set_watchlist(tickers.split(','))
            st.success('Watchlist saved')
            st.rerun()
        except ValueError as e:
            st.error(str(e))
    key = get_api_key()
    st.write('Market data API:', 'Configured ✅' if key else 'Missing key ⚠️')
    st.caption(
        f'Pacing: ≥{MIN_REQUEST_INTERVAL:.1f}s between live requests • '
        'Configure ALPHAVANTAGE_API_KEY in Streamlit Secrets or environment variable.'
    )
    force_refresh = st.checkbox(
        'Bypass cache (force live API calls)',
        value=False,
        help='Leave unchecked so repeated clicks reuse cached daily series and preserve your 25/day quota.',
    )

st.subheader('Agent desk')
cols = st.columns(4)
for col, name, desc in zip(
    cols,
    ['Market Data Agent', 'Trend Agent', 'Risk Agent', 'Report Agent'],
    [
        f'Paced daily series (≥{MIN_REQUEST_INTERVAL:.1f}s) + SQLite cache',
        'MA20 / MA50 + 20-day momentum',
        'Recent realized annualized volatility',
        'Evidence-backed daily brief',
    ],
):
    with col:
        st.markdown(f'**{name}**')
        st.caption(desc)

status_container = st.container()

run_clicked = st.button(
    '▶ Run research agents',
    type='primary',
    disabled=(not bool(key)) or st.session_state['is_running'],
)

if run_clicked:
    now_epoch = time.time()
    # Guard against accidental rapid double-clicks
    if st.session_state['is_running'] or (now_epoch - st.session_state['last_run_epoch'] < 2.0 and 'latest_run' in st.session_state and not force_refresh):
        st.info('Using recent run results to prevent duplicate API calls.')
    else:
        st.session_state['is_running'] = True
        progress_placeholder = status_container.empty()

        def _render_counts(counts):
            with progress_placeholder.container():
                m1, m2, m3, m4 = st.columns(4)
                m1.metric('Live Fetched (Success)', counts.get('success', 0))
                m2.metric('Served from Cache', counts.get('cached', 0))
                m3.metric('Failed', counts.get('failed', 0))
                m4.metric('Pending', counts.get('pending', 0))

        try:
            with st.spinner('Running research agents with rate-limit pacing (≥1.5s/request) and cache checks...'):
                result = run(
                    key=key,
                    use_cache=True,
                    force_refresh=force_refresh,
                    progress_callback=_render_counts,
                )
            st.session_state['latest_run'] = result
            st.session_state['last_run_epoch'] = time.time()
        finally:
            st.session_state['is_running'] = False

        progress_placeholder.empty()

if 'latest_run' in st.session_state:
    r = st.session_state['latest_run']
    counts = r.get('counts', {
        'success': len(r.get('live_results', r.get('results', []))),
        'cached': len(r.get('cached_results', [])),
        'failed': len(r.get('errors', [])),
        'pending': len(r.get('pending', [])),
    })
    st.subheader('Latest Run Status')
    c1, c2, c3, c4 = st.columns(4)
    c1.metric('Live Fetched (Success)', counts['success'])
    c2.metric('Served from Cache', counts['cached'])
    c3.metric('Failed', counts['failed'])
    c4.metric('Pending', counts['pending'])

    if r.get('quota_exhausted'):
        st.error(
            'Daily Alpha Vantage quota confirmed exhausted. '
            'Retries were stopped immediately to avoid unnecessary requests. '
            'Previously fetched and cached symbols remain preserved below.'
        )
    elif r.get('quota_uncertain'):
        st.warning(
            'Rate-limit response persisted after conservative bounded backoff. '
            'Because Alpha Vantage uses a generic rate-limit notice without reporting remaining call counts, '
            'remaining daily quota is uncertain (none is invented). Successful and cached symbols remain preserved below.'
        )
    elif r['errors']:
        st.warning('Some symbols could not be fetched; successful and cached symbols are preserved below. Failed symbols are never scored or fabricated.')

    if r['results']:
        st.success(
            f"Available signals: {len(r['results'])} ticker(s) "
            f"({counts['success']} live API fetch, {counts['cached']} from local cache)."
        )

rows = latest()
if rows:
    # Mark rows not fetched live in the current session as cached so historical DB rows are never misrepresented as live
    live_syms_in_session = set()
    if 'latest_run' in st.session_state:
        live_syms_in_session = {x['symbol'] for x in st.session_state['latest_run'].get('live_results', [])}
    for row in rows:
        if row['symbol'] not in live_syms_in_session and row.get('data_origin') == 'live':
            row['data_origin'] = 'cached'
            if not row.get('stale'):
                ts_short = (row.get('fetched_at_utc') or '')[:19].replace('T', ' ') + ' UTC'
                row['freshness'] = f'Cached ({ts_short.strip()})'

    stale_symbols = [r['symbol'] for r in rows if r.get('stale')]
    st.subheader('Watchlist signals — latest stored session (historical daily close, not live quotes)')
    if stale_symbols:
        st.warning(
            f"⚠️ Stale historical data detected for: {', '.join(stale_symbols)}. "
            "Check 'Market Close (As-Of)' and 'Cached / Fetched At (UTC)' before interpreting signals."
        )

    frame = pd.DataFrame(rows)
    display_cols = ['symbol', 'asof', 'freshness', 'fetched_at_utc', 'close', 'change20', 'ma20', 'ma50', 'volatility', 'stance']
    for col_name in display_cols:
        if col_name not in frame.columns:
            frame[col_name] = ''

    st.dataframe(
        frame[display_cols],
        use_container_width=True,
        hide_index=True,
        column_config={
            'asof': st.column_config.TextColumn('Market Close (As-Of)'),
            'freshness': st.column_config.TextColumn('Data Status / Freshness'),
            'fetched_at_utc': st.column_config.TextColumn('Cached / Fetched At (UTC)'),
            'change20': st.column_config.NumberColumn('20-session return %', format='%.2f%%'),
            'volatility': st.column_config.NumberColumn('Annualized vol %', format='%.1f%%'),
        },
    )
    st.caption(
        'Signal labels describe historical end-of-day technical trends, not live market prices or buy/sell recommendations. '
        'Cached and stale entries are explicitly labeled with their market close date and UTC fetch timestamp.'
    )
    for r in rows:
        badge = '⚠️ STALE' if r.get('stale') else ('🟢 LIVE FETCH' if r['symbol'] in live_syms_in_session else '📦 CACHED')
        with st.expander(f"{r['symbol']}: {r['stance']} — Close as of {r['asof']} [{badge}]"):
            st.write(r['rationale'])
            st.caption(f"Status: {r.get('freshness', 'Cached')} • Stored timestamp (UTC): {r.get('fetched_at_utc', 'unknown')}")
            st.markdown(f"[Data-method documentation]({r['source']})")
else:
    st.info('No verified market-data results stored yet. Configure an API key, then click Run research agents.')

if 'latest_run' in st.session_state:
    r = st.session_state['latest_run']
    if r['errors']:
        st.subheader('Fetch errors (no signals generated for failed symbols)')
        st.json(r['errors'])
    if r.get('pending'):
        st.subheader('Pending / skipped symbols (daily quota exhausted)')
        st.write(', '.join(r['pending']))
    content = report(r)
    st.download_button(
        'Download latest research brief (.md)',
        content,
        file_name='stock_research_brief.md',
        mime='text/markdown',
    )

st.divider()
st.info(
    'Free Alpha Vantage access is limited to 1 request/second (paced here at ≥1.5s/request) and 25 requests/day. '
    'Daily series are cached locally in SQLite so repeated dashboard reruns do not consume your daily quota.'
)
