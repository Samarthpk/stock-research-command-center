"""Read-only free Upstox India research dashboard."""
import os
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
import streamlit as st
from upstox_data import load_instruments,search_instruments,fetch_quote,fetch_daily,UpstoxDataError
from unified_research import calculate_technicals
from signal_validation import evaluate_signal

st.title("🇮🇳 Upstox Free Live Quotes & Research")
st.caption("NSE/BSE research using Upstox read-only Analytics Token, with live-freshness and historical evidence validation. No orders. No guaranteed buy or sell recommendations.")
def analytics_token():
    try:
        return st.secrets.get("UPSTOX_ANALYTICS_TOKEN") or os.getenv("UPSTOX_ANALYTICS_TOKEN","")
    except Exception:
        return os.getenv("UPSTOX_ANALYTICS_TOKEN","")

@st.cache_data(ttl=86400,show_spinner="Loading Upstox NSE/BSE instrument masters")
def directory():
    return load_instruments()

@st.cache_data(ttl=900,show_spinner="Fetching Upstox historical OHLCV")
def daily(key,token):
    issuer={"instrument_key":key}
    return fetch_daily(issuer,token,days=400)

catalog=directory()
cols=st.columns(2)
for col,name in zip(cols,("NSE","BSE")):
    item=catalog["source_health"].get(name,{})
    col.metric(name+" instruments",item.get("count",0))
    if item.get("status")!="OK":
        col.warning(name+" instrument master unavailable: "+item.get("reason","unknown"))
if not catalog["rows"]:
    st.error("Upstox instrument directory unavailable. No issuer matching or prices can be shown.")
    st.stop()

q=st.text_input("Search Indian listed company / ticker / ISIN",placeholder="SUZLON, ITC, TCL, RELIANCE")
exchange=st.radio("Exchange",["ALL","NSE","BSE"],horizontal=True)
if not q.strip():
    st.info("Enter a company name or ticker. Upstox instrument master resolves the correct listed issuer without per-stock hardcoding.")
    st.stop()
matches=search_instruments(catalog["rows"],q,exchange)
if not matches:
    st.warning("No matching instrument available from Upstox NSE/BSE master.")
    st.stop()
choices={f"{r['name']} — {r['exchange']}:{r['symbol']} ({r['isin']})":i for i,r in enumerate(matches)}
selected=st.selectbox("Choose exact listed company",list(choices))
issuer=matches[choices[selected]]
st.write("**Selected:**",issuer["name"]," | **Exchange:**",issuer["exchange"]," | **ISIN:**",issuer["isin"])
st.caption("Verified instrument key: "+issuer["instrument_key"]+" | Source: "+issuer["source"])
token=analytics_token()
if not token:
    st.error("UPSTOX_ANALYTICS_TOKEN missing from this Streamlit app's Secrets (not GitHub Secrets).")
    st.stop()
a,b=st.columns(2)
with a:
    get_quote=st.button("Fetch latest NSE/BSE quote",type="primary")
with b:
    get_history=st.button("Analyze 400-day history")
id_key=issuer["instrument_key"]
if get_quote:
    try:
        qresult=fetch_quote(issuer,token)
        st.session_state["upstox_quote"]={"key":id_key,"result":qresult}
    except UpstoxDataError as exc:
        st.session_state.pop("upstox_quote",None)
        st.error("Upstox quote unavailable: "+str(exc))
quote=st.session_state.get("upstox_quote")
if quote and quote["key"]==id_key:
    current=quote["result"]
    st.subheader("Market quote")
    x,y,z=st.columns(3)
    x.metric("Reported last traded price",f"₹{current['price']:,.2f}")
    y.metric("Reported traded volume",f"{current['volume']:,}")
    z.metric("Freshness",current["quote_status"])
    st.caption("As-of "+current["timestamp"]+" | "+current["source"]+". Fresh snapshot is NOT a guaranteed streaming execution quote; outside market hours it is last reported.")
if get_history:
    try:
        series=daily(id_key,token)
        indicators=calculate_technicals(series)
        signal=evaluate_signal(series,holdout=60,min_trades=10) if len(series)>=141 else {"screening_signal":"NOT_ENOUGH_HISTORY_FOR_BACKTEST"}
        st.session_state["upstox_analysis"]={"key":id_key,"indicators":indicators,"signal":signal}
    except (UpstoxDataError,ValueError,KeyError,TypeError,ZeroDivisionError) as exc:
        st.session_state.pop("upstox_analysis",None)
        st.error("Historical analysis unavailable: "+str(exc))
report=st.session_state.get("upstox_analysis")
if report and report["key"]==id_key:
    d=report["indicators"]
    st.subheader("Technical indicators — historical daily candles")
    st.caption("Daily history as-of "+d["as_of"]+"; technical signals are not company financial statements.")
    c1,c2,c3,c4=st.columns(4)
    c1.metric("Historical close",f"₹{d['close']:,.2f}")
    c2.metric("RSI14",f"{d['rsi14']:.1f}")
    c3.metric("ATR14",f"{d['atr14']:.2f}")
    c4.metric("Historical volatility risk",d["risk_level"])
    st.dataframe(pd.DataFrame([{"MA20":d["ma20"],"MA50":d["ma50"],"MACD":d["macd"],"MACD signal":d["macd_signal"],"Reward/risk example":d["illustrative_reward_risk"]}]),hide_index=True,use_container_width=True)
    left,right=st.columns(2)
    with left:
        st.markdown("**Technical positive signals**")
        for p in d["technical_bull_factors"]:st.write("•",p)
    with right:
        st.markdown("**Technical risks/limitations**")
        for p in d["technical_bear_factors"]:st.write("•",p)
    st.write("**Backtest signal status:**",report["signal"]["screening_signal"])
    bt=report["signal"].get("backtest",{})
    st.caption("Holdout status: "+bt.get("status","not available")+"; trades "+str(bt.get("trades",0))+". Backtested setups are not verified buy/sell recommendations or future return guarantees.")
    st.warning("Investment fundamentals, audited company filings, fair-value targets, and personal suitability not validated by this Upstox price-only page.")
st.info("Upstox Analytics Token is free and read-only. Respect broker rate limits, personal-use data rights and market-data redistribution restrictions; do not expose token or broker data publicly without permission.")
