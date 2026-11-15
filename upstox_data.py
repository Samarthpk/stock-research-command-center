"""Read-only Upstox Analytics Token adapter (NSE/BSE).

Only GET market-data endpoints. No order, account, or broker trading functions.
Official BOD instrument JSON resolves issuer identity; namesake ticker matches
are never accepted across unrelated exchanges.
"""
from __future__ import annotations
import datetime as dt
import gzip
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

BOD_NSE="https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
BOD_BSE="https://assets.upstox.com/market-quote/instruments/exchange/BSE.json.gz"
ROOT="https://api.upstox.com"
MAX_COMPRESSED=18_000_000
MAX_UNCOMPRESSED=140_000_000
IST=ZoneInfo("Asia/Kolkata")

class UpstoxDataError(ValueError):
    pass

def _get_json(url, *, token=None, timeout=25):
    if not url.startswith((ROOT+"/","https://assets.upstox.com/market-quote/instruments/")):
        raise UpstoxDataError("Unsupported Upstox endpoint")
    headers={"User-Agent":"StockResearchCommandCenter/0.8 (read-only analytics)",
             "Accept":"application/json"}
    if token:
        headers["Authorization"]="Bearer "+token
    req=urllib.request.Request(url,headers=headers)
    try:
        with urllib.request.urlopen(req,timeout=timeout) as response:
            limit=MAX_COMPRESSED if url.endswith(".gz") else 6_000_000
            raw=response.read(limit+1)
    except urllib.error.HTTPError as exc:
        # Prevent accidentally printing Upstox errors containing secrets.
        raise UpstoxDataError("Upstox HTTP "+str(exc.code)+" (check analytics token, rate limits and permissions)") from None
    except (OSError,TimeoutError):
        raise UpstoxDataError("Upstox service unreachable or timed out") from None
    if len(raw)>limit:
        raise UpstoxDataError("Upstox response exceeds configured size limit")
    if url.endswith(".gz"):
        try:
            raw=gzip.decompress(raw)
        except (OSError,EOFError):
            raise UpstoxDataError("Invalid compressed instrument master") from None
        if len(raw)>MAX_UNCOMPRESSED:
            raise UpstoxDataError("Instrument master exceeds size limit")
    try:
        obj=json.loads(raw)
    except (ValueError,UnicodeDecodeError):
        raise UpstoxDataError("Upstox returned invalid JSON") from None
    return obj

def parse_instruments(payload, exchange):
    """Equity-only BOD with unique Upstox instrument keys, including SME series."""
    if exchange not in ("NSE","BSE") or not isinstance(payload,list):
        raise UpstoxDataError("Invalid exchange instrument master")
    seen={}
    for item in payload:
        if not isinstance(item,dict):
            continue
        segment=item.get("segment")
        if segment!=exchange+"_EQ" or item.get("exchange")!=exchange:
            continue
        key=item.get("instrument_key")
        symbol=str(item.get("trading_symbol") or "").strip().upper()
        name=str(item.get("name") or "").strip()
        isin=str(item.get("isin") or "").strip().upper()
        if not key or not name or not symbol or not key.startswith(segment+"|"):
            continue
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9.&_+\\-]{0,29}",symbol):
            continue
        if not re.fullmatch(r"IN[A-Z0-9]{10}",isin):
            continue
        if key!=segment+"|"+isin:
            continue
        rec={"market":"IN","exchange":exchange,"segment":segment,
             "symbol":symbol,"name":name,"isin":isin,
             "instrument_key":key,"exchange_token":str(item.get("exchange_token") or ""),
             "instrument_type":str(item.get("instrument_type") or ""),
             "source":BOD_NSE if exchange=="NSE" else BOD_BSE}
        if key in seen and seen[key]["symbol"]!=symbol:
            # Unexpected duplicate identity: refuse ambiguous issuer.
            seen.pop(key,None)
            continue
        seen[key]=rec
    return list(seen.values())

def load_instruments(fetch=None):
    fetch=fetch or _get_json
    rows=[]
    health={}
    for exchange,url in (("NSE",BOD_NSE),("BSE",BOD_BSE)):
        try:
            parsed=parse_instruments(fetch(url),exchange)
            if not parsed:
                raise UpstoxDataError("Empty equity master")
            rows.extend(parsed)
            health[exchange]={"status":"OK","count":len(parsed)}
        except (UpstoxDataError,ValueError,TypeError) as exc:
            health[exchange]={"status":"UNAVAILABLE","reason":str(exc)[:120]}
    return {"rows":rows,"source_health":health,"retrieved_at":dt.datetime.now(dt.timezone.utc).isoformat()}

