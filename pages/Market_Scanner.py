"""Full market screening and provider-entitled quote/validated-signal desk."""
import json
import os
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import pandas as pd
import streamlit as st
from listed_universe import load_directory
from market_scanner import validate_bulk_upload,screen_bulk
from market_quotes import licensed_gateway_quote
from signal_validation import evaluate_signal

st.title("🌎 Full-Market Scanner & Signal Validation")
st.caption("US + NSE/BSE official listings. Scanner requires bulk OHLCV evidence; verified realtime quotes require a licensed timestamped feed. Screening scores and historical backtests are not trading recommendations.")

@st.cache_data(ttl=86400,show_spinner="Loading official exchange directories")
def catalog():
    return load_directory()

universe=catalog()
for label,source in universe["sources"].items():
    if source["status"]!="OK":
        st.warning(f"{label}: {source['status']} — universe incomplete.")
st.metric("Exchange listings currently loaded",f"{len(universe['rows']):,}")
st.caption("US exchange directory sources and NSE/BSE active equity sources are attempted separately. Missing exchange sources are not counted as screened.")

market=st.radio("Select exchange market",["ALL","US","IN"],horizontal=True,format_func=lambda x:{"ALL":"Both markets","US":"US","IN":"India NSE+BSE"}[x])
st.markdown("### 1. Supply bulk historical OHLCV")
st.caption("No provider data? You can inspect the universe, but there will be no fabricated rankings. Upload legally sourced UTF-8 CSV with market,exchange,symbol,date,open,high,low,close,volume. Exchange and symbol must match official listings exactly; at least 60 valid daily bars per stock.")
uploaded=st.file_uploader("Upload approved bulk daily OHLCV data (up to 100MB)",type=["csv"],accept_multiple_files=False)
st.caption("Uploaded data stays in this app's temporary session. Do not upload confidential, personally identifiable, or redistribution-restricted data.")
if uploaded is not None and st.button("Screen ALL eligible uploaded stocks",type="primary"):
    try:
        raw=uploaded.getvalue()
        dataset=validate_bulk_upload(raw,universe["rows"])
        scan=screen_bulk(dataset,universe["rows"],market=market)
        st.session_state["scan_result"]=scan
        st.session_state["scan_dataset"]=dataset
    except (ValueError,TypeError,KeyError) as exc:
        st.error(f"Scan rejected: {exc}")
        st.session_state.pop("scan_result",None)
        st.session_state.pop("scan_dataset",None)

result=st.session_state.get("scan_result")
if result:
    st.markdown("### 2. Universe screening results")
    x,y,z=st.columns(3)
    x.metric("Eligible listed identifiers",f"{result['universe_count']:,}")
    y.metric("Actually analyzed",f"{result['analyzed']:,}")
    z.metric("Universe coverage",f"{result['coverage_pct']:.2f}%")
    st.write("**Scanner status:**",result["status"])
    st.caption(f"Unavailable or insufficient histories: {result['uncovered']:,}. Stale excluded: {result['stale_excluded']:,}; insufficient-bar excluded: {result['short_or_invalid_excluded']:,}.")
    if result["status"]!="ALL_LISTED_IDENTIFIERS_ANALYZED" or not universe["complete_coverage"]:
        st.warning("This is NOT a complete full-market analysis. Only verified listing identifiers with user-provided acceptable OHLCV were evaluated.")
    if result["ranked"]:
        df=pd.DataFrame(result["ranked"])
        st.dataframe(df,hide_index=True,use_container_width=True,height=450)
        st.download_button("Download screened companies CSV",df.to_csv(index=False),file_name="market_screen.csv",mime="text/csv")
        st.markdown("### 3. Walk-forward historical signal validation")
        choices={f"{r['company']} | {r['market']}:{r['exchange']}:{r['symbol']}":(r["market"],r["exchange"],r["symbol"]) for r in result["ranked"]}
        selected=st.selectbox("Choose screened issuer for backtest",list(choices))
        key=choices[selected]
        bars=st.session_state["scan_dataset"]["series"].get(key,[])
        if len(bars)<141:
            st.warning("Backtest needs at least 141 daily sessions (80 warmup + 60 holdout + 1). Add historical data for this issuer.")
        else:
            analysis=evaluate_signal(bars)
            st.write("**Latest strategy status:**",analysis["screening_signal"])
            st.caption(analysis["reason"])
            setup=analysis["latest_setup"]
            if setup["status"]=="CANDIDATE":
                st.write("**Historical conditional setup:**",setup["signal_as_of"])
                st.write(f"ATR risk/reward heuristic: {setup['estimated_rr']}:1, based on latest close {setup['signal_close']:.4f}. Actual future execution prices may differ.")
            backtest=analysis["backtest"]
            st.write("**Holdout validation:**",backtest["status"])
            st.write("**Number of evaluated trades:**",backtest["trades"],"— minimum required:",backtest["min_trades"])
            if backtest["status"] in ("VALIDATED_HOLDOUT","FAILED_HOLDOUT"):
                a,b,c=st.columns(3)
                a.metric("Holdout win rate",f"{backtest['win_rate_pct']:.1f}%")
                b.metric("Avg net trade return",f"{backtest['average_net_return_pct']:.2f}%")
                c.metric("Backtested trade equity drawdown",f"{backtest['max_trade_equity_drawdown_pct']:.1f}%")
            st.caption(backtest.get("limitations","Trade sample too small for a supported signal."))
            st.download_button("Download strategy validation JSON",json.dumps(analysis,indent=2),file_name="strategy_validation.json",mime="application/json")
    else:
        st.info("No instrument passed price-coverage, freshness and minimum-history requirements.")

st.divider()
st.markdown("### 4. Exchange-entitled realtime quote verification")
st.caption("A separate licensed quote gateway is required. Free Alpha Vantage EOD prices are NOT realtime.")
if not result or not result["ranked"]:
    st.info("Screen at least one validated issuer above before requesting its realtime quote.")
else:
    issuer_opts={f"{r['market']}:{r['exchange']}:{r['symbol']}":r for r in result["ranked"]}
    quote_choice=st.selectbox("Issuer for realtime quote",list(issuer_opts))
    issuer=issuer_opts[quote_choice]
    try:
        endpoint=st.secrets.get("LICENSED_QUOTES_URL","")
        token=st.secrets.get("LICENSED_QUOTES_TOKEN","")
    except Exception:
        endpoint=""
        token=""
    endpoint=endpoint or os.getenv("LICENSED_QUOTES_URL","")
    token=token or os.getenv("LICENSED_QUOTES_TOKEN","")
    if not endpoint or not token:
        st.warning("Licensed quote provider not configured. Add LICENSED_QUOTES_URL and LICENSED_QUOTES_TOKEN to Streamlit Secrets to enable verified realtime quotes.")
    elif st.button("Verify fresh provider quote"):
        response=licensed_gateway_quote(issuer,endpoint=endpoint,token=token)
        if response["status"]=="OK":
            q=response["quote"]
            st.metric("Verified realtime quote",f"{q['currency']} {q['price']:,.2f}")
            st.caption(f"Provider: {q['source']} | Exchange: {q['exchange']} | Timestamp: {q['as_of']} | Age: {q['seconds_old']} seconds")
        else:
            st.warning(f"Realtime evidence not verified: {response['status']}. No quote displayed as live.")
st.info("No licensed feed connected → all-stock daily scan and real-time price availability remain blocked by data coverage or entitlement, not missing AI prompts.")
