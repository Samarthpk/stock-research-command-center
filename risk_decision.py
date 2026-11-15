"""Evidence-based risk diagnostics, not personalized investment advice.

Uses only validated historical daily price metadata in autonomous mission reports.
Bands are volatility-based illustrative scenarios, NOT calibrated forecasts, support,
resistance, target prices, or automatic buy/sell recommendations.
"""
from __future__ import annotations
import math

def _number(value):
    try:
        val = float(value)
        return val if math.isfinite(val) else None
    except (ValueError, TypeError):
        return None

def position_scenario(capital, entry, invalidation, risk_fraction=0.01, max_allocation_fraction=0.10):
    """Conservative integer-share sizing, excluding gaps, costs, FX and slippage."""
    cap, buy, stop, risk, alloc = map(_number, (capital, entry, invalidation, risk_fraction, max_allocation_fraction))
    if any(x is None for x in (cap, buy, stop, risk, alloc)):
        return {"status": "INVALID_INPUT"}
    if not (cap > 0 and buy > stop >= 0 and 0 < risk <= 0.05 and 0 < alloc <= 1):
        return {"status": "INVALID_INPUT"}
    per_share = buy - stop
    shares = max(0, min(math.floor(cap * risk / per_share), math.floor(cap * alloc / buy)))
    return {
        "status": "ILLUSTRATIVE_ONLY",
        "shares": shares,
        "allocated_capital": round(shares * buy, 2),
        "planned_risk": round(shares * per_share, 2),
        "risk_budget": round(cap * risk, 2),
        "allocation_limit": round(cap * alloc, 2),
        "warning": "Stops may gap; actual loss can exceed planned risk. Currency, fees, spreads and slippage excluded.",
    }

def assess_record(record, horizon_sessions=20):
    """Risk diagnostic with hard evidence gates; no unverified buy or price target."""
    price = record.get("price") or {}
    close = _number(price.get("close"))
    vol = _number(price.get("volatility_pct"))
    asof = price.get("as_of")
    safe = (price.get("status") == "OK" and
            bool(price.get("ohlcv_verified")) and
            not price.get("data_discrepancy") and
            close is not None and close > 0 and
            vol is not None and vol >= 0 and
            isinstance(horizon_sessions, int) and 1 <= horizon_sessions <= 252)
    base = {
        "ticker": record.get("ticker", "UNKNOWN"),
        "market": record.get("market", "US"),
        "currency": record.get("currency", price.get("currency", "USD")),
        "as_of": asof,
        "research_outcome": record.get("research_outcome"),
        "evidence_quality": record.get("quality"),
        "bull_facts": [],
        "bear_facts": [],
        "decision": "NO_TRADE_INSUFFICIENT_EVIDENCE",
        "risk_level": "UNKNOWN",
        "buy_zone": None,
        "sell_zone": None,
        "forecast": None,
        "reason": "Historical price evidence is incomplete, disputed, or lacks verified volatility.",
    }
    if not safe:
        return base
    # A screening band is a normal-volatility scaling heuristic. It is NOT
    # a forecast interval or quantified likelihood without backtesting.
    horizon_move = (vol / 100.0) * math.sqrt(horizon_sessions / 252.0)
    down, up = max(0, close * (1 - horizon_move)), close * (1 + horizon_move)
    risk_level = ("VERY HIGH" if vol >= 70 else
                  "HIGH" if vol >= 45 else
                  "MODERATE" if vol >= 25 else "LOW HISTORICAL VOLATILITY")
    ma20, ma50 = _number(price.get("ma20")), _number(price.get("ma50"))
    bulls, bears = [], []
    if ma20 is not None and ma50 is not None:
        if close > ma20 > ma50:
            bulls.append("Close above MA20 and MA20 above MA50; historical trend is positive.")
        elif close < ma20 < ma50:
            bears.append("Close below MA20 and MA20 below MA50; historical trend is negative.")
        else:
            bears.append("Moving averages are mixed; no clear trend confirmation.")
    if vol >= 45:
        bears.append("High realized annualized volatility; drawdowns and gaps can be substantial.")
    else:
        bulls.append("Recent realized volatility is below the configured high-volatility screen.")
    if record.get("quality") != "PRIMARY_EVIDENCE_PRESENT":
        bears.append("Verified primary filings and/or company financial metrics are incomplete.")
    base.update({
        "risk_level": risk_level,
        "bull_facts": bulls,
        "bear_facts": bears,
        "reference_close": round(close, 4),
        "historical_annualized_vol_pct": round(vol, 2),
        "horizon_sessions": horizon_sessions,
        "illustrative_volatility_band": [round(down, 2), round(up, 2)],
        "reason": "Historical-volatility risk screen only. No calibrated forecast, investment recommendation, or validated buy/sell zone.",
    })
    return base
