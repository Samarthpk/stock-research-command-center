import importlib
import os
import runpy
import sys
import time
from pathlib import Path
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
import global_markets
if getattr(global_markets, '_GLOBAL_MARKETS_VERSION', None) != '4.1':
    global_markets = importlib.reload(global_markets)
import engine
if getattr(engine, '_ENGINE_VERSION', None) != '4.2':
    engine = importlib.reload(engine)

from engine import (
    init_db,
    watchlist,
    set_watchlist,
    run,
    latest,
    report,
    get_api_key,
    verify_bse_symbol_support,
    research_indian_benchmarks,
    MIN_REQUEST_INTERVAL,
)
from global_markets import (
    MARKET_CHOICES,
    MARKET_CHOICE_TO_CODE,
    INDIA_INSTRUMENTS,
    get_market_schedule_status,
    format_price,
)

st.set_page_config(page_title='Stock Research Command Center', page_icon='📈', layout='wide')
init_db()

if 'is_running' not in st.session_state:
    st.session_state['is_running'] = False
if 'last_run_epoch' not in st.session_state:
    st.session_state['last_run_epoch'] = 0.0

st.title('📈 Stock Research Command Center — Global Edition')
st.caption(
    'Phase 4: Global Autonomous Stock Intelligence (US NYSE/NASDAQ in USD $ • India NSE/BSE in INR ₹) • '
    'daily closing data (not real-time) • no automated trading'
)

with st.sidebar:
    st.header('Navigation & Control')
    qp_page = str(st.query_params.get('page', '')).strip().lower()
    page_options = [
        '📈 Stock Command Center',
        '🛰️ Autonomous Mission Control',
        '🔎 Research Intelligence',
    ]
    page_idx = 1 if 'mission' in qp_page else (2 if 'intel' in qp_page or 'research' in qp_page else 0)
    active_page = st.radio('Workspace Page', options=page_options, index=page_idx)
    st.divider()

if active_page == '🛰️ Autonomous Mission Control':
    runpy.run_path(str(Path(__file__).resolve().parent / 'pages' / 'Autonomous_Mission_Control.py'), run_name='__main__')
    st.stop()
elif active_page == '🔎 Research Intelligence':
    runpy.run_path(str(Path(__file__).resolve().parent / 'pages' / 'Research_Intelligence.py'), run_name='__main__')
    st.stop()

with st.sidebar:
    st.subheader('Market & Watchlist Control')
    qp_mkt = str(st.query_params.get('market', '')).strip().upper()
    default_idx = 1 if qp_mkt == 'US' else (2 if qp_mkt in ('IN', 'INDIA') else 0)
    market_choice = st.radio(
        'Market Selection',
        options=MARKET_CHOICES,
        index=default_idx,
        help='Filter watchlists, market calendars, and research runs by market (All Markets, US, or India).',
    )
    selected_market_code = MARKET_CHOICE_TO_CODE.get(market_choice, 'ALL')

    st.subheader('Watchlists by Market')
    us_watch = watchlist('US')
    us_tickers_input = st.text_area(
        'US Watchlist (NYSE / NASDAQ — USD $)',
        value=', '.join(us_watch),
        height=75,
    )
    if st.button('Save US Watchlist'):
        try:
            set_watchlist(us_tickers_input.split(','), market='US')
            st.success('US Watchlist saved')
            st.rerun()
        except ValueError as e:
            st.error(str(e))

    in_watch = watchlist('IN')
    in_tickers_input = st.text_area(
        'India Watchlist (NSE / BSE — INR ₹)',
        value=', '.join(in_watch),
        height=75,
        help='Enter NSE symbol (e.g., RELIANCE, TCS) or explicit .BSE symbol (e.g., RELIANCE.BSE). NSE and BSE identifiers are mapped distinctly.',
    )
    if st.button('Save India Watchlist'):
        try:
            set_watchlist(in_tickers_input.split(','), market='IN')
            st.success('India Watchlist saved')
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

