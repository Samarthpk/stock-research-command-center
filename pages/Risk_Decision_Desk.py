"""Phase 5 initial research decision desk. Passive: reads persisted public market reports only."""
from pathlib import Path
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamlit as st
from risk_decision import assess_record, position_scenario

st.title("🧭 Phase 5 — Risk & Decision Desk")
st.caption("Validated historical risk diagnostics and scenario calculations — not live quotes, probability forecasts, or buy/sell instructions. No automated trading.")

reports = Path(__file__).resolve().parents[1] / "reports" / "autonomous"
scope = st.radio("Research market", ["US", "India"], horizontal=True)
file = reports / ("us_latest.json" if scope == "US" else "india_latest.json")
if not file.exists():
    st.warning("No persisted research report available for this market.")
    st.stop()
try:
    mission = json.loads(file.read_text(encoding="utf-8"))
except (OSError, ValueError):
    st.error("Research report unavailable or malformed.")
    st.stop()
st.caption(f"Report ID: {mission.get('mission_id', 'unknown')} | Completed: {mission.get('finished_at', 'unknown')} | Mission: {mission.get('status', 'unknown')}")
results = mission.get("results") or []
if not results:
    st.warning("This research report contains no stocks.")
    st.stop()
options = {f"{r.get('ticker','?')} — {r.get('company',r.get('ticker','?'))}": i for i, r in enumerate(results)}
choice = st.selectbox("Select researched stock", list(options))
record = results[options[choice]]
risk = assess_record(record)
st.subheader(f"{risk['ticker']} — Risk & Evidence")
c1,c2,c3 = st.columns(3)
c1.metric("Risk classification", risk["risk_level"])
c2.metric("Decision", risk["decision"])
c3.metric("Data as of", str(risk["as_of"] or "Unknown"))
if risk.get("reference_close") is not None:
    symbol = "₹" if risk["currency"] == "INR" else "$"
    a,b,c=st.columns(3)
    a.metric("Verified daily close", f"{symbol}{risk['reference_close']:,.2f}")
    b.metric("Annualized historical volatility", f"{risk['historical_annualized_vol_pct']:.1f}%")
    c.metric("Illustrative horizon", f"{risk['horizon_sessions']} trading sessions")
    low,high=risk["illustrative_volatility_band"]
    st.info(f"Volatility scale only: {symbol}{low:,.2f} – {symbol}{high:,.2f}. This is NOT a forecast interval, target, entry recommendation, or estimated probability.")
else:
    st.warning("Insufficient verified market data for a numeric volatility screen. No buy/sell zones or price targets can be given.")
st.markdown("### Positive evidence and concerns")
left,right=st.columns(2)
with left:
    st.markdown("**Potential positives (screening only)**")
    for fact in risk["bull_facts"]:
        st.write("•",fact)
    if not risk["bull_facts"]: st.caption("No verified positive screen.")
with right:
    st.markdown("**Negative evidence and risks**")
    for fact in risk["bear_facts"]:
        st.write("•",fact)
    if not risk["bear_facts"]: st.caption("Data insufficient for a detailed bearish assessment.")
st.markdown("### Entry, exit and forecasting status")
st.warning("Verified buy zone: unavailable | Verified sell target: unavailable | Calibrated price prediction: unavailable. These require further historical validation and company-specific fundamental research.")
st.markdown("### Position-size education simulator")
st.caption("User-entered hypothetical entry and invalidation levels — the app does not generate recommendations.")
col1,col2=st.columns(2)
with col1:
    capital=st.number_input(f"Capital ({risk['currency']})",min_value=0.0,value=1000.0,step=100.0)
    max_risk=st.selectbox("Maximum planned risk per position", [0.005,0.01,0.02],index=1,format_func=lambda x:f"{x*100:g}%")
with col2:
    entered=st.number_input(f"Hypothetical entry ({risk['currency']})",min_value=0.0,value=float(risk.get("reference_close") or 100),step=1.0)
    stop=st.number_input(f"Hypothetical invalidation price ({risk['currency']})",min_value=0.0,value=round(float(risk.get("reference_close") or 100)*0.95,2),step=1.0)
limit=st.slider("Maximum capital allocated to one stock (%)",1,100,10)/100
sizing=position_scenario(capital,entered,stop,max_risk,limit)
if sizing["status"]=="INVALID_INPUT":
    st.error("Enter positive capital and entry, invalidation below entry, and valid risk constraints.")
else:
    x,y,z=st.columns(3)
    x.metric("Illustrative whole shares",sizing["shares"])
    y.metric("Illustrative allocation",f"{sizing['allocated_capital']:,.2f} {risk['currency']}")
    z.metric("Planned stop-distance loss",f"{sizing['planned_risk']:,.2f} {risk['currency']}")
    st.caption(sizing["warning"])
st.caption("Classification LOW HISTORICAL VOLATILITY measures past price variability only; it never means safe or 'perfect to buy'. Research and trading remain separate.")