def search_instruments(rows,query,exchange="ALL",max_results=100):
    q=str(query or "").strip().casefold()
    if not q:
        return []
    results=[r for r in rows if (exchange=="ALL" or r["exchange"]==exchange)
             and (q in r["symbol"].casefold() or q in r["name"].casefold()
                  or q in r["isin"].casefold() or q in r["exchange_token"].casefold())]
    results.sort(key=lambda r:(0 if r["symbol"].casefold()==q else 1,
                               0 if r["exchange"]=="NSE" else 1,r["symbol"]))
    return results[:max(1,min(max_results,200))]

def _market_response(payload):
    if not isinstance(payload,dict) or payload.get("status")!="success" or not isinstance(payload.get("data"),dict):
        raise UpstoxDataError("Upstox market-data request unsuccessful")
    return payload["data"]

def parse_quote(payload,issuer,now=None):
    data=_market_response(payload)
    key=issuer["instrument_key"]
    record=None
    for item in data.values():
        if isinstance(item,dict) and item.get("instrument_token")==key:
            record=item
            break
    if record is None:
        raise UpstoxDataError("Upstox quote instrument identity mismatch")
    symbol=str(record.get("symbol") or "").strip().upper()
    if symbol!=issuer["symbol"]:
        raise UpstoxDataError("Upstox quote ticker identity mismatch")
    try:
        price=float(record["last_price"])
        volume=int(record.get("volume") or 0)
        ts=dt.datetime.fromisoformat(str(record["timestamp"]).replace("Z","+00:00"))
    except (ValueError,TypeError,KeyError):
        raise UpstoxDataError("Price, volume or quote timestamp missing") from None
    if not math.isfinite(price) or price<=0 or volume<0 or ts.tzinfo is None:
        raise UpstoxDataError("Invalid market quote evidence")
    now=now or dt.datetime.now(dt.timezone.utc)
    age=(now-ts.astimezone(dt.timezone.utc)).total_seconds()
    if age< -60:
        raise UpstoxDataError("Quote timestamp in the future")
    # Freshness checks never misrepresent a weekend closing print as live.
    freshness="FRESH_SNAPSHOT" if 0<=age<=120 else "LAST_REPORTED_NOT_LIVE"
    return {"exchange":issuer["exchange"],"symbol":symbol,"company":issuer["name"],
            "currency":"INR","price":price,"volume":volume,
            "timestamp":ts.isoformat(),"age_seconds":round(age),
            "quote_status":freshness,
            "source":"Upstox Market Quote V3 (read-only)",
            "instrument_key":key}

def fetch_quote(issuer,token,fetch=None):
    if not token:
        raise UpstoxDataError("UPSTOX_ANALYTICS_TOKEN missing")
    key=issuer["instrument_key"]
    url=ROOT+"/v3/market-quote/quotes?"+urllib.parse.urlencode({"instrument_key":key})
    return parse_quote((fetch or _get_json)(url,token=token),issuer)

def parse_daily_candles(payload,min_bars=60):
    data=_market_response(payload)
    candles=data.get("candles")
    if not isinstance(candles,list):
        raise UpstoxDataError("Missing Upstox candles")
    rows={}
    for candle in candles:
        if not isinstance(candle,list) or len(candle)<6:
            continue
        try:
            timestamp=dt.datetime.fromisoformat(str(candle[0]).replace("Z","+00:00"))
            if timestamp.tzinfo is None:
                continue
            day=timestamp.astimezone(IST).date().isoformat()
            o,h,l,c,v=(float(x) for x in candle[1:6])
            if not all(math.isfinite(x) for x in (o,h,l,c,v)):
                continue
            if min(o,h,l,c)<=0 or h<max(o,c,l) or l>min(o,c) or v<0:
                continue
            if day in rows:
                raise UpstoxDataError("Duplicate trading session in historical response")
            rows[day]={"date":day,"open":o,"high":h,"low":l,"close":c,"volume":v}
        except (TypeError,ValueError,IndexError):
            continue
    ordered=[rows[day] for day in sorted(rows)]
    if len(ordered)<min_bars:
        raise UpstoxDataError("Not enough valid daily candles for reliable indicators")
    return ordered

def fetch_daily(issuer,token,*,days=365,fetch=None,today=None):
    if not token:
        raise UpstoxDataError("UPSTOX_ANALYTICS_TOKEN missing")
    if days<120 or days>3650:
        raise UpstoxDataError("Historical range outside supported bounds")
    today=today or dt.datetime.now(IST).date()
    from_date=today-dt.timedelta(days=days)
    instrument=urllib.parse.quote(issuer["instrument_key"],safe="")
    url=ROOT+"/v3/historical-candle/"+instrument+"/days/1/"+today.isoformat()+"/"+from_date.isoformat()
    payload=(fetch or _get_json)(url,token=token)
    return parse_daily_candles(payload)
