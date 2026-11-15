"""Read-only Indian equity portfolio accounting and reconciliation.

User supplied holdings (not broker authenticated). Values use explicit quote
observations; never treat missing quotes as zero-value holdings.
Transactions and dividends are supplemental records, not merged into holdings.
"""
from __future__ import annotations
import csv
import datetime as dt
import io
import json
import math
import re
from collections import defaultdict

MAX_HOLDINGS=500
MAX_LEDGER=10000
ALLOWED_EVENTS={"BUY","SELL","DIVIDEND","FEE","TAX","DEPOSIT","WITHDRAWAL"}
COLUMNS=["instrument_key","symbol","company","exchange","isin","quantity","average_price","sector"]
ALIASES={
    "instrument_key":("instrument_key","instrument token","instrument_token","instrument key"),
    "symbol":("symbol","trading_symbol","trading symbol","stock symbol","scrip"),
    "company":("company","company_name","company name","name","stock name"),
    "exchange":("exchange",),
    "isin":("isin","isin number"),
    "quantity":("quantity","qty","holding qty","holding quantity","net quantity"),
    "average_price":("average_price","average price","avg price","buy average","avg buy price","avg. price"),
    "sector":("sector","industry"),
}
def _number(raw,label,zero=False):
    try:
        value=float(str(raw).replace(",","").strip())
    except (ValueError,TypeError):
        raise ValueError(f"{label}: invalid number") from None
    if not math.isfinite(value) or (value<0 if zero else value<=0):
        raise ValueError(f"{label}: value outside valid range")
    return value

def _key(record):
    return str(record.get("instrument_key") or "").strip()

def validate_holding(record, directory):
    """Resolve exactly one NSE/BSE instrument by Upstox key or ISIN+exchange.

    Never auto-map a stock by a short ticker alone (TCL collision).
    """
    if not isinstance(record,dict):
        raise ValueError("Holding must be an object")
    exchange=str(record.get("exchange") or "").strip().upper()
    key=_key(record)
    isin=str(record.get("isin") or "").strip().upper()
    symbol=str(record.get("symbol") or "").strip().upper()
    if exchange not in ("NSE","BSE"):
        raise ValueError("Specify NSE/BSE exchange")
    if key:
        matches=[r for r in directory if r.get("instrument_key")==key and r.get("exchange")==exchange]
    elif isin:
        matches=[r for r in directory if r.get("isin")==isin and r.get("exchange")==exchange]
    else:
        raise ValueError("Instrument key or ISIN required to prevent ticker collision")
    if len(matches)!=1:
        raise ValueError("Issuer identity not uniquely verified in current Upstox master")
    issuer=matches[0]
    if isin and isin!=issuer["isin"]:
        raise ValueError("Provided ISIN differs from exchange master")
    if symbol and symbol!=issuer["symbol"]:
        raise ValueError("Provided trading symbol differs from exchange master")
    qty=_number(record.get("quantity"),"Quantity")
    avg=_number(record.get("average_price"),"Average purchase price",zero=True)
    if round(qty,6)!=qty:
        raise ValueError("Quantity precision exceeds six decimal places")
    sector=str(record.get("sector") or "Unknown").strip()[:80] or "Unknown"
    return {"instrument_key":issuer["instrument_key"],
            "exchange":issuer["exchange"],"symbol":issuer["symbol"],
            "isin":issuer["isin"],"company":issuer["name"],
            "quantity":qty,"average_price":avg,"sector":sector,
            "cost_basis_source":"USER_SUPPLIED_NOT_BROKER_VERIFIED"}

