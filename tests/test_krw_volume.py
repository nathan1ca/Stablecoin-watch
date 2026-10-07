#!/usr/bin/env python3
"""국내 원화마켓 스테이블코인 거래대금 가공 테스트. 네트워크 없음."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "etl"))

import fetch_krw_volume as kv  # noqa: E402


class TestParse(unittest.TestCase):
    def test_upbit_style(self):
        rows = [{"candle_date_time_kst": "2026-10-07T00:00:00", "candle_acc_trade_price": 1.5e9, "trade_price": 1357},
                {"candle_date_time_kst": "bad"}]
        self.assertEqual(kv.parse_upbit_style(rows), {"2026-10-07": {"krw": 1.5e9, "close": 1357.0}})

    def test_coinone_kst_date(self):
        # 1791331200000 = 2026-10-06T15:00:00Z = 2026-10-07 00:00 KST
        r = {"chart": [{"timestamp": 1791331200000, "quote_volume": "26567353384.96", "close": "1357.0"}]}
        self.assertEqual(kv.parse_coinone(r)["2026-10-07"]["krw"], 26567353384.96)


class TestBuild(unittest.TestCase):
    def series(self, n, v):
        return {f"2026-09-{d:02d}": {"krw": v, "close": 1350} for d in range(1, n + 1)}

    def test_summary_and_partial(self):
        raw = {("upbit", "USDT"): {**self.series(30, 3e9), "2026-10-01": {"krw": 1e9, "close": 1}},
               ("upbit", "USDC"): {**self.series(30, 1e9), "2026-10-01": {"krw": 1e8, "close": 1}},
               ("bithumb", "USDT"): RuntimeError("down")}
        r = kv.build(raw, today="2026-10-01")
        self.assertEqual(r["meta"]["status"]["bithumb"]["USDT"][:2], "실패")
        self.assertTrue(r["daily"][-1]["partial"])               # 오늘은 진행 중
        s = r["summary"]
        self.assertEqual(s["last_full_date"], "2026-09-30")
        self.assertEqual(s["avg30_krw"], 4e9)
        self.assertEqual(s["share30_pct"], {"upbit": 100.0})
        self.assertEqual(s["asset_share30_pct"], {"USDT": 75.0, "USDC": 25.0})
        self.assertIsNone(s["chg30_pct"])                        # 직전 30일 없음

    def test_days_missing_an_exchange_are_dropped(self):
        raw = {("upbit", "USDT"): self.series(5, 1e9), ("coinone", "USDT"): self.series(3, 1e9)}
        r = kv.build(raw, today="2026-10-01")
        self.assertEqual([d["date"] for d in r["daily"]], ["2026-09-01", "2026-09-02", "2026-09-03"])
        self.assertEqual(r["daily"][0]["total_krw"], 2e9)


if __name__ == "__main__":
    unittest.main()
