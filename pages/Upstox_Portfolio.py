"""Private, read-only Upstox portfolio workspace.

No server-side persistence: users must save an encrypted backup themselves.
No broker holdings access, no orders. Holdings require explicit import/entry.
"""
import json
import hmac
import os
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import pandas as pd
import streamlit as st
from upstox_data import load_instruments,search_instruments,fetch_quote,fetch_daily,UpstoxDataError
from portfolio_manager import (validate_holding,parse_holdings_csv,validate_event,
                               summary,events_summary,export_backup,import_backup,merge_holdings)
from unified_research import calculate_technicals
from signal_validation import evaluate_signal

st.title("💼 Upstox Portfolio Manager — India")
st.caption("Personal read-only NSE/BSE holdings, market valuation, concentration and risk research. Holdings are user supplied, not automatically broker synchronized. No trading or investment recommendations.")
st.warning("Private financial data: the portfolio requires an access password; it is not broker-synchronized or stored permanently. Backups are not encrypted by this app.")
try:
    expected_password=st.secrets.get("PORTFOLIO_ACCESS_PASSWORD","")
except Exception:
    expected_password=os.getenv("PORTFOLIO_ACCESS_PASSWORD","")
if not expected_password or len(expected_password)<12:
    st.error("Private portfolio locked: set PORTFOLIO_ACCESS_PASSWORD (at least 12 characters) in Streamlit Secrets, then reload. Do not use your Upstox login/password.")
    st.stop()
if not st.session_state.get("portfolio_authorized",False):
    with st.form("portfolio_login"):
        entry=st.text_input("Portfolio access password",type="password")
        signed=st.form_submit_button("Unlock portfolio")
    if not signed or not hmac.compare_digest(entry,expected_password):
        if signed:st.error("Wrong portfolio password")
        st.stop()
    st.session_state.portfolio_authorized=True
st.caption("Portfolio access unlocked for this browser session. This password gate is basic access control, not a substitute for private deployment and proper user authentication.")
if st.button("Lock portfolio"):
    for key in ("portfolio_authorized","pm_holdings","pm_quotes","pm_events","pm_signal"):
        st.session_state.pop(key,None)
    st.rerun()


def token():
    try:return st.secrets.get("UPSTOX_ANALYTICS_TOKEN") or os.getenv("UPSTOX_ANALYTICS_TOKEN","")
    except Exception:return os.getenv("UPSTOX_ANALYTICS_TOKEN","")

@st.cache_data(ttl=86400)
def instruments():
    return load_instruments()

@st.cache_data(ttl=600,show_spinner="Fetching quote from Upstox")
def quoted(key,access):
    return fetch_quote(next(r for r in instruments()["rows"] if r["instrument_key"]==key),access)

@st.cache_data(ttl=900)
def candles(key,access):
    return fetch_daily({"instrument_key":key},access,days=400)

universe=instruments()
for exchange,status in universe["source_health"].items():
    if status["status"]!="OK": st.warning(exchange+" master unavailable; source coverage incomplete.")
if not universe["rows"]:
    st.error("Instrument master unavailable: portfolio imports cannot be verified.")
    st.stop()

if "pm_holdings" not in st.session_state:st.session_state.pm_holdings=[]
if "pm_events" not in st.session_state:st.session_state.pm_events=[]
if "pm_quotes" not in st.session_state:st.session_state.pm_quotes={}
if "pm_signal" not in st.session_state:st.session_state.pm_signal={}

st.subheader("1. Add or import your holdings")
tab_manual,tab_csv,tab_restore=st.tabs(["Add a holding","CSV import","Restore portfolio backup"])
with tab_manual:
    search=st.text_input("Company, ticker or ISIN",placeholder="HGINFRA",key="pm_search")
    matches=search_instruments(universe["rows"],search, max_results=50) if search.strip() else []
    if matches:
        choices={f"{r['name']} | {r['exchange']}:{r['symbol']} | {r['isin']}":r for r in matches}
        chosen=st.selectbox("Verify company and exchange",list(choices))
        issuer=choices[chosen]
        qty=st.number_input("Current holding quantity",min_value=0.0,value=1.0,step=1.0)
        avg=st.number_input("Average buy price (₹)",min_value=0.0,value=100.0,step=1.0)
        sector=st.text_input("Sector (optional, user-defined)",value="Unknown")
        if st.button("Add verified holding",type="primary"):
            try:
                new=validate_holding({**issuer,"quantity":qty,"average_price":avg,"sector":sector},universe["rows"])
                if any(r["instrument_key"]==new["instrument_key"] for r in st.session_state.pm_holdings):
                    st.warning("This instrument already exists. Remove it and add the correct consolidated quantity/average to avoid double counting.")
                else:
                    st.session_state.pm_holdings.append(new)
                    st.success("Holding added to the current session. Save a backup for future use.")
            except ValueError as exc:st.error(str(exc))
    else:
        st.caption("Search an instrument to enter the number of shares and your average purchase price.")