def parse_holdings_csv(raw,directory):
    if not isinstance(raw,(bytes,bytearray)) or not raw or len(raw)>2_000_000:
        raise ValueError("Missing/oversized holdings CSV (max 2 MB)")
    try:
        body=raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("CSV must be UTF-8") from None
    reader=csv.DictReader(io.StringIO(body,newline=""))
    fields={str(x or "").strip().casefold():x for x in reader.fieldnames or []}
    mapping={}
    for key,aliases in ALIASES.items():
        for alias in aliases:
            if alias in fields:
                mapping[key]=fields[alias]
                break
    if not {"exchange","quantity","average_price"}.issubset(mapping) or not ({"instrument_key","isin"}&set(mapping)):
        raise ValueError("CSV must contain exchange, quantity, average_price, and instrument_key OR isin")
    parsed=[]
    errors=[]
    for idx,row in enumerate(reader,start=2):
        if idx>MAX_HOLDINGS+1:
            raise ValueError("Too many holding rows")
        rec={k:row.get(header) for k,header in mapping.items()}
        try:
            parsed.append(validate_holding(rec,directory))
        except ValueError as exc:
            errors.append({"row":idx,"error":str(exc)})
    if errors:
        return {"holdings":[],"errors":errors,"accepted":0,"rejected":len(errors)}
    return {"holdings":merge_holdings(parsed),"errors":[],"accepted":len(parsed),"rejected":0}

def merge_holdings(records):
    """For duplicate lots, sum quantity and compute weighted average cost."""
    by_key={}
    for rec in records:
        key=rec["instrument_key"]
        if key in by_key:
            original=by_key[key]
            count=original["quantity"]+rec["quantity"]
            original["average_price"]=(original["quantity"]*original["average_price"]+rec["quantity"]*rec["average_price"])/count
            original["quantity"]=count
            if original["sector"]!=rec["sector"]:
                original["sector"]="Unknown"
        else:
            by_key[key]=dict(rec)
    return list(by_key.values())

def validate_event(record, directory=None):
    event=str(record.get("event") or "").strip().upper()
    if event not in ALLOWED_EVENTS:
        raise ValueError("Unrecognized transaction event")
    date=str(record.get("date") or "").strip()
    try:
        date=dt.date.fromisoformat(date).isoformat()
    except ValueError:
        raise ValueError("Event date must be YYYY-MM-DD") from None
    if dt.date.fromisoformat(date)>dt.datetime.now(dt.timezone.utc).date():
        raise ValueError("Future event not supported")
    instrument=str(record.get("instrument_key") or "").strip()
    quantity=_number(record.get("quantity") or 0,"Event quantity",zero=True)
    price=_number(record.get("price") or 0,"Event price",zero=True)
    cash=_number(record.get("cash_amount") or 0,"Event cash amount",zero=True)
    if event in ("BUY","SELL"):
        if not instrument or quantity<=0 or price<=0:
            raise ValueError("Buy/sell requires verified instrument key, positive quantity and price")
    elif event=="DIVIDEND":
        if not instrument or cash<=0:
            raise ValueError("Dividend requires instrument and cash received")
    elif cash<=0:
        raise ValueError("Cash event requires positive amount")
    if instrument and directory is not None and sum(1 for x in directory if x.get("instrument_key")==instrument)!=1:
        raise ValueError("Transaction instrument not verified")
    return {"date":date,"event":event,"instrument_key":instrument,
            "quantity":quantity,"price":price,"cash_amount":cash,
            "notes":str(record.get("notes") or "").strip()[:250],
            "origin":"USER_SUPPLIED_NOT_BROKER_VERIFIED"}

