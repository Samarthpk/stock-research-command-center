"""Evidence-gated, out-of-sample technical setups and backtests.

Not personalized investment advice. Explicitly abstains when data are absent,
backtest sample insufficient, or estimated performance does not pass criteria.
Signal computed from session t close; entry at t+1 OPEN. No lookahead.
"""
from __future__ import annotations
import math
import statistics
from unified_research import calculate_technicals

def _closed_position(row, stop, target):
    # If stop and target hit in same bar, conservatively assume stop.
    if row["open"] <= stop:
        return float(row["open"]), "GAP_STOP"
    if row["open"] >= target:
        return float(row["open"]), "GAP_TARGET"
    if row["low"] <= stop:
        return stop, "STOP"
    if row["high"] >= target:
        return target, "TARGET"
    return None, None

def setup_from_history(history, risk_atr=1.5, reward_atr=3.0):
    if len(history)<60:
        raise ValueError("At least 60 sessions required")
    m=calculate_technicals(history)
    close=m["close"]
    atr=m["atr14"]
    if atr<=0 or not math.isfinite(atr):
        return {"status":"ABSTAIN","reason":"ATR zero or invalid"}
    trending=close>m["ma20"]>m["ma50"] and m["macd"]>m["macd_signal"] and 40<=m["rsi14"]<=70
    if not trending:
        return {"status":"NO_SETUP","reason":"Technical trend/momentum criteria not satisfied","as_of":m["as_of"]}
    stop=max(0,close-risk_atr*atr)
    target=close+reward_atr*atr
    if stop>=close or target<=close:
        return {"status":"ABSTAIN","reason":"Invalid stop/target"}
    return {"status":"CANDIDATE","signal_close":close,"signal_as_of":m["as_of"],
            "risk_atr":risk_atr,"reward_atr":reward_atr,
            "stop_distance":risk_atr*atr,
            "target_distance":reward_atr*atr,
            "estimated_rr":round(reward_atr/risk_atr,2),
            "reason":"Historical technical criteria only; does not establish fair value."}

def walk_forward_backtest(rows, *, warmup=80, holdout=60, max_holding_bars=10,
                          fee_bps=10.0, min_trades=10):
    """Last holdout sessions simulate entry next-bar open and exit via daily OHLC.

    Each trade is non-overlapping. The final candidate is assessed using
    full history but separated from backtest validation. Long-only, no shorts.
    """
    if len(rows)<warmup+holdout+1 or holdout<20 or warmup<60 or max_holding_bars<1:
        return {"status":"INSUFFICIENT_HISTORY","trades":0,"min_trades":min_trades}
    if not math.isfinite(fee_bps) or fee_bps<0:
        raise ValueError("fee_bps must be nonnegative")
    ordered=sorted(rows,key=lambda x:x["date"])
    if len(set(r["date"] for r in ordered))!=len(ordered):
        return {"status":"INVALID_HISTORY","reason":"Duplicate sessions"}
    trades=[]
    i=max(warmup,len(ordered)-holdout)
    while i<len(ordered)-1:
        historical=ordered[:i+1]
        setup=setup_from_history(historical)
        if setup["status"]!="CANDIDATE":
            i+=1
            continue
        entry_bar=ordered[i+1]
        entry=float(entry_bar["open"])
        if not math.isfinite(entry) or entry<=0:
            i+=1
            continue
        stop=max(0,entry-setup["stop_distance"])
        target=entry+setup["target_distance"]
        exit_index=None
        exit_price=None
        exit_reason=None
        for j in range(i+1,min(i+max_holding_bars+1,len(ordered))):
            price,reason=_closed_position(ordered[j],stop,target)
            if price is not None:
                exit_price=price
                exit_reason=reason
                exit_index=j
                break
        if exit_index is None:
            exit_index=min(i+max_holding_bars,len(ordered)-1)
            exit_price=float(ordered[exit_index]["close"])
            exit_reason="TIME"
        gross=exit_price/entry-1
        net=gross-(fee_bps/10000)*2
        trades.append({"signal_date":ordered[i]["date"],"entry_date":entry_bar["date"],
                       "exit_date":ordered[exit_index]["date"],"entry":round(entry,4),
                       "exit":round(exit_price,4),"return_net":net,"exit_reason":exit_reason})
        i=exit_index+1
    summary={"status":"INSUFFICIENT_TRADES","trades":len(trades),"min_trades":min_trades,
             "holdout_sessions":holdout,"fee_bps_per_side":fee_bps,
             "trade_details":trades}
    if len(trades)<min_trades:
        return summary
    returns=[t["return_net"] for t in trades]
    win_rate=sum(1 for x in returns if x>0)/len(returns)
    avg=statistics.mean(returns)
    equity=1.0
    peak=1.0
    worst=0.0
    for ret in returns:
        equity*=1+ret
        peak=max(peak,equity)
        worst=min(worst,equity/peak-1)
    summary.update({
        "status":"VALIDATED_HOLDOUT" if avg>0 and len(trades)>=min_trades else "FAILED_HOLDOUT",
        "win_rate_pct":round(100*win_rate,2),
        "average_net_return_pct":round(100*avg,2),
        "compounded_net_return_pct":round(100*(equity-1),2),
        "max_trade_equity_drawdown_pct":round(100*worst,2),
        "method":"Walk-forward last holdout sessions; next-open entry; pessimistic same-bar stop priority; costs; nonoverlapping trades.",
        "limitations":"Not fully calibrated. Survivorship bias, slippage, bid/ask, corporate actions, borrow fees, taxes and changing regimes may affect realized performance.",
    })
    return summary

def evaluate_signal(rows, min_trades=10, holdout=60):
    live=setup_from_history(rows)
    bt=walk_forward_backtest(rows,holdout=holdout,min_trades=min_trades)
    if live["status"]!="CANDIDATE":
        recommendation="NO_BUY_SETUP"
        reason="No qualifying technical setup at the latest available daily close."
    elif bt.get("status")!="VALIDATED_HOLDOUT":
        recommendation="WATCH_ONLY_NOT_VALIDATED"
        reason="Technical candidate exists, but holdout evidence does not support a validated signal."
    else:
        recommendation="BACKTEST_SUPPORTED_CANDIDATE"
        reason="Positive historical holdout results, not a guaranteed profitable buy or personalized suitability recommendation."
    return {"screening_signal":recommendation,"reason":reason,
            "latest_setup":live,"backtest":bt,
            "disclaimer":"For research only. No automatic orders. Backtest performance does not predict future returns."}
