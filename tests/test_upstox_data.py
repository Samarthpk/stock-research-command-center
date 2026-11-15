import datetime as dt
import unittest
from upstox_data import (parse_instruments,load_instruments,search_instruments,
                         parse_quote,fetch_quote,parse_daily_candles,fetch_daily,
                         UpstoxDataError,BOD_NSE,BOD_BSE)
from unified_research import calculate_technicals

TCL={"segment":"NSE_EQ","exchange":"NSE","name":"THAAI CASTING LIMITED",
     "instrument_type":"EQ","trading_symbol":"TCL","isin":"INE0QJL01014",
     "instrument_key":"NSE_EQ|INE0QJL01014","exchange_token":"25001"}
SUZLON={"segment":"NSE_EQ","exchange":"NSE","name":"SUZLON ENERGY LIMITED",
        "instrument_type":"EQ","trading_symbol":"SUZLON","isin":"INE040H01021",
        "instrument_key":"NSE_EQ|INE040H01021","exchange_token":"12018"}

class UpstoxTests(unittest.TestCase):
    def test_instrument_master_prevents_tcl_foreign_collision(self):
        rows=parse_instruments([TCL,SUZLON,{"segment":"NSE_FO","exchange":"NSE","name":"TCL Options",
            "trading_symbol":"TCL","instrument_key":"NSE_FO|1","isin":"INE0QJL01014"}],"NSE")
        self.assertEqual(len(rows),2)
        match=search_instruments(rows,"TCL")
        self.assertEqual(len(match),1)
        self.assertEqual(match[0]["name"],"THAAI CASTING LIMITED")
        self.assertEqual(match[0]["instrument_key"],"NSE_EQ|INE0QJL01014")
    def test_instrument_identity_and_non_equity_filtered(self):
        invalid=dict(SUZLON,instrument_key="NSE_EQ|DIFFERENT")
        self.assertEqual(parse_instruments([invalid],"NSE"),[])
    def test_partial_source_status_visible(self):
        def source(url):
            if url==BOD_NSE:return [TCL]
            raise UpstoxDataError("BSE down")
        data=load_instruments(fetch=source)
        self.assertEqual(data["source_health"]["NSE"]["status"],"OK")
        self.assertEqual(data["source_health"]["BSE"]["status"],"UNAVAILABLE")
    def test_quote_identity_and_freshness(self):
        data={"status":"success","data":{"NSE_EQ:TCL":{
            "instrument_token":TCL["instrument_key"],"symbol":"TCL",
            "timestamp":"2026-11-16T09:30:00+05:30","last_price":148.1,"volume":1000}}}
        issuer={**TCL,"symbol":"TCL"}
        now=dt.datetime(2026,11,16,4,0,45,tzinfo=dt.timezone.utc)
        quote=parse_quote(data,issuer,now=now)
        self.assertEqual(quote["quote_status"],"FRESH_SNAPSHOT")
        self.assertEqual(quote["price"],148.1)
        wrong={"status":"success","data":{"NSE_EQ:OTHER":{**data["data"]["NSE_EQ:TCL"],"instrument_token":"NSE_EQ|OTHER"}}}
        with self.assertRaises(UpstoxDataError):parse_quote(wrong,issuer,now=now)
        old=parse_quote(data,issuer,now=now+dt.timedelta(hours=1))
        self.assertEqual(old["quote_status"],"LAST_REPORTED_NOT_LIVE")
    def test_quotes_use_read_only_get_and_token(self):
        events=[]
        def fetch(url,token):
            events.append((url,token))
            return {"status":"success","data":{"NSE_EQ:TCL":{
                "instrument_token":TCL["instrument_key"],"symbol":"TCL",
                "timestamp":"2026-11-16T09:30:00+05:30","last_price":151.0,"volume":123}}}
        out=fetch_quote({**TCL,"symbol":"TCL"},"hidden-test-token",fetch=fetch)
        self.assertEqual(out["symbol"],"TCL")
        self.assertIn("/v3/market-quote/quotes?",events[0][0])
        self.assertEqual(events[0][1],"hidden-test-token")
    def test_daily_candles_and_indicator_pipeline(self):
        start=dt.date(2026,1,1)
        candles=[]
        for i in range(110):
            day=(start+dt.timedelta(days=i)).isoformat()
            price=100+i*0.15+(i%3)*0.03
            candles.append([day+"T00:00:00+05:30",price,price+2,price-2,price,1000+i,0])
        rows=parse_daily_candles({"status":"success","data":{"candles":list(reversed(candles))}})
        self.assertEqual(len(rows),110)
        self.assertEqual(rows[0]["date"],start.isoformat())
        self.assertIn("rsi14",calculate_technicals(rows))
    def test_missing_candles_rejected(self):
        with self.assertRaises(UpstoxDataError):
            parse_daily_candles({"status":"success","data":{"candles":[]}})
    def test_historical_url_is_v3_days(self):
        events=[]
        def fake(url,token):
            events.append(url)
            return {"status":"success","data":{"candles":[]}}
        with self.assertRaises(UpstoxDataError):
            fetch_daily(TCL,"hidden-test-token",fetch=fake,today=dt.date(2026,11,15))
        self.assertIn("/v3/historical-candle/NSE_EQ%7CINE0QJL01014/days/1/",events[0])
    def test_market_data_never_requires_post_orders(self):
        import inspect,upstox_data
        code=inspect.getsource(upstox_data)
        self.assertNotIn("place_order",code)
        self.assertNotIn("order_modify",code)

if __name__=="__main__":
    unittest.main()
