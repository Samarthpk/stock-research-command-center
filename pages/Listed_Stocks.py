"""Exchange-wide stock directory search. Directory discovery is NOT live stock research."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
import streamlit as st
from listed_universe import load_directory,search_directory

st.title("🌐 Listed Stocks — US & India")
st.caption("Search thousands of exchange-listed instruments by company, ticker or ISIN. Listings are directory metadata, NOT price coverage, AI evaluation or buy recommendations.")

@st.cache_data(ttl=24*3600,show_spinner="Loading official exchange directories…")
def get_universe():
    return load_directory()

if st.button("Refresh official listings",help="Refreshes cached listing files. Does not spend Alpha Vantage quota."):
    get_universe.clear()
catalog=get_universe()
sources=catalog["sources"]
st.markdown("### Universe coverage & source health")
for label,status in sources.items():
    if status["status"]=="OK":
        st.success(f"{label}: {status['count']:,} symbols loaded")
    elif status["status"]=="NOT_IMPLEMENTED":
        st.warning(f"{label}: not yet covered. NSE data does not include every BSE-exclusive company.")
    else:
        st.error(f"{label}: source unavailable ({status.get('error','unknown')}); results for this exchange are incomplete.")
st.caption(f"Directory fetched at UTC: {catalog['retrieved_at']}. Listing content may include non-common-share securities; see instrument type. Sources are independent of Alpha Vantage.")

if not catalog["rows"]:
    st.error("No verified directory data is currently available. Please use the source links below or retry later.")
else:
    markets=st.radio("Market",["ALL","US","IN"],horizontal=True,format_func=lambda x:{"ALL":"US + India","US":"🇺🇸 US","IN":"🇮🇳 India"}[x])
    filtered=[r for r in catalog["rows"] if markets=="ALL" or r["market"]==markets]
    exchanges=["ALL"]+sorted(set(r["exchange"] for r in filtered))
    left,right=st.columns([2,1])
    with left:
        query=st.text_input("Search stocks by ticker, company name, or ISIN",placeholder="e.g. AAPL, Microsoft, RELIANCE, ITC, Infosys",key="universe_search")
    with right:
        exchange=st.selectbox("Exchange",exchanges)
    matches=search_directory(filtered,query=query,exchange=exchange,limit=1000)
    c1,c2=st.columns(2)
    c1.metric("Listings loaded",f"{len(filtered):,}")
    c2.metric("Matching listings",f"{matches['total']:,}")
    if matches["total"]>1000:
        st.info("Showing first 1,000 matches. Type a company or ticker to narrow the results.")
    if matches["rows"]:
        frame=pd.DataFrame(matches["rows"])
        st.dataframe(frame[["symbol","name","market","exchange","asset_type","isin"]],hide_index=True,use_container_width=True,height=520)
        st.download_button("Export current search results CSV",frame.to_csv(index=False).encode("utf-8"),file_name="listed_symbols_search.csv",mime="text/csv")
    else:
        st.info("No matching listings in successfully loaded exchange sources.")
st.markdown("### Research limitation")
st.info("Thousands of instruments can be SEARCHED here, but deep-dive price and filing research is quota-limited. The free Alpha Vantage key cannot fetch a full US+India universe daily. Symbols discovered here are not automatically supported by the price provider, and the Indian BSE-exclusive list is still missing.")
st.markdown("### Official directory sources")
for label,status in sources.items():
    if status.get("source"):
        st.markdown(f"- [{label}]({status['source']})")
