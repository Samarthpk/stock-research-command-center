"""Universal issuer resolution and historical OHLCV technical risk engine.

Never makes stock recommendations or substitutes an unverified security.
Price results are daily historical observations; targets are conditional
chart levels, not calibrated forecasts.
"""
from __future__ import annotations
import math
import re
import statistics
import urllib.parse

def resolve_from_directory(rows, symbol, market, exchange=None):
    """Resolve exact exchange-specific issuer; reject ambiguous cross-exchange tickers."""
    query=(symbol or "").strip().upper()
    mkt=(market or "").strip().upper()
    if mkt not in ("US","IN") or not query:
        raise ValueError("Select US or IN and a ticker")
    choices=[r for r in rows if r.get("market")==mkt and
             (str(r.get("symbol","")).upper()==query or str(r.get("trading_symbol","")).upper()==query)]
    if exchange:
        choices=[r for r in choices if r.get("exchange")==exchange]
    if not choices:
        raise ValueError("Symbol not found in a successfully loaded official exchange directory")
    if len(choices)!=1:
        raise ValueError("Ambiguous ticker across exchange listings; select a specific exchange")
    r=choices[0]
    if not r.get("name"):
        raise ValueError("Issuer name missing in directory")
    return r

def link_nse_to_bse_issuer(nse_issuer, all_listings):
    """Link NSE to exactly one BSE issuer by identical valid ISIN, never ticker text."""
    if nse_issuer.get("market")!="IN" or nse_issuer.get("exchange")!="NSE":
        return None
    isin=str(nse_issuer.get("isin") or "").strip().upper()
    if not re.fullmatch(r"IN[A-Z0-9]{10}",isin):
        return None
    matches=[r for r in all_listings if r.get("market")=="IN" and
             r.get("exchange")=="BSE" and str(r.get("isin") or "").strip().upper()==isin]
    if len(matches)!=1:
        return None
    return matches[0]

def provider_candidates(issuer):
    """Use BSE codes for Indian pricing. Never presume NSE ticker maps to BSE."""
    mkt=issuer["market"]
    sym=issuer["symbol"]
    if mkt=="US":
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}",sym):
            return []
        return [sym]
    if issuer.get("exchange")=="BSE":
        candidates=[]
        trading=str(issuer.get("trading_symbol") or "").upper()
        if trading and re.fullmatch(r"[A-Z0-9.\-]{2,15}",trading):
            candidates.append(trading+".BSE")
        if str(sym).isdigit():
            candidates.append(sym+".BSE")
        return list(dict.fromkeys(candidates))
    return []  # NSE-only record is not proof of a BSE scrip mapping

def historical_ohlcv(payload):
    daily=payload.get("Time Series (Daily)") if isinstance(payload,dict) else None
    if not isinstance(daily,dict):
        raise ValueError("Provider did not return verified daily OHLCV")
    rows=[]
    for day,v in daily.items():
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}",day):
            continue
        try:
            o,h,l,c,vv=(float(v[k]) for k in ("1. open","2. high","3. low","4. close","5. volume"))
        except (TypeError,ValueError,KeyError):
            continue
        if not all(math.isfinite(x) for x in (o,h,l,c,vv)) or min(o,h,l,c)<=0 or vv<0 or h<max(o,c,l) or l>min(o,c):
            continue
        rows.append({"date":day,"open":o,"high":h,"low":l,"close":c,"volume":vv})
    rows.sort(key=lambda r:r["date"])
    if len(rows)<60:
        raise ValueError("Need >=60 valid historical OHLCV bars")
    return rows

def _ema(values,n):
    alpha=2/(n+1)
    state=sum(values[:n])/n
    output=[None]*(n-1)+[state]
    for val in values[n:]:
        state=alpha*val+(1-alpha)*state
        output.append(state)
    return output

