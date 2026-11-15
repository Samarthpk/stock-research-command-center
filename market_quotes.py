"""Quote adapters with strict entitlement, identity and freshness checks.

Free Alpha Vantage global quotes are END-OF-DAY, not realtime.
US realtime is requested only with explicit licensed entitlement.
For US/India at scale, a licensed HTTPS quote gateway can implement the
documented JSON schema; arbitrary data is never relabeled realtime.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

QUOTES_DOC="https://www.alphavantage.co/documentation/#latestprice"
def validate_quote(payload, *, symbol, exchange, market, max_age_seconds=120, now=None):
    """Contract: {symbol,exchange,market,currency,price,as_of,source,feed_type}."""
    if not isinstance(payload,dict):
        raise ValueError("Quote must be a JSON object")
    if payload.get("symbol") != symbol or payload.get("exchange") != exchange or payload.get("market") != market:
        raise ValueError("Quote issuer identity or exchange does not match requested security")
    currency="USD" if market=="US" else "INR"
    if payload.get("currency") != currency:
        raise ValueError("Quote currency mismatched")
    if payload.get("feed_type") not in ("REALTIME","DELAYED","EOD"):
        raise ValueError("Quote feed_type missing or invalid")
    try:
        price=float(payload["price"])
        timestamp=dt.datetime.fromisoformat(str(payload["as_of"]).replace("Z","+00:00"))
    except (ValueError,TypeError,KeyError):
        raise ValueError("Quote price/timestamp invalid") from None
    if not math.isfinite(price) or price<=0 or timestamp.tzinfo is None:
        raise ValueError("Quote price must be positive and timestamp timezone-aware")
    now=now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Current time must be timezone-aware")
    delta=(now-timestamp).total_seconds()
    if delta< -30:
        raise ValueError("Quote timestamp in future")
    if not isinstance(payload.get("source"),str) or not payload["source"].strip():
        raise ValueError("Quote source identifier missing")
    fresh=0<=delta<=max_age_seconds
    label=payload["feed_type"]
    if label=="REALTIME" and not fresh:
        label="STALE_REALTIME_FEED"
    return {**payload,"price":price,"as_of":timestamp.isoformat(),"seconds_old":int(delta),"verified_fresh":fresh,"display_status":label}

def alpha_vantage_us_realtime(symbol, *, api_key, entitlement_enabled=False, get_json=None):
    """Premium-US quote. Never use an unentitled EOD quote as realtime."""
    if not entitlement_enabled:
        return {"status":"NOT_ENTITLED","reason":"Realtime US quote entitlement must be enabled by provider. Free key gives EOD data."}
    if not api_key:
        return {"status":"NO_API_KEY"}
    if not re.fullmatch(r"[A-Z][A-Z0-9.\\-]{0,9}",symbol):
        return {"status":"INVALID_SYMBOL"}
    url="https://www.alphavantage.co/query?"+urllib.parse.urlencode({
        "function":"GLOBAL_QUOTE","symbol":symbol,"entitlement":"realtime","apikey":api_key,
    })
    def default_request(target):
        req=urllib.request.Request(target,headers={"User-Agent":"StockResearchCommandCenter/0.7"})
        with urllib.request.urlopen(req,timeout=15) as response:
            return json.loads(response.read(500_000))
    try:
        obj=(get_json or default_request)(url)
        if any(k in obj for k in ("Information","Note","Error Message")):
            return {"status":"PROVIDER_UNAVAILABLE","reason":"Entitlement, quota, or provider rejection"}
        q=obj.get("Global Quote") or {}
        if q.get("01. symbol") != symbol:
            return {"status":"PROVIDER_IDENTITY_MISMATCH"}
        px=float(q["05. price"])
        trading_day=str(q["07. latest trading day"])
        if not math.isfinite(px) or px<=0:
            raise ValueError("invalid quote price")
        # GLOBAL_QUOTE only provides a trading DATE. It does not return a
        # verified exchange timestamp, so cannot be proved realtime.
        return {"status":"FRESHNESS_NOT_VERIFIABLE","price":px,"trading_day":trading_day,
                "source":"Alpha Vantage GLOBAL_QUOTE entitlement=realtime",
                "reason":"Provider response lacks a time-of-trade timestamp. Never label as verified realtime."}
    except (ValueError,TypeError,KeyError,TimeoutError,OSError):
        return {"status":"PROVIDER_UNAVAILABLE","reason":"Quote unavailable or malformed"}

def licensed_gateway_quote(issuer, *, endpoint, token, fetch=None):
    """Caller-configured licensed HTTPS gateway with strict issuer and timestamp contract.

    Endpoint is an administrator-configured fixed HTTPS API URL (not user input).
    Auth is Bearer token; secrets never sent in query strings or app reports.
    """
    if not endpoint or not token:
        return {"status":"NOT_CONFIGURED","reason":"Licensed gateway URL and token required"}
    parsed=urllib.parse.urlparse(endpoint)
    if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        return {"status":"INVALID_ENDPOINT"}
    if parsed.hostname in ("localhost","127.0.0.1","::1") or parsed.hostname.endswith((".local",".internal")):
        return {"status":"INVALID_ENDPOINT"}
    params=urllib.parse.urlencode({"market":issuer["market"],"exchange":issuer["exchange"],"symbol":issuer["symbol"]})
    url=endpoint+("&" if parsed.query else "?")+params
    def default_fetch(target,api_token):
        request=urllib.request.Request(target,headers={
            "Accept":"application/json","Authorization":"Bearer "+api_token,
            "User-Agent":"StockResearchCommandCenter/0.7",
        })
        with urllib.request.urlopen(request,timeout=12) as response:
            return json.loads(response.read(200_000))
    try:
        reply=(fetch or default_fetch)(url,token)
        result=validate_quote(reply,symbol=issuer["symbol"],exchange=issuer["exchange"],market=issuer["market"])
        return {"status":"OK" if result["display_status"]=="REALTIME" and result["verified_fresh"] else "NOT_VERIFIED_REALTIME", "quote":result}
    except (ValueError,KeyError,TypeError,OSError) as exc:
        return {"status":"REJECTED","reason":type(exc).__name__}