with tab_csv:
    st.caption("CSV requires exchange, ISIN or instrument_key, quantity and average_price. Optional symbol, sector and company. All rows must match current Upstox master; invalid rows reject the entire import.")
    template="exchange,isin,quantity,average_price,sector\\nNSE,INE926X01010,10,450.00,Infrastructure\\n"
    st.download_button("Download CSV example",template.replace("\\n","\n"),file_name="portfolio_holdings_template.csv",mime="text/csv")
    data=st.file_uploader("Upload your holdings CSV",type="csv",key="pm_csv")
    if data is not None and st.button("Validate & replace holdings from CSV"):
        try:
            result=parse_holdings_csv(data.getvalue(),universe["rows"])
            if result["errors"]:
                st.error(f"Import rejected: {result['rejected']} rows invalid.")
                st.dataframe(pd.DataFrame(result["errors"]),hide_index=True)
            elif not result["holdings"]:
                st.error("No holdings detected; current portfolio unchanged.")
            else:
                st.session_state.pm_holdings=result["holdings"]
                st.session_state.pm_quotes={}
                st.session_state.pm_signal={}
                st.success(f"Imported {result['accepted']} verified rows. Replaced previous holdings (not merged).")
        except (ValueError,TypeError) as exc:st.error(str(exc))
with tab_restore:
    backup=st.file_uploader("Restore a previously downloaded portfolio JSON",type=["json"],key="pm_backup")
    if backup is not None and st.button("Restore backup (replace current holdings and events)"):
        try:
            holdings,events=import_backup(backup.getvalue(),universe["rows"])
            st.session_state.pm_holdings=holdings
            st.session_state.pm_events=events
            st.session_state.pm_quotes={}
            st.session_state.pm_signal={}
            st.success("Backup restored into this session. Quotes must be refreshed.")
        except (ValueError,TypeError) as exc:st.error(str(exc))

holdings=st.session_state.pm_holdings
if not holdings:
    st.info("No holdings imported yet. Add a stock above to begin.")
    st.stop()

st.subheader("2. Your portfolio and market value")
st.caption("Quotes are requested only on button clicks, not streamed. Unpriced positions are excluded from the marked subtotal, never treated as zero-valued.")
access=token()
if not access:
    st.error("UPSTOX_ANALYTICS_TOKEN missing. Holdings import and backup still work; current value needs the token.")
elif st.button("Refresh latest Upstox quotes for my holdings",type="primary"):
    failures=[]
    for holding in holdings:
        key=holding["instrument_key"]
        try:
            live=quoted(key,access)
            st.session_state.pm_quotes[key]=live
        except (UpstoxDataError,ValueError,StopIteration) as exc:
            st.session_state.pm_quotes.pop(key,None)
            failures.append(f"{holding['symbol']} ({holding['exchange']}): {exc}")
    if failures:
        st.warning(f"{len(failures)} holdings could not be valued:")
        for failure in failures:st.caption(failure)
    else:st.success("Quote requests completed. Check per-symbol timestamp and freshness before acting.")

stress=st.slider("Hypothetical decline scenario (%)",min_value=5,max_value=50,value=20,step=5)
snapshot=summary(holdings,st.session_state.pm_quotes,stress_pct=stress)
a,b,c,d=st.columns(4)
a.metric("Invested cost basis",f"₹{snapshot['invested_total']:,.2f}")
b.metric("Value of priced positions",f"₹{snapshot['priced_market_value']:,.2f}" if snapshot["all_holdings_priced"] else f"₹{snapshot['priced_market_value']:,.2f} (partial)")
c.metric("Unrealized P&L (priced positions only)",f"₹{snapshot['priced_unrealized_pnl']:,.2f}")
d.metric("P&L % (priced positions only)",f"{snapshot['priced_unrealized_pnl_pct']:.2f}%" if snapshot["priced_unrealized_pnl_pct"] is not None else "N/A")
if snapshot["unpriced_positions"]:
    st.warning(f"{snapshot['unpriced_positions']} position(s) have no verified quote. Full portfolio value, P&L and concentration cannot be established.")
st.dataframe(pd.DataFrame(snapshot["holdings"]),use_container_width=True,hide_index=True)
st.markdown("### Allocation and concentration")
x,y,z=st.columns(3)
x.metric("Top holding % of priced value",f"{snapshot['top_stock_weight_pct']:.1f}%" if snapshot["top_stock_weight_pct"] is not None else "N/A")
y.metric("Concentration index (HHI)",f"{snapshot['hhi']:.3f}" if snapshot["hhi"] is not None else "N/A")
z.metric(f"Hypothetical {stress}% fall on priced value",f"−₹{snapshot['scenario_loss_on_priced_value']:,.2f}")
if snapshot["top_stock_weight_pct"] is not None and snapshot["top_stock_weight_pct"]>35:
    st.warning("Concentration screen: one priced stock accounts for over 35% of currently priced holdings. Not a formal risk limit.")
