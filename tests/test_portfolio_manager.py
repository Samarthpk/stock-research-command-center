import datetime as dt
import json
import unittest
from portfolio_manager import (validate_holding,parse_holdings_csv,summary,
                               validate_event,events_summary,export_backup,import_backup)
TCL={"instrument_key":"NSE_EQ|INE0QJL01014","symbol":"TCL",
     "company":"THAAI CASTING LIMITED","name":"THAAI CASTING LIMITED","exchange":"NSE",
     "isin":"INE0QJL01014"}
HG={"instrument_key":"NSE_EQ|INE926X01010","symbol":"HGINFRA",
    "name":"H.G.INFRA ENGINEERING LTD","exchange":"NSE","isin":"INE926X01010"}

class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.directory=[TCL,HG]
    def test_identity_rejects_short_ticker_and_wrong_company(self):
        with self.assertRaises(ValueError):
            validate_holding({"exchange":"NSE","symbol":"TCL","quantity":10,"average_price":100},self.directory)
        with self.assertRaises(ValueError):
            validate_holding({"exchange":"NSE","isin":TCL["isin"],"symbol":"HGINFRA","quantity":10,"average_price":100},self.directory)
        with self.assertRaises(ValueError):
            validate_holding({"exchange":"BSE","isin":TCL["isin"],"quantity":10,"average_price":100},self.directory)
    def test_cash_and_unrealized_pnl(self):
        a=validate_holding({"exchange":"NSE","isin":TCL["isin"],"quantity":10,"average_price":100,"sector":"Industrials"},self.directory)
        b=validate_holding({"exchange":"NSE","isin":HG["isin"],"quantity":5,"average_price":500,"sector":"Infrastructure"},self.directory)
        q={"NSE_EQ|INE0QJL01014":{"instrument_key":TCL["instrument_key"],"price":120,"timestamp":"2026-11-14T10:00:00+05:30","quote_status":"LAST_REPORTED_NOT_LIVE"},
           "NSE_EQ|INE926X01010":{"instrument_key":HG["instrument_key"],"price":400,"timestamp":"2026-11-14T10:00:00+05:30","quote_status":"LAST_REPORTED_NOT_LIVE"}}
        report=summary([a,b],q)
        self.assertEqual(report["invested_total"],3500)
        self.assertEqual(report["priced_market_value"],3200)
        self.assertEqual(report["priced_unrealized_pnl"],-300)
        self.assertEqual(report["priced_unrealized_pnl_pct"],round(-300/3500*100,2))
        self.assertAlmostEqual(report["top_stock_weight_pct"],62.5)
        self.assertTrue(report["all_holdings_priced"])
        self.assertEqual(report["scenario_loss_on_priced_value"],640)
        self.assertEqual(report["sector_values"]["Infrastructure"],2000)
    def test_missing_quote_must_not_be_assumed_zero_value(self):
        a=validate_holding({"exchange":"NSE","isin":TCL["isin"],"quantity":10,"average_price":100},self.directory)
        r=summary([a],{})
        self.assertFalse(r["all_holdings_priced"])
        self.assertEqual(r["unpriced_positions"],1)
        self.assertIsNone(r["holdings"][0]["market_value"])
    def test_csv_requires_verified_isin_and_rejects_partial_import(self):
        raw=b"exchange,isin,quantity,average_price,sector\nNSE,INE926X01010,10,450,Infrastructure\nNSE,INVALIDISIN,10,400,Unknown\n"
        out=parse_holdings_csv(raw,self.directory)
        self.assertEqual(out["accepted"],0)
        self.assertEqual(out["rejected"],1)
        self.assertFalse(out["holdings"])
    def test_csv_weighted_average_on_duplicate_lots(self):
        raw=b"exchange,isin,quantity,average_price\nNSE,INE0QJL01014,10,100\nNSE,INE0QJL01014,30,200\n"
        result=parse_holdings_csv(raw,self.directory)
        self.assertEqual(result["accepted"],2)
        self.assertEqual(len(result["holdings"]),1)
        self.assertEqual(result["holdings"][0]["quantity"],40)
        self.assertEqual(result["holdings"][0]["average_price"],175)
    def test_events_dividends_and_backup_roundtrip(self):
        holding=validate_holding({"exchange":"NSE","isin":TCL["isin"],"quantity":10,"average_price":100},self.directory)
        date=dt.datetime.now(dt.timezone.utc).date().isoformat()
        dividend=validate_event({"date":date,"event":"DIVIDEND","instrument_key":TCL["instrument_key"],"cash_amount":50},self.directory)
        fee=validate_event({"date":date,"event":"FEE","cash_amount":7},self.directory)
        ledger=events_summary([dividend,fee])
        self.assertEqual(ledger["dividends_logged"],50)
        self.assertEqual(ledger["fees_and_taxes_logged"],7)
        restored,events=import_backup(export_backup([holding],[dividend,fee]).encode(),self.directory)
        self.assertEqual(restored[0]["symbol"],"TCL")
        self.assertEqual(len(events),2)
    def test_quote_identity_cannot_be_reassigned(self):
        a=validate_holding({"exchange":"NSE","isin":TCL["isin"],"quantity":10,"average_price":100},self.directory)
        with self.assertRaises(ValueError):
            summary([a],{TCL["instrument_key"]:{"instrument_key":HG["instrument_key"],"price":120}})

if __name__=="__main__":unittest.main()
