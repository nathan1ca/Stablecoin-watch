#!/usr/bin/env python3
"""최근 30일 온체인 이체 건수 가공 테스트. 네트워크 없음."""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "etl"))

import fetch_onchain_tx as ot  # noqa: E402


def series(n, tx, active=None, start=date(2026, 8, 1)):
    return [{"asset": "x", "time": f"{start + timedelta(days=i)}T00:00:00.000000000Z",
             "TxTfrCnt": str(tx), **({"AdrActCnt": str(active)} if active is not None else {})}
            for i in range(n)]


class TestOnchainTx(unittest.TestCase):
    def test_summary_and_change(self):
        rows = series(30, 100) + series(30, 150, 40, start=date(2026, 8, 31))
        r = ot.summarize("usdt_eth", "USDT", "Ethereum", ot.parse_rows(rows))
        self.assertEqual(r["tx_30d"], 4500)
        self.assertEqual(r["tx_prev_30d"], 3000)
        self.assertEqual(r["chg_pct"], 50.0)
        self.assertEqual(r["tx_avg"], 150)
        self.assertEqual(r["active_avg"], 40)
        self.assertEqual(len(r["daily"]), 30)

    def test_short_history_no_change(self):
        r = ot.summarize("pyusd_eth", "PYUSD", "Ethereum", ot.parse_rows(series(40, 10)))
        self.assertIsNone(r["chg_pct"])      # 직전 30일이 다 차지 않으면 비교하지 않음
        self.assertEqual(r["tx_30d"], 300)

    def test_bad_values_skipped(self):
        rows = series(2, 5) + [{"time": "2026-08-03T00:00:00Z", "TxTfrCnt": None}]
        self.assertEqual(len(ot.parse_rows(rows)), 2)

    def test_build_status_and_totals(self):
        res = {"usdt_eth": series(30, 100), "usdt_trx": series(30, 300), "usdc_eth": RuntimeError("403"),
               "dai": []}
        b = ot.build(res)
        self.assertEqual([r["code"] for r in b["rows"]], ["usdt_trx", "usdt_eth"])
        self.assertEqual(b["totals"]["by_symbol"]["USDT"], 12000)
        self.assertTrue(b["meta"]["status"]["usdc_eth"].startswith("실패"))
        self.assertEqual(b["meta"]["status"]["dai"], "데이터 없음")
        self.assertIn("Solana", b["meta"]["chains_missing"])


if __name__ == "__main__":
    unittest.main()
