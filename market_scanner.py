"""Full-exchange *eligible-universe* bulk screening with explicit coverage accounting.

No backdoor calls to a 25-request/day free API. The scanner evaluates ALL
eligible issuer IDs whose OHLCV rows were provided by a legitimate bulk feed
or uploaded CSV. It reports the exact uncovered portion of the market.
"""
from __future__ import annotations
import csv
import datetime as dt
import io
import math
import re
from collections import defaultdict
from unified_research import calculate_technicals

REQUIRED_COLUMNS=("market","exchange","symbol","date","open","high","low","close","volume")
MAX_CSV_BYTES=100_000_000
MAX_INPUT_ROWS=1_400_000
MAX_INSTRUMENTS=30_000
MAX_BARS_PER_SYMBOL=500

def validate_bulk_upload(data, listings, max_bytes=MAX_CSV_BYTES):
    if not isinstance(data,(bytes,bytearray)) or not data or len(data)>max_bytes:
        raise ValueError("Empty or oversized dataset; maximum 100MB")
    try:
        text=bytes(data).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Upload must be UTF-8 CSV") from None
    reader=csv.DictReader(io.StringIO(text,newline=""))
    columns=set(reader.fieldnames or [])
    if not set(REQUIRED_COLUMNS).issubset(columns):
        raise ValueError("CSV needs: "+", ".join(REQUIRED_COLUMNS))
    registry=defaultdict(list)
    for r in listings:
        registry[(r.get("market"),r.get("exchange"),str(r.get("symbol","")).upper())].append(r)
    groups=defaultdict(list)
    errors=defaultdict(int)
    read_count=0
    for row in reader:
        read_count+=1
        if read_count>MAX_INPUT_ROWS:
            raise ValueError("Too many rows; supply a smaller segment or higher-capacity pipeline")
        market=str(row.get("market") or "").strip().upper()
        exchange=str(row.get("exchange") or "").strip()
        symbol=str(row.get("symbol") or "").strip().upper()
        key=(market,exchange,symbol)
        if len(registry.get(key,[]))!=1:
            errors["unverified_or_ambiguous_issuer"]+=1
            continue
        try:
            day=dt.date.fromisoformat(str(row["date"]).strip())
            if day>dt.datetime.now(dt.timezone.utc).date():
                raise ValueError("future data")
            o,h,l,c,v=(float(row[col]) for col in ("open","high","low","close","volume"))
            if not all(math.isfinite(x) for x in (o,h,l,c,v)) or min(o,h,l,c)<=0 or v<0 or h<max(o,l,c) or l>min(o,c):
                raise ValueError("bad OHLCV")
        except (ValueError,TypeError,KeyError):
            errors["invalid_bar"]+=1
            continue
        if key not in groups and len(groups)>=MAX_INSTRUMENTS:
            errors["too_many_symbols"]+=1
            continue
        if len(groups[key])<MAX_BARS_PER_SYMBOL:
            groups[key].append({"date":day.isoformat(),"open":o,"high":h,"low":l,"close":c,"volume":v})
        else:
            errors["bars_above_limit"]+=1
    # Duplicate dates must not be silently counted as valid independent sessions.
    for key,rows in list(groups.items()):
        sorted_rows=sorted(rows,key=lambda x:x["date"])
        if len(set(x["date"] for x in sorted_rows))!=len(sorted_rows):
            errors["duplicate_session"]+=1
            del groups[key]
            continue
        groups[key]=sorted_rows
    return {"series":dict(groups),"errors":dict(errors),"input_rows":read_count,
            "verified_instrument_count":len(groups),
            "dataset_status":"USER_SUPPLIED_UNVERIFIED_SOURCE"}

def screen_bulk(uploaded, listings, market="ALL", max_age_days=7, today=None):
    """Evaluate entire loaded universe; do not call incomplete scans full coverage."""
    if market not in ("US","IN","ALL"):
        raise ValueError("Invalid market")
    today=today or dt.datetime.now(dt.timezone.utc).date()
    eligible={(r["market"],r["exchange"],str(r["symbol"]).upper()):r
              for r in listings if market=="ALL" or r["market"]==market}
    valid_records=[]
    missing_old=0
    missing_history=0
    for key,issuer in eligible.items():
        rows=uploaded["series"].get(key)
        if not rows:
            continue
        if len(rows)<60:
            missing_history+=1
            continue
        if (today-dt.date.fromisoformat(rows[-1]["date"])).days>max_age_days:
            missing_old+=1
            continue
        try:
            metrics=calculate_technicals(rows)
        except (ValueError,KeyError,TypeError,ZeroDivisionError):
            missing_history+=1
            continue
        # Screening rank only: it is NOT a prediction or recommendation.
        score=int(metrics["close"]>metrics["ma20"]>metrics["ma50"])
        score+=int(metrics["macd"]>metrics["macd_signal"])
        score+=int(40<=metrics["rsi14"]<=65)
        score+=int(metrics["illustrative_reward_risk"] is not None and metrics["illustrative_reward_risk"]>=2)
        valid_records.append({
            "market":key[0],"exchange":key[1],"symbol":key[2],"company":issuer["name"],
            "as_of":metrics["as_of"],"last_close":metrics["close"],
            "risk":metrics["risk_level"],"rsi14":metrics["rsi14"],
            "ma20":metrics["ma20"],"ma50":metrics["ma50"],
            "macd":metrics["macd"],"atr14":metrics["atr14"],
            "reward_risk_scenario":metrics["illustrative_reward_risk"],
            "screen_score_0_to_4":score,
            "assessment":"TECHNICAL_SCREEN_ONLY",
        })
    valid_records.sort(key=lambda x:(-x["screen_score_0_to_4"],x["risk"],x["market"],x["symbol"]))
    total=len(eligible)
    analyzed=len(valid_records)
    return {
        "status":"PARTIAL_UNIVERSE" if analyzed<total else "ALL_LISTED_IDENTIFIERS_ANALYZED",
        "universe_count":total,
        "symbols_with_any_uploaded_bars":sum(1 for k in eligible if k in uploaded["series"]),
        "analyzed":analyzed,
        "uncovered":total-analyzed,
        "coverage_pct":round(100*analyzed/total,2) if total else 0,
        "stale_excluded":missing_old,"short_or_invalid_excluded":missing_history,
        "source_status":uploaded["dataset_status"],
        "warnings":["Uploaded price data is user-supplied, not independently authenticated.",
                    "Universe counts depend on available official directory feeds.",
                    "Technical screen scores are heuristic and not returns forecasts or buy/sell recommendations."],
        "ranked":valid_records,
    }
