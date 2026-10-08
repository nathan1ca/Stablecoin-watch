#!/usr/bin/env python3
"""교차 가격(CoinGecko) 대상 선정과 종목 id 기준 대응 테스트. 네트워크 없음.

2026-10-08 스냅숏에서 경보 4종(apxUSD −139bp·Binance-Peg BUSD +106bp·FRAX −139bp·
AP USDA −964bp)은 모두 가격 출처가 DefiLlama 하나뿐이었다. 교차검증 대상이
'발행잔액 상위 30종 중 수기 대조표 12종'이라, 정작 경보로 뜨는 작은 종목은 두 번째
가격으로 확인되지 않았다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "etl"))

import fetch  # noqa: E402


def asset(i, sym, circ, price, gecko=None, cur="USD"):
    key = "pegged" + cur
    a = {
        "id": str(i), "name": sym, "symbol": sym, "pegType": key,
        "pegMechanism": "fiat-backed", "price": price,
        "circulating": {key: circ},
        "circulatingPrevDay": {key: circ},
        "circulatingPrevWeek": {key: circ},
        "circulatingPrevMonth": {key: circ},
        "chainCirculating": {},
    }
    if gecko:
        a["gecko_id"] = gecko
    return a


def market():
    return [
        asset(1, "USDT", 180e9, 0.9995, "tether"),
        asset(2, "USDC", 70e9, 0.9999, "usd-coin"),
        asset(3, "DAI", 5e9, 1.0, None),            # gecko_id 없음 → 수기 대조표(상위만)
        asset(4, "QUIET", 1e8, 1.0001, "quiet-usd"),  # 작고 페그 안쪽 → 대상 아님
        asset(5, "BUSD", 2e8, 1.0106, "binance-peg-busd"),   # 작지만 +106bp → 대상
        asset(6, "USDA", 7e7, 0.9036, "ap-usda"),     # 같은 심볼 두 종목
        asset(7, "USDA", 1.4e8, None, "avalon-usda"),  # 가격 없음 → 대상 아님
        asset(8, "NOGECKO", 5e7, 0.95, None),          # 이탈했지만 id 없음 → 못 받음
        asset(9, "JPYC", 2e7, 0.0063, "jpyc", cur="JPY"),  # 비USD 는 $1 기준으로 보지 않음
    ]


class TestTargets(unittest.TestCase):
    def setUp(self):
        self.t = fetch.crosscheck_targets(market(), top_n=3, watch_bp=25)

    def test_top_n_included_with_manual_fallback(self):
        self.assertEqual(self.t["1"], "tether")
        self.assertEqual(self.t["3"], "dai")  # CG_STABLE_IDS 보충

    def test_small_off_peg_coin_included(self):
        self.assertEqual(self.t["5"], "binance-peg-busd")
        self.assertEqual(self.t["6"], "ap-usda")

    def test_small_on_peg_coin_excluded(self):
        self.assertNotIn("4", self.t)

    def test_no_price_or_no_gecko_or_non_usd_excluded(self):
        self.assertNotIn("7", self.t)
        self.assertNotIn("8", self.t)
        self.assertNotIn("9", self.t)

    def test_extra_symbols_included(self):
        t = fetch.crosscheck_targets(market(), top_n=3, watch_bp=25, extra_symbols={"quiet"})
        self.assertEqual(t["4"], "quiet-usd")

    def test_cap(self):
        t = fetch.crosscheck_targets(market(), top_n=30, watch_bp=25, cap=2)
        self.assertEqual(len(t), 2)


class TestSnapshotUsesIdKey(unittest.TestCase):
    def snap(self, ext):
        return {r["id"]: r for r in fetch.build_snapshot(
            market(), [], issuers={}, yield_bearing={}, external_prices=ext)["assets"]}

    def test_second_price_tempers_single_source_breach(self):
        rows = self.snap({"5": 1.0003})
        r = rows["5"]
        self.assertEqual(r["price_sources"], 2)
        # 중위값(두 값 평균) 1.00545 → +54.5bp, 출처 차이 103bp ≥ 30bp → 가격 품질 저하
        self.assertAlmostEqual(r["dev_bp"], 54.45, places=1)
        self.assertEqual(r["price_quality"], "degraded")
        self.assertNotEqual(r["grade_peg"], "sound")  # 지우지 않고 남긴다

    def test_confirmed_depeg_stays_breach(self):
        rows = self.snap({"6": 0.9040})
        r = rows["6"]
        self.assertEqual(r["price_sources"], 2)
        self.assertEqual(r["grade_peg"], "breach")
        self.assertEqual(r["price_quality"], "ok")

    def test_id_price_not_shared_with_same_symbol(self):
        rows = self.snap({"6": 0.9040})
        self.assertEqual(rows["7"]["price_sources"], 0)

    def test_symbol_key_still_supported(self):
        rows = self.snap({"USDT": 0.9993})
        self.assertEqual(rows["1"]["price_sources"], 2)


if __name__ == "__main__":
    unittest.main()