# Market Calendar, Time Zone & Holiday Status Banner
cal_cols = st.columns(2)
for idx, m_code in enumerate(['US', 'IN']):
    sched = get_market_schedule_status(m_code)
    with cal_cols[idx]:
        status_badge = '🟢 Open' if sched['is_open_now'] else ('🗓️ Holiday' if sched['is_holiday'] else '🕒 Closed')
        st.markdown(
            f"**{sched['market_name']} ({sched['currency']} {sched['currency_symbol']})** — {status_badge}  \n"
            f"Local time: `{sched['local_time']}` (`{sched['timezone']}`, {'DST active' if sched['is_dst'] else 'Standard time'}) • "
            f"Latest completed session: `{sched['latest_completed_session']}`"
            + (f" • Holiday: **{sched['holiday_name']}**" if sched['holiday_name'] else '')
        )

with st.expander('🇮🇳 India Market Instrument Registry (Non-Interchangeable NSE / BSE / ISIN Mapping)'):
    reg_rows = []
    for sym, info in INDIA_INSTRUMENTS.items():
        reg_rows.append({
            'Canonical ID': sym,
            'Company Name': info['name'],
            'NSE Symbol': info['nse_symbol'],
            'BSE Scrip Code': info['bse_code'],
            'Alpha Vantage BSE Ticker': info['bse_symbol'],
            'ISIN': info['isin'],
            'Currency': 'INR (₹)',
        })
    st.dataframe(pd.DataFrame(reg_rows), use_container_width=True, hide_index=True)
    st.caption(
        'NSE symbols and BSE scrip codes are never treated as interchangeable. '
        'Alpha Vantage .BSE historical OHLCV availability is verified explicitly below; if unavailable, symbols are marked UNSUPPORTED.'
    )
    if st.button('Test RELIANCE.BSE Historical OHLCV Support', disabled=not bool(key)):
        with st.spinner('Testing live Alpha Vantage TIME_SERIES_DAILY OHLCV for RELIANCE.BSE...'):
            st.session_state['bse_ohlcv_check'] = verify_bse_symbol_support('RELIANCE', key=key)

if str(st.query_params.get('verify_bse', '')) == '1' and 'bse_ohlcv_check' not in st.session_state:
    st.session_state['bse_ohlcv_check'] = verify_bse_symbol_support('RELIANCE', key=key)
    if key and not latest(market='IN'):
        try:
            st.session_state['latest_run'] = run(symbols=['RELIANCE'], key=key, use_cache=True, market='IN')
            st.session_state['latest_run_market'] = 'IN'
        except Exception:
            pass

if 'bse_ohlcv_check' in st.session_state:
    st.subheader('RELIANCE.BSE Historical OHLCV Verification Result')
    st.json(st.session_state['bse_ohlcv_check'])

st.subheader('Agent desk')
cols = st.columns(4)
for col, name, desc in zip(
    cols,
    ['Market Data Agent', 'Trend Agent', 'Risk Agent', 'Report Agent'],
    [
        f'Paced daily series (≥{MIN_REQUEST_INTERVAL:.1f}s) + SQLite cache (USD & INR)',
        'MA20 / MA50 + 20-day momentum + market ranking',
        'Recent realized annualized volatility',
        'Evidence-backed US, India & Global brief',
    ],
):
    with col:
        st.markdown(f'**{name}**')
        st.caption(desc)

status_container = st.container()

run_clicked = st.button(
    f'▶ Run research agents ({market_choice})',
    type='primary',
    disabled=(not bool(key)) or st.session_state['is_running'],
)

if run_clicked:
    now_epoch = time.time()
    if (
        st.session_state['is_running']
        or (
            now_epoch - st.session_state['last_run_epoch'] < 2.0
            and 'latest_run' in st.session_state
            and st.session_state.get('latest_run_market') == selected_market_code
            and not force_refresh
        )
    ):
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
            with st.spinner(f'Running {market_choice} research agents with rate-limit pacing (≥1.5s/request) and cache checks...'):
                result = run(
                    key=key,
                    use_cache=True,
                    force_refresh=force_refresh,
                    progress_callback=_render_counts,
                    market=selected_market_code,
                )
            st.session_state['latest_run'] = result
            st.session_state['latest_run_market'] = selected_market_code
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

