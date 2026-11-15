import datetime as dt
import io
import math
import unittest
from market_quotes import validate_quote,alpha_vantage_us_realtime,licensed_gateway_quote
from market_scanner import validate_bulk_upload,screen_bulk
from signal_validation import walk_forward_backtest,evaluate_signal

class QuoteTests(unittest.TestCase):
    def test_reject_mismatched_ticker_and_unverified_time(self):
        now=dt.datetime(2026,11,15,12,0,tzinfo=dt.timezone.utc)
        data={"market":"US","exchange":"Nasdaq","symbol":"AAPL","currency":"USD","price":250,
              "as_of":"2026-11-15T11:59:55Z","source":"Licensed X","feed_type":"REALTIME"}
        self.assertEqual(validate_quote(data,symbol="AAPL",market="US",exchange="Nasdaq",now=now)["display_status"],"REALTIME")
        with self.assertRaises(ValueError):validate_quote(data,symbol="MSFT",market="US",exchange="Nasdaq",now=now)
        with self.assertRaises(ValueError):validate_quote({**data,"as_of":"2026-11-15"},symbol="AAPL",market="US",exchange="Nasdaq",now=now)
        old=validate_quote({**data,"as_of":"2026-11-15T11:30:00Z"},symbol="AAPL",market="US",exchange="Nasdaq",now=now)
        self.assertEqual(old["display_status"],"STALE_REALTIME_FEED")

    def test_realtime_entitlement_required_and_date_only_not_verified(self):
        self.assertEqual(alpha_vantage_us_realtime("AAPL",api_key="demo")["status"],"NOT_ENTITLED")
        quote={"Global Quote":{"01. symbol":"AAPL","05. price":"200.1","07. latest trading day":"2026-11-13"}}
        data=alpha_vantage_us_realtime("AAPL",api_key="demo",entitlement_enabled=True,get_json=lambda url:quote)
        self.assertEqual(data["status"],"FRESHNESS_NOT_VERIFIABLE")

    def test_gateway_requires_https_and_verifies_quote(self):
        issuer={"market":"US","exchange":"Nasdaq","symbol":"AAPL"}
        self.assertEqual(licensed_gateway_quote(issuer,endpoint="http://x.test/api",token="token")["status"],"INVALID_ENDPOINT")
        mismatched={"market":"US","exchange":"Nasdaq","symbol":"MSFT","currency":"USD","price":300,
                    "as_of":"2026-11-15T11:00:00Z","source":"Licensed X","feed_type":"REALTIME"}
        self.assertEqual(licensed_gateway_quote(issuer,endpoint="https://vendor.example/api",token="token",fetch=lambda url,key:mismatched)["status"],"REJECTED")

class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.issuer={"market":"US","exchange":"Nasdaq","symbol":"AAPL","name":"Apple Inc."}
        self.second={"market":"US","exchange":"Nasdaq","symbol":"MSFT","name":"Microsoft"}
        self.today=dt.date(2026,11,15)
    def rows(self,days=160):
        start=dt.date(2026,5,1)
        head="market,exchange,symbol,date,open,high,low,close,volume\n"
        out=[]
        for i in range(days):
            date=(start+dt.timedelta(days=i)).isoformat()
            val=100+i*.12+(i%5)*.03
            out.append(f"US,Nasdaq,AAPL,{date},{val},{val+2},{val-2},{val},1000")
        return (head+"\n".join(out)+"\n").encode()
    def test_universe_count_and_coverage_never_fake_full(self):
        parsed=validate_bulk_upload(self.rows(),[self.issuer,self.second])
        outcome=screen_bulk(parsed,[self.issuer,self.second],market="US",today=self.today,max_age_days=40)
        self.assertEqual(outcome["universe_count"],2)
        self.assertEqual(outcome["analyzed"],1)
        self.assertEqual(outcome["uncovered"],1)
        self.assertEqual(outcome["status"],"PARTIAL_UNIVERSE")
        self.assertEqual(outcome["coverage_pct"],50)
    def test_unlisted_row_rejected_not_analyzed(self):
        raw=b"market,exchange,symbol,date,open,high,low,close,volume\nUS,Nasdaq,FAKE,2026-11-13,1,2,0.5,1.5,100\n"
        parsed=validate_bulk_upload(raw,[self.issuer])
        self.assertEqual(parsed["errors"]["unverified_or_ambiguous_issuer"],1)
        self.assertEqual(len(parsed["series"]),0)
    def test_duplicate_days_rejected(self):
        raw=b"market,exchange,symbol,date,open,high,low,close,volume\nUS,Nasdaq,AAPL,2026-11-13,1,2,0.5,1.5,100\nUS,Nasdaq,AAPL,2026-11-13,1,2,0.5,1.5,100\n"
        parsed=validate_bulk_upload(raw,[self.issuer])
        self.assertEqual(parsed["verified_instrument_count"],0)
        self.assertEqual(parsed["errors"]["duplicate_session"],1)

class BacktestTests(unittest.TestCase):
    def bars(self,n=250,flat=False):
        out=[]
        for i in range(n):
            c=100 if flat else 100+i*.22+(i%7)*.09
            day=(dt.date(2025,1,1)+dt.timedelta(days=i)).isoformat()
            out.append({"date":day,"open":c,"high":c+1,"low":c-1,"close":c,"volume":1000})
        return out
    def test_abstention_when_inadequate_bars(self):
        result=evaluate_signal(self.bars(75))
        self.assertIn(result["screening_signal"],("NO_BUY_SETUP","WATCH_ONLY_NOT_VALIDATED"))
        self.assertEqual(result["backtest"]["status"],"INSUFFICIENT_HISTORY")
    def test_next_open_date_and_no_overlap(self):
        out=walk_forward_backtest(self.bars(220),holdout=80,min_trades=1)
        for tr in out["trade_details"]:
            self.assertGreater(tr["entry_date"],tr["signal_date"])
            self.assertGreaterEqual(tr["exit_date"],tr["entry_date"])
    def test_fee_changes_net_returns(self):
        a=walk_forward_backtest(self.bars(220),holdout=80,min_trades=1,fee_bps=0)
        b=walk_forward_backtest(self.bars(220),holdout=80,min_trades=1,fee_bps=25)
        if a["trades"]:
            self.assertLess(b["trade_details"][0]["return_net"],a["trade_details"][0]["return_net"])
    def test_no_guaranteed_buy_on_flat_series(self):
        r=evaluate_signal(self.bars(200,flat=True))
        self.assertEqual(r["screening_signal"],"NO_BUY_SETUP")
if __name__=="__main__":
    unittest.main()
