"""Unified issuer-validated stock research dashboard (historical daily data).

All data pulled on explicit click. Cache source listings 24h and historical
provider results 12h to respect provider quotas. Never claim real-time data.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
import streamlit as st
from listed_universe import load_directory,search_directory
from unified_research import resolve_from_directory,provider_candidates,historical_ohlcv,calculate_technicals

st.title("📊 Unified Stock Research — US + India")
st.caption("Issuer-verified official exchange listings + historical daily OHLCV technical research. No real-time price, automatic trades, guaranteed predictions or investment recommendations.")

@st.cache_data(ttl=86400,show_spinner="Loading official exchange directories")
def directory():
    return load_directory()

@st.cache_data(ttl=12*3600,show_spinner="Fetching daily historical OHLCV from provider")
def daily_series(provider_symbol,key):
    # Explicit click only; this function does not run on page load.
    url="https://www.alphavantage.co/query?"+urllib.parse.urlencode({
        "function":"TIME_SERIES_DAILY","symbol":provider_symbol,
        "outputsize":"compact","apikey":key
    })
    req=urllib.request.Request(url,headers={"User-Agent":"StockResearchCommandCenter/0.6 (historical analysis)"})
    with urllib.request.urlopen(req,timeout=25) as resp:
        payload=json.loads(resp.read(2_000_000))
    if "Note" in payload or "Information" in payload:
        raise ValueError("Provider quota/rate-limit response; try after quota resets.")
    if "Error Message" in payload:
        raise ValueError("No price series available for this exchange symbol from configured provider.")
    return historical_ohlcv(payload)

catalog=directory()
for name,state in catalog["sources"].items():
    if state["status"]!="OK":
        st.warning(f"{name}: {state['status']}. Directory coverage incomplete.")
if not catalog["rows"]:
    st.error("No official exchange directory is available. Price lookup disabled; check source availability.")
    st.stop()
market=st.radio("Market",["US","IN"],horizontal=True,format_func=lambda x:"🇺🇸 US" if x=="US" else "🇮🇳 India NSE/BSE")
rows=[r for r in catalog["rows"] if r["market"]==market]
if not rows:
    st.warning("No listings loaded for this market.")
    st.stop()
query=st.text_input("Search company name / exact symbol / ISIN",placeholder="AAPL, Microsoft, Suzlon, TCL, RELIANCE")
if not query.strip():
    st.info("Enter a ticker or company name to retrieve a verified matching issuer.")
    st.stop()
found=search_directory(rows,query,market=market,limit=50)
if not found["rows"]:
    st.warning("No matching issuer in official directory. Research blocked to prevent wrong-company analysis.")
    st.stop()
if found["total"]>50:
    st.info("Many matches: narrow the search to a more precise symbol or company name.")
options={f"{r['name']} — {r['exchange']}:{r['symbol']}"+(f" ({r.get('trading_symbol')})" if r.get("trading_symbol") else ""):i for i,r in enumerate(found["rows"])}
selected=st.selectbox("Select the exact listed company and exchange",list(options))
issuer=found["rows"][options[selected]]
st.write("**Verified directory issuer:**",issuer["name"]," | **Exchange:**",issuer["exchange"]," | **ID:**",issuer["symbol"]," | **ISIN:**",issuer.get("isin") or "Not supplied")
st.caption(f"Official listing source: {issuer['source']} (listing verification, not a guarantee of current trading status).")

candidates=provider_candidates(issuer)
if not candidates:
    st.warning("No validated Alpha Vantage provider-symbol mapping for this exchange listing. Price and technical metrics withheld — will not substitute an unrelated ticker.")
    st.info("For NSE-only listings, provider mapping to BSE must be verified from matching ISIN before fetching BSE prices. Full-universe bulk pricing requires a licensed provider.")
    st.stop()
if len(candidates)>1:
    st.caption("BSE trading-symbol and numeric scrip-code candidates are linked to the same selected BSE issuer directory record. Failed candidates do not produce data.")

try:
    key=st.secrets.get("ALPHAVANTAGE_API_KEY") or os.getenv("ALPHAVANTAGE_API_KEY")
except Exception:
    key=os.getenv("ALPHAVANTAGE_API_KEY")
if not key:
    st.error("ALPHAVANTAGE_API_KEY not configured in this Streamlit deployment. GitHub Actions secrets do not automatically carry over.")
    st.stop()
if st.button("Fetch verified historical price + analyze",type="primary"):
    errors=[]
    with st.spinner("Fetching historical price evidence with strict issuer identity"):
        for candidate in candidates:
            try:
                series=daily_series(candidate,key)
                report=calculate_technicals(series)
                report["source_ticker"]=candidate
                report["source_url"]="https://www.alphavantage.co/documentation/#daily"
                report["company"]=issuer["name"]
                report["exchange"]=issuer["exchange"]
                report["market"]=market
                report["currency"]="USD" if market=="US" else "INR"
                st.session_state["unified_report"]={"issuer":issuer,"report":report}
                errors=[]
                break
            except Exception as exc:
                errors.append(f"{candidate}: {type(exc).__name__}: {str(exc)}")
        if errors:
            st.session_state.pop("unified_report",None)
            st.error("Data unavailable for verified issuer. No substitute stock selected.")
            for err in errors:st.caption(err)
current=st.session_state.get("unified_report")
if current and (current["issuer"]["symbol"],current["issuer"]["exchange"],current["issuer"]["market"])==(issuer["symbol"],issuer["exchange"],issuer["market"]):
    d=current["report"]
    st.subheader(f"Historical technical research — {d['company']}")
    st.caption(f"Close as of {d['as_of']} | {d['source_ticker']} | {d['source_url']} | Prices are historical, not live.")
    sym="$" if market=="US" else "₹"
    a,b,c,dcol=st.columns(4)
    a.metric("Last verified close",f"{sym}{d['close']:,.2f}")
    b.metric("Last reported daily volume",f"{d['volume']:,}")
    c.metric("Historical risk level",d["risk_level"])
    dcol.metric("Annualized volatility",f"{d['annualized_volatility_pct']:.1f}%")
    metrics=pd.DataFrame([{"MA20":d["ma20"],"MA50":d["ma50"],"RSI14":d["rsi14"],"MACD":d["macd"],"MACD signal":d["macd_signal"],"ATR14":d["atr14"]}])
    st.markdown("### Technical indicators")
    st.dataframe(metrics,hide_index=True,use_container_width=True)
    a,b,c=st.columns(3)
    a.metric("20-day low (historical support screen)",f"{sym}{d['historical_20d_low_support']:,.2f}")
    b.metric("20-day high (historical resistance screen)",f"{sym}{d['historical_20d_high_resistance']:,.2f}")
    c.metric("Illustrative reward/risk",str(d["illustrative_reward_risk"]) if d["illustrative_reward_risk"] is not None else "N/A")
    st.caption(f"Example only: entry at historical close {sym}{d['close']:,.2f}, hypothetical downside boundary {sym}{d['illustrative_stop_level']:,.2f}, upper 20-session price bound {sym}{d['historical_20d_high_resistance']:,.2f}. Not recommended entry/exit/stop/target.")
    st.markdown("### Historical positive and negative signals")
    for observation in d["observations"]:st.write("•",observation)
    st.warning("Why invest / avoid is NOT established: no verified up-to-date issuer financial statements, valuation, cash-flow, leverage or governance facts are supplied by this price-only module. Do not use a positive technical screen as an investment recommendation.")
    st.json({"issuer":current["issuer"],"technical_research":d},expanded=False)
    st.download_button("Export technical research JSON",json.dumps({"issuer":current["issuer"],"technical_research":d},indent=2),file_name="technical_research.json",mime="application/json")
