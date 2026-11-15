"""Deterministic risk decision and sizing regression tests; no network or credentials."""
import unittest
from risk_decision import assess_record, position_scenario

class TestRiskDecision(unittest.TestCase):
    def test_no_trade_when_missing_price(self):
        rec={"ticker":"ITC","market":"IN","price":{"status":"NOT_CONFIGURED"}}
        out=assess_record(rec)
        self.assertEqual(out["risk_level"],"UNKNOWN")
        self.assertIsNone(out["forecast"])
        self.assertIsNone(out["buy_zone"])
        self.assertIsNone(out["sell_zone"])
        self.assertEqual(out["decision"],"NO_TRADE_INSUFFICIENT_EVIDENCE")

    def test_no_trade_when_discrepancy(self):
        rec={"ticker":"AAPL","price":{"status":"OK","ohlcv_verified":True,"close":120,"volatility_pct":20,"data_discrepancy":{"status":"UNRESOLVED"}}}
        self.assertEqual(assess_record(rec)["risk_level"],"UNKNOWN")

    def test_verified_volatility_diagnostic_never_becomes_recommendation(self):
        rec={"ticker":"AAPL","market":"US","currency":"USD","quality":"PRIMARY_EVIDENCE_PRESENT","price":{"status":"OK","ohlcv_verified":True,"close":100,"ma20":95,"ma50":90,"volatility_pct":50,"as_of":"2026-11-13"}}
        out=assess_record(rec)
        self.assertEqual(out["risk_level"],"HIGH")
        self.assertEqual(out["decision"],"NO_TRADE_INSUFFICIENT_EVIDENCE")
        self.assertEqual(out["historical_annualized_vol_pct"],50)
        self.assertIsNone(out["forecast"])
        self.assertIsNone(out["buy_zone"])
        self.assertEqual(len(out["illustrative_volatility_band"]),2)
        self.assertGreater(len(out["bull_facts"]),0)

    def test_risk_sizing_allocation_and_stop_constraints(self):
        out=position_scenario(1000,100,95,risk_fraction=0.01,max_allocation_fraction=0.10)
        self.assertEqual(out["shares"],1)
        self.assertEqual(out["allocated_capital"],100)
        self.assertEqual(out["planned_risk"],5)
        self.assertLessEqual(out["planned_risk"],out["risk_budget"])

    def test_invalid_inputs_not_sized(self):
        for args in [(1000,95,100),(1000,100,100),(-5,100,90),(1000,100,float("nan"))]:
            self.assertEqual(position_scenario(*args)["status"],"INVALID_INPUT")

if __name__=="__main__":
    unittest.main()