def calculate_technicals(rows, stop_atr_multiplier=1.5):
    if len(rows)<60:
        raise ValueError("Need >=60 market sessions")
    closes=[float(r["close"]) for r in rows]
    highs=[float(r["high"]) for r in rows]
    lows=[float(r["low"]) for r in rows]
    prev=closes[0]
    trs=[]
    for r in rows:
        trs.append(max(r["high"]-r["low"],abs(r["high"]-prev),abs(r["low"]-prev)))
        prev=r["close"]
    # Wilder RSI14
    diffs=[closes[i]-closes[i-1] for i in range(1,len(closes))]
    avg_gain=sum(max(x,0) for x in diffs[:14])/14
    avg_loss=sum(max(-x,0) for x in diffs[:14])/14
    for d in diffs[14:]:
        avg_gain=(13*avg_gain+max(d,0))/14
        avg_loss=(13*avg_loss+max(-d,0))/14
    rsi=100 if avg_loss==0 and avg_gain>0 else (50 if avg_loss==0 else 100-100/(1+avg_gain/avg_loss))
    # ATR14, Wilder smoothing
    atr=sum(trs[1:15])/14
    for t in trs[15:]:
        atr=(13*atr+t)/14
    e12,e26=_ema(closes,12),_ema(closes,26)
    macd=[e12[i]-e26[i] for i in range(25,len(closes))]
    signal=_ema(macd,9)[-1]
    macd_last=macd[-1]
    close=closes[-1]
    ma20=statistics.mean(closes[-20:])
    ma50=statistics.mean(closes[-50:])
    returns=[closes[i]/closes[i-1]-1 for i in range(len(closes)-19,len(closes))]
    annual_vol=statistics.stdev(returns)*math.sqrt(252)*100
    risk=("VERY HIGH" if annual_vol>=70 else "HIGH" if annual_vol>=45 else "MODERATE" if annual_vol>=25 else "LOW HISTORICAL VOLATILITY")
    support=min(lows[-20:])
    resistance=max(highs[-20:])
    # hypothetical entry = observed historical close, not buy price.
    stop=min(close-stop_atr_multiplier*atr,support)
    stop=max(0,stop)
    potential_loss=close-stop
    potential_gain=max(0,resistance-close)
    rr=(potential_gain/potential_loss) if potential_loss>0 else None
    notes=[]
    if close>ma20>ma50:
        notes.append("Historical trend: close above MA20, MA20 above MA50.")
    elif close<ma20<ma50:
        notes.append("Historical trend: close below MA20, MA20 below MA50.")
    else:
        notes.append("Mixed trend; moving averages are not aligned.")
    if rsi>=70:notes.append("RSI >=70: momentum is stretched; NOT an automatic sell signal.")
    if rsi<=30:notes.append("RSI <=30: oversold screen; NOT an automatic buy signal.")
    if macd_last>signal:notes.append("MACD above its signal line (historical momentum).")
    else:notes.append("MACD below its signal line (historical momentum).")
    notes.append("No fundamentals, valuations or investment suitability implied by technical data.")
    return {
        "as_of":rows[-1]["date"],"close":round(close,4),"volume":int(rows[-1]["volume"]),
        "ma20":round(ma20,4),"ma50":round(ma50,4),"rsi14":round(rsi,2),
        "macd":round(macd_last,4),"macd_signal":round(signal,4),
        "atr14":round(atr,4),"annualized_volatility_pct":round(annual_vol,2),
        "risk_level":risk,
        "historical_20d_low_support":round(support,4),
        "historical_20d_high_resistance":round(resistance,4),
        "illustrative_stop_level":round(stop,4),
        "illustrative_upside_pct":round(100*potential_gain/close,2),
        "illustrative_downside_pct":round(100*potential_loss/close,2),
        "illustrative_reward_risk":round(rr,2) if rr is not None else None,
        "observations":notes,
        "decision":"RESEARCH_ONLY_NO_RECOMMENDATION",
        "warning":"Historical daily levels only. Not a buy/sell zone, price forecast, or guaranteed stop execution. Prices, fees, gaps and FX may vary.",
    }