if snapshot["sector_values"]:
    st.markdown("**Sector allocation — only user-supplied sectors**")
    st.dataframe(pd.DataFrame([{"sector":k,"market_value":v} for k,v in snapshot["sector_values"].items()]),hide_index=True)
st.caption(snapshot["warning"])

st.subheader("3. Investment research review (no automatic trades)")
if access:
    choices={f"{r['company']} | {r['exchange']}:{r['symbol']}":r for r in holdings}
    selected=st.selectbox("Holding to examine",list(choices))
    holding=choices[selected]
    if st.button("Run 400-day risk and holdout research for this holding"):
        try:
            history=candles(holding["instrument_key"],access)
            m=calculate_technicals(history)
            verdict=evaluate_signal(history) if len(history)>=141 else {"screening_signal":"INSUFFICIENT_HISTORY"}
            st.session_state.pm_signal[holding["instrument_key"]]={"metrics":m,"assessment":verdict}
        except (UpstoxDataError,ValueError,TypeError,KeyError) as exc:
            st.session_state.pm_signal.pop(holding["instrument_key"],None)
            st.error(str(exc))
    finding=st.session_state.pm_signal.get(holding["instrument_key"])
    if finding:
        data=finding["metrics"]
        st.write("**Historical risk:**",data["risk_level"],"| **RSI14:**",data["rsi14"],"| **MACD:**",data["macd"],"| **ATR14:**",data["atr14"])
        st.write("**Technical strategy status:**",finding["assessment"]["screening_signal"])
        st.caption("This is a technical monitoring screen only, not a verified personalized BUY/HOLD/REDUCE instruction.")
        aa,bb=st.columns(2)
        with aa:
            st.markdown("**Positive technical evidence**")
            for p in data["technical_bull_factors"]:st.write("•",p)
        with bb:
            st.markdown("**Negative technical evidence**")
            for p in data["technical_bear_factors"]:st.write("•",p)
        st.info("Decision cannot be validated without your risk objectives, time horizon, verified fundamentals and current liquidity context.")

st.subheader("4. Cash dividends and transaction ledger")
st.caption("User-entered events only. Broker trades, dividends, corporate actions, tax lots and realized P&L are not auto-synchronized.")
with st.form("portfolio_event"):
    event=st.selectbox("Event",["DIVIDEND","BUY","SELL","FEE","TAX","DEPOSIT","WITHDRAWAL"])
    date=st.date_input("Date")
    opts={"No instrument / cash event":""}
    opts.update({f"{r['exchange']}:{r['symbol']} ({r['isin']})":r["instrument_key"] for r in holdings})
    selection=st.selectbox("Instrument",list(opts))
    quantity=st.number_input("Trade quantity (buy/sell)",min_value=0.0,value=0.0)
    price=st.number_input("Trade execution price (₹)",min_value=0.0,value=0.0)
    amount=st.number_input("Cash amount / fees / dividend (₹)",min_value=0.0,value=0.0)
    notes=st.text_input("Notes",max_chars=250)
    if st.form_submit_button("Add ledger event"):
        try:
            rec=validate_event({"event":event,"date":date.isoformat(),"instrument_key":opts[selection],
                                "quantity":quantity,"price":price,"cash_amount":amount,"notes":notes},universe["rows"])
            st.session_state.pm_events.append(rec)
            st.success("Ledger event recorded for this session; holdings and cost basis remain unchanged.")
        except ValueError as exc:st.error(str(exc))
ledger=events_summary(st.session_state.pm_events)
st.write(f"**Events:** {ledger['events_count']} | **Dividends logged:** ₹{ledger['dividends_logged']:,.2f} | **Fees/taxes logged:** ₹{ledger['fees_and_taxes_logged']:,.2f}")
if st.session_state.pm_events:
    st.dataframe(pd.DataFrame(st.session_state.pm_events),hide_index=True,use_container_width=True)
st.caption(ledger["warning"])

st.subheader("5. Export, backup and privacy")
st.download_button("Download portfolio backup JSON (private)",export_backup(holdings,st.session_state.pm_events),
                   file_name="my_private_portfolio_backup.json",mime="application/json")
st.download_button("Download latest valuation CSV",pd.DataFrame(snapshot["holdings"]).to_csv(index=False),
                   file_name="portfolio_valuation.csv",mime="text/csv")
st.caption("Session data can disappear when Streamlit restarts. Store your downloaded JSON in a private encrypted location; this app does not encrypt backups or provide persistent multi-device synchronization.")
if st.button("Clear current session holdings, quotes and events"):
    for key in ("pm_holdings","pm_quotes","pm_events","pm_signal"):
        st.session_state.pop(key,None)
    st.rerun()
