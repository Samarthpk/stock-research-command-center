import unittest
from unified_research import resolve_from_directory, provider_candidates, link_nse_to_bse_issuer, historical_ohlcv, calculate_technicals

class UnifiedResearchTests(unittest.TestCase):
    def test_legacy_india_blocks_unsupported_ticker_global_news(self):
        from intelligence import build_research
        def no_fetch(*a,**kw):raise AssertionError("No network for unknown issuer")
        result=build_research("TCL",fetch=no_fetch,market="IN")
        self.assertEqual(result["assessment"],"INSUFFICIENT_EVIDENCE")
        self.assertEqual(result["news"],[])
        self.assertTrue(any("identity" in m.lower() for m in result["missing_data"]))
    def test_ambiguous_ticker_cross_market_must_use_market(self):
        data=[{"symbol":"TCL","name":"Thaai Casting Limited","market":"IN","exchange":"NSE"},{"symbol":"TCL","name":"Transurban","market":"US","exchange":"NYSE"}]
        self.assertEqual(resolve_from_directory(data,"TCL","IN")["name"],"Thaai Casting Limited")
        self.assertEqual(resolve_from_directory(data,"TCL","US")["name"],"Transurban")
    def test_same_market_duplicate_requires_exchange(self):
        rows=[{"symbol":"A","name":"One","market":"US","exchange":"Nasdaq"},{"symbol":"A","name":"Two","market":"US","exchange":"NYSE"}]
        with self.assertRaises(ValueError):resolve_from_directory(rows,"A","US")
        self.assertEqual(resolve_from_directory(rows,"A","US","NYSE")["name"],"Two")
    def test_nse_bse_join_requires_unique_identical_isin(self):
        n={"market":"IN","exchange":"NSE","symbol":"TCL","isin":"INE0QJL01014"}
        b={"market":"IN","exchange":"BSE","symbol":"999999","isin":"INE0QJL01014"}
        self.assertEqual(link_nse_to_bse_issuer(n,[n,b]),b)
        self.assertIsNone(link_nse_to_bse_issuer(n,[n]))
        self.assertIsNone(link_nse_to_bse_issuer(n,[n,b,b]))
    def test_no_nse_ticker_guess(self):
        self.assertEqual(provider_candidates({"market":"IN","exchange":"NSE","symbol":"TCL"}),[])
        self.assertEqual(provider_candidates({"market":"IN","exchange":"BSE","symbol":"532667","trading_symbol":"SUZLON"}),["SUZLON.BSE","532667.BSE"])
    def test_reject_non_daily_data_and_bad_prices(self):
        with self.assertRaises(ValueError):historical_ohlcv({"Information":"daily limit"})
        with self.assertRaises(ValueError):historical_ohlcv({"Time Series (Daily)":{"2026-11-13":{"1. open":"-1","2. high":"12","3. low":"8","4. close":"10","5. volume":"100"}}})
    def test_uptrend_calculations_and_rr_no_recommendation(self):
        bars=[]
        for i in range(100):
            val=100+i*.3+(i%3)*.11
            bars.append({"date":f"day{i}","open":val,"high":val+1,"low":val-1,"close":val,"volume":1000+i})
        out=calculate_technicals(bars)
        for key in ["close","volume","ma20","ma50","rsi14","macd","macd_signal","atr14","annualized_volatility_pct","historical_20d_low_support","historical_20d_high_resistance","risk_level","illustrative_reward_risk"]:
            self.assertIn(key,out)
        self.assertGreater(out["rsi14"],50)
        self.assertEqual(out["decision"],"RESEARCH_ONLY_NO_RECOMMENDATION")
        self.assertGreater(out["illustrative_downside_pct"],0)
if __name__=="__main__":unittest.main()