def summary(holdings,quotes,stress_pct=20):
    if not 0<=stress_pct<=100:
        raise ValueError("Invalid stress case")
    rows=[]
    invested_total=0
    marked_total=0
    marked_cost=0
    missing=0
    for record in holdings:
        qty=record["quantity"]
        cost=qty*record["average_price"]
        invested_total+=cost
        quote=quotes.get(record["instrument_key"])
        price=None
        timestamp=None
        freshness="NOT_QUOTED"
        if quote is not None:
            if quote.get("instrument_key")!=record["instrument_key"]:
                raise ValueError("Quote identity mismatch")
            price=_number(quote.get("price"),"Quote price")
            timestamp=quote.get("timestamp")
            freshness=quote.get("quote_status") or "UNVERIFIED"
        current=qty*price if price is not None else None
        pnl=current-cost if current is not None else None
        if current is None:missing+=1
        else:
            marked_total+=current
            marked_cost+=cost
        rows.append({**record,"invested":round(cost,2),
                     "last_price":price,"price_timestamp":timestamp,
                     "quote_freshness":freshness,
                     "market_value":round(current,2) if current is not None else None,
                     "unrealized_pnl":round(pnl,2) if pnl is not None else None,
                     "unrealized_pnl_pct":round(100*pnl/cost,2) if cost>0 and pnl is not None else None})
    allocation=[]
    sectors=defaultdict(float)
    for row in rows:
        if row["market_value"] is not None and marked_total>0:
            weight=row["market_value"]/marked_total
            row["allocation_pct_of_priced_holdings"]=round(weight*100,2)
            sectors[row["sector"]]+=row["market_value"]
            allocation.append(weight)
        else:
            row["allocation_pct_of_priced_holdings"]=None
    top=max(allocation,default=0)
    hhi=sum(w*w for w in allocation)
    # Clearly conditional: a uniform 20% decline across marked assets is not a calibrated VaR estimate.
    return {"holdings":rows,
            "invested_total":round(invested_total,2),
            "priced_cost_basis":round(marked_cost,2),
            "priced_market_value":round(marked_total,2),
            "priced_unrealized_pnl":round(marked_total-marked_cost,2),
            "priced_unrealized_pnl_pct":round(100*(marked_total-marked_cost)/marked_cost,2) if marked_cost>0 else None,
            "all_holdings_priced":missing==0 and len(rows)>0,
            "unpriced_positions":missing,
            "top_stock_weight_pct":round(top*100,2) if allocation else None,
            "hhi":round(hhi,4) if allocation else None,
            "stress_pct":stress_pct,
            "scenario_loss_on_priced_value":round(marked_total*stress_pct/100,2),
            "sector_values":{sector:round(amount,2) for sector,amount in sectors.items()},
            "sector_source":"USER_SUPPLIED (Unknown where not provided)",
            "warning":"Portfolio values are partial when any quote missing; quote freshness varies. Stress case is hypothetical, not VaR or predicted loss."}

def events_summary(events):
    dividend=sum(event["cash_amount"] for event in events if event["event"]=="DIVIDEND")
    fees=sum(event["cash_amount"] for event in events if event["event"] in ("FEE","TAX"))
    return {"dividends_logged":round(dividend,2),"fees_and_taxes_logged":round(fees,2),
            "events_count":len(events),
            "warning":"Event ledger is user-entered and not reconciled with Upstox statement. Dividends are not automatically included in unrealized P&L. No tax advice."}

def export_backup(holdings,events):
    payload={"schema_version":1,"holdings":holdings,"events":events,
             "created_at":dt.datetime.now(dt.timezone.utc).isoformat(),
             "warning":"Contains private financial holdings; store securely."}
    return json.dumps(payload,indent=2)

def import_backup(raw,directory):
    if not isinstance(raw,(bytes,bytearray)) or len(raw)>3_000_000:
        raise ValueError("Backup must be JSON <=3 MB")
    try:
        data=json.loads(raw)
    except (ValueError,UnicodeDecodeError):
        raise ValueError("Invalid backup JSON") from None
    if not isinstance(data,dict) or data.get("schema_version")!=1:
        raise ValueError("Unsupported backup schema")
    if not isinstance(data.get("holdings"),list) or not isinstance(data.get("events"),list):
        raise ValueError("Backup records missing")
    if len(data["holdings"])>MAX_HOLDINGS or len(data["events"])>MAX_LEDGER:
        raise ValueError("Backup exceeds entry limits")
    holdings=merge_holdings([validate_holding(item,directory) for item in data["holdings"]])
    events=[validate_event(item,directory) for item in data["events"]]
    return holdings,events