rows = latest(market=selected_market_code)
if rows:
    live_syms_in_session = set()
    if 'latest_run' in st.session_state:
        live_syms_in_session = {x['symbol'] for x in st.session_state['latest_run'].get('live_results', [])}
    for row in rows:
        if row['symbol'] not in live_syms_in_session and row.get('data_origin') == 'live':
            row['data_origin'] = 'cached'
            if not row.get('stale'):
                ts_short = (row.get('fetched_at_utc') or '')[:19].replace('T', ' ') + ' UTC'
                row['freshness'] = f'Cached ({ts_short.strip()})'
        row['formatted_close'] = format_price(row.get('close'), row.get('currency', 'USD'))

    stale_symbols = [r['symbol'] for r in rows if r.get('stale')]
    st.subheader(f'Watchlist signals ({market_choice}) — latest stored session (historical daily close, not live quotes)')
    if stale_symbols:
        st.warning(
            f"⚠️ Stale historical data detected for: {', '.join(stale_symbols)}. "
            "Check 'Market Close (As-Of)' and 'Cached / Fetched At (UTC)' before interpreting signals."
        )

    frame = pd.DataFrame(rows)
    display_cols = [
        'symbol', 'market', 'exchange', 'currency', 'asof', 'freshness',
        'fetched_at_utc', 'formatted_close', 'change20', 'ma20', 'ma50', 'volatility', 'ranking', 'stance',
    ]
    for col_name in display_cols:
        if col_name not in frame.columns:
            frame[col_name] = ''

    st.dataframe(
        frame[display_cols],
        use_container_width=True,
        hide_index=True,
        column_config={
            'market': st.column_config.TextColumn('Market'),
            'exchange': st.column_config.TextColumn('Exchange'),
            'currency': st.column_config.TextColumn('Currency'),
            'asof': st.column_config.TextColumn('Market Close (As-Of)'),
            'freshness': st.column_config.TextColumn('Data Status / Freshness'),
            'fetched_at_utc': st.column_config.TextColumn('Cached / Fetched At (UTC)'),
            'formatted_close': st.column_config.TextColumn('Close (Native Currency)'),
            'change20': st.column_config.NumberColumn('20-session return %', format='%.2f%%'),
            'volatility': st.column_config.NumberColumn('Annualized vol %', format='%.1f%%'),
            'ranking': st.column_config.TextColumn('Research Ranking'),
        },
    )
    st.caption(
        'Signal labels and research rankings (INVESTIGATE / WATCH / CAUTION / INSUFFICIENT_EVIDENCE) describe historical '
        'end-of-day trends in native currency (USD $ for US, INR ₹ for India), not live market prices or buy/sell recommendations.'
    )
    for r in rows:
        badge = '⚠️ STALE' if r.get('stale') else ('🟢 LIVE FETCH' if r['symbol'] in live_syms_in_session else '📦 CACHED')
        price_str = format_price(r.get('close'), r.get('currency', 'USD'))
        ranking_label = r.get('ranking', 'WATCH')
        with st.expander(f"[{r.get('market', 'US')}] {r['symbol']} ({r.get('currency', 'USD')}) — {ranking_label} / {r['stance']} — Close {price_str} as of {r['asof']} [{badge}]"):
            if r.get('data_discrepancy'):
                st.error(
                    f"⚠️ Unresolved Price Discrepancy for {r['asof']}: cached close {r['data_discrepancy'].get('previous_close')} "
                    f"vs observed close {r['data_discrepancy'].get('observed_close')}. Existing evidence is preserved and ranking is withheld."
                )
            st.write(r['rationale'])
            st.caption(
                f"Exchange: {r.get('exchange', 'NYSE/NASDAQ')} • Provider Symbol: {r.get('provider_symbol', r['symbol'])} • "
                f"Currency: {r.get('currency', 'USD')} • Basis: {r.get('price_basis', 'UNADJUSTED_DAILY_OHLCV')} • "
                f"Status: {r.get('freshness', 'Cached')} • Stored timestamp (UTC): {r.get('fetched_at_utc', 'unknown')}"
            )
            st.markdown(f"[Data-method documentation]({r['source']})")
else:
    st.info(f'No verified market-data results stored yet for {market_choice}. Configure an API key, then click Run research agents.')

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
