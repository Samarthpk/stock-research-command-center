"""Searchable exchange-listed US and Indian equity directory (not market price data).

US: Nasdaq Trader symbol-directory files (Nasdaq + other US exchanges).
India: NSE's official EQ segment csv. BSE-exclusive listings require a
separately verified official universe feed and are explicitly NOT claimed.
Failures are visible, not silently replaced by the five-stock seed watchlist.
"""
from __future__ import annotations

import csv
import io
import json
import re
import urllib.request
import urllib.parse
from datetime import datetime, timezone

US_NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
US_OTHER_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
NSE_EQUITY_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
BSE_EQUITY_URL = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?" + urllib.parse.urlencode({"scripcode":"","Group":"","industry":"","segment":"Equity","status":"Active"})
SOURCES = {"US Nasdaq": US_NASDAQ_URL, "US other exchanges": US_OTHER_URL, "India NSE EQ": NSE_EQUITY_URL}
MAX_DOWNLOAD_BYTES = 5_000_000
MAX_ROWS = 30000

def _download(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "StockResearchCommandCenter/0.5 (listing directory)", "Accept": "text/plain,text/csv,*/*"})
    with urllib.request.urlopen(req, timeout=18) as resp:
        data = resp.read(MAX_DOWNLOAD_BYTES + 1)
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise ValueError("Directory source exceeded maximum allowed size")
        return data.decode("utf-8-sig", errors="replace")

def _clean(value):
    return " ".join(str(value or "").strip().split())

def _record(symbol, name, market, exchange, asset_type, source, isin=""):
    symbol = _clean(symbol).upper()
    name = _clean(name)
    if not symbol or not name or len(symbol) > 25 or len(name) > 255:
        return None
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.&_+\\-]{0,24}", symbol):
        return None
    return {"symbol": symbol, "name": name, "market": market, "exchange": exchange, "asset_type": asset_type, "isin": _clean(isin), "source": source}

def parse_nasdaq_directory(text, *, other=False):
    """Exclude footer/test issues, and label ETFs separately from equities."""
    reader = csv.DictReader(io.StringIO(text), delimiter="|")
    result = []
    for row in reader:
        if not row or str(next(iter(row.values()), "")).startswith("File Creation Time"):
            continue
        if _clean(row.get("Test Issue")).upper() == "Y":
            continue
        symbol = row.get("ACT Symbol") if other else row.get("Symbol")
        name = row.get("Security Name")
        exchange = {"N":"NYSE","A":"NYSE American","P":"NYSE Arca","Z":"Cboe BZX","V":"IEX"}.get(_clean(row.get("Exchange")), "Other US") if other else "Nasdaq"
        is_etf = _clean(row.get("ETF")).upper() == "Y"
        asset_type = "ETF" if is_etf else ("Equity / Other security" if other else "Equity / Other security")
        record = _record(symbol, name, "US", exchange, asset_type, US_OTHER_URL if other else US_NASDAQ_URL)
        if record:
            result.append(record)
        if len(result) > MAX_ROWS:
            raise ValueError("Directory row limit exceeded")
    return result

def parse_nse_equities(text):
    reader = csv.DictReader(io.StringIO(text))
    out=[]
    for row in reader:
        data={str(k or "").strip().upper():v for k,v in row.items()}
        series = _clean(data.get("SERIES")).upper()
        if not series:
            continue
        record=_record(data.get("SYMBOL"),data.get("NAME OF COMPANY"),"IN","NSE",f"Equity / Series {series}",NSE_EQUITY_URL,data.get("ISIN NUMBER"))
        if record:
            out.append(record)
        if len(out) > MAX_ROWS:
            raise ValueError("Directory row limit exceeded")
    return out

def parse_bse_equities(text):
    """Parse active BSE equity records; scrip code is the unique exchange identifier."""
    payload = json.loads(text)
    if not isinstance(payload, list):
        raise ValueError("Unexpected BSE directory response")
    out = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        fields = {str(k).casefold():v for k,v in entry.items()}
        if _clean(fields.get("status")).casefold() != "active":
            continue
        if _clean(fields.get("segment")).casefold() != "equity":
            continue
        code = _clean(fields.get("scrip_cd"))
        symbol = _clean(fields.get("scrip_id"))
        name = _clean(fields.get("scrip_name") or fields.get("issuer_name"))
        r = _record(code, name, "IN", "BSE", "Equity", BSE_EQUITY_URL, fields.get("isin_number"))
        if r:
            r["trading_symbol"] = symbol
            r["bse_scrip_code"] = code
            r["group"] = _clean(fields.get("group"))
            out.append(r)
        if len(out) > MAX_ROWS:
            raise ValueError("BSE directory row limit exceeded")
    return out

def load_directory(fetch=_download):
    """Returns data AND per-source status so partial coverage is never shown as complete."""
    rows=[]
    statuses={}
    for label, url, parser in [
        ("US Nasdaq", US_NASDAQ_URL, lambda text:parse_nasdaq_directory(text,other=False)),
        ("US other exchanges", US_OTHER_URL, lambda text:parse_nasdaq_directory(text,other=True)),
        ("India NSE Equity", NSE_EQUITY_URL, parse_nse_equities),
        ("India BSE Equity", BSE_EQUITY_URL, parse_bse_equities),
    ]:
        try:
            parsed=parser(fetch(url))
            if not parsed:
                raise ValueError("No parseable listings")
            rows.extend(parsed)
            statuses[label]={"status":"OK","count":len(parsed),"source":url}
        except Exception as exc:
            statuses[label]={"status":"UNAVAILABLE","count":0,"source":url,"error":type(exc).__name__}

    unique={}
    for row in rows:
        unique[(row["market"],row["exchange"],row["symbol"])]=row
    all_ok=all(item["status"]=="OK" for item in statuses.values())
    return {"rows":list(unique.values()),"sources":statuses,"retrieved_at":datetime.now(timezone.utc).isoformat(),"complete_coverage":all_ok, "coverage_note":"Exchange directory coverage; subject to listings, API availability and instruments not in the selected source files."}

def search_directory(rows, query="",market="ALL",exchange="ALL",limit=500):
    q=_clean(query).casefold()
    matches=[r for r in rows if (market=="ALL" or r["market"]==market) and (exchange=="ALL" or r["exchange"]==exchange) and (not q or q in r["symbol"].casefold() or q in r["name"].casefold() or q in r["isin"].casefold())]
    matches.sort(key=lambda r:(0 if q and r["symbol"].casefold()==q else 1, r["market"],r["symbol"],r["exchange"]))
    return {"total":len(matches),"rows":matches[:max(1,min(int(limit),2000))]}
