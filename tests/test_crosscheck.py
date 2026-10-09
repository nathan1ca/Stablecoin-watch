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
        asset(7, "USDA", 1.4e8, None, "avalon-usda"),  # 가격 없음·액면 하한 이상 → 대상(금액 계상용)
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

    def test_no_gecko_or_non_usd_excluded(self):
        self.assertNotIn("8", self.t)
        self.assertNotIn("9", self.t)

    def test_extra_symbols_included(self):
        t = fetch.crosscheck_targets(market(), top_n=3, watch_bp=25, extra_symbols={"quiet"})
        self.assertEqual(t["4"], "quiet-usd")

    def test_cap(self):
        t = fetch.crosscheck_targets(market(), top_n=30, watch_bp=25, cap=2)
        self.assertEqual(len(t), 2)

    def test_unpriced_coin_above_floor_included(self):
        # 2026-10-09: DefiLlama 가격이 없는 Avalon USDA($1.46억 액면)는 두 번째 가격이 없으면
        # $1로 계상된다 → 금액을 재기 위해 교차 가격 대상에 넣는다.
        self.assertEqual(self.t["7"], "avalon-usda")

    def test_unpriced_coin_below_floor_excluded(self):
        mk = market() + [asset(10, "TINY", 1e6, None, "tiny-usd")]
        t = fetch.crosscheck_targets(mk, top_n=3, watch_bp=25)
        self.assertNotIn("10", t)

    def test_cap_keeps_priority_before_off_peg_fill(self):
        # 상한에 닿아도 가격 없는 대형 종목(금액 계상에 쓰임)이 편차 큰 소형 종목보다 먼저 들어간다.
        mk = [asset(1, "USDT", 180e9, 0.9995, "tether"),
              asset(2, "BIGOFF", 9e8, 0.97, "big-off"),
              asset(3, "MIDOFF", 6e8, 0.96, "mid-off"),
              asset(4, "USDN", 4.09e8, None, "neutrino")]
        t = fetch.crosscheck_targets(mk, top_n=1, watch_bp=25, cap=3)
        self.assertEqual(list(t), ["1", "4", "2"])


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


class TestSourceAnomaly(unittest.TestCase):
    """2026-10-08 22시 UTC: DefiLlama 응답에서 체인 몫이 통째로 빠져 총액 −3.8%."""

    def setUp(self):
        from datetime import datetime, timezone, timedelta
        self.now = datetime(2026, 10, 8, 22, 0, tzinfo=timezone.utc)
        self.prev = {
            "meta": {"generated_at": (self.now - timedelta(hours=5)).isoformat()},
            "totals": {"circulating_usd": 315e9},
            "assets": [{"id": "1", "symbol": "USDG", "circulating": 3.3e9, "mcap_usd": 3.3e9,
                        "chains": [{"chain": "Solana", "amount": 1.5e9}, {"chain": "Ethereum", "amount": 0.5e9}]}],
            "by_chain": [{"chain": "Solana", "amount": 15e9}, {"chain": "Ethereum", "amount": 160e9}],
        }
        self.td = timedelta

    def raw(self, sol):
        cc = {"Ethereum": {"current": {"peggedUSD": 0.5e9}}}
        if sol is not None:
            cc["Solana"] = {"current": {"peggedUSD": sol}}
        return [{"id": "1", "chainCirculating": cc}]

    def snap(self, total, sol_chain=15e9):
        return {"totals": {"circulating_usd": total},
                "by_chain": [{"chain": "Solana", "amount": sol_chain}, {"chain": "Ethereum", "amount": 160e9}]}

    def test_normal_run_has_no_anomaly(self):
        self.assertEqual(fetch.source_anomalies(self.prev, self.snap(315.5e9), self.raw(1.48e9), now=self.now), [])

    def test_chain_dropout_detected(self):
        r = fetch.source_anomalies(self.prev, self.snap(314e9), self.raw(None), now=self.now)
        self.assertTrue(any("USDG: Solana" in x for x in r), r)

    def test_total_drop_and_market_chain_detected(self):
        r = fetch.source_anomalies(self.prev, self.snap(303e9, sol_chain=1e9), self.raw(1.5e9), now=self.now)
        self.assertTrue(any("총 발행잔액" in x for x in r))
        self.assertTrue(any("체인 합계 Solana" in x for x in r))

    def test_empty_chain_fetch_is_not_anomaly(self):
        s = self.snap(315e9); s["by_chain"] = []
        self.assertEqual(fetch.source_anomalies(self.prev, s, self.raw(1.5e9), now=self.now), [])

    def test_old_prev_not_compared(self):
        self.prev["meta"]["generated_at"] = (self.now - self.td(hours=60)).isoformat()
        self.assertEqual(fetch.source_anomalies(self.prev, self.snap(200e9), self.raw(None), now=self.now), [])

    def test_hold_only_for_limited_time(self):
        self.assertTrue(fetch.hold_decision(self.prev, ["x"], now=self.now)["held"])
        self.prev["meta"]["generated_at"] = (self.now - self.td(hours=37)).isoformat()
        d = fetch.hold_decision(self.prev, ["x"], now=self.now)
        self.assertFalse(d["held"])
        self.assertIn("새 값을 게시", d["note"])

    def test_real_crash_with_flat_chains_is_not_held(self):
        # 가격이 무너져도 수량·체인 몫이 그대로면 원천 이상이 아니다(시장 사건은 그대로 게시).
        self.assertEqual(fetch.source_anomalies(self.prev, self.snap(312e9), self.raw(1.4e9), now=self.now), [])


class TestSourceAnomalyPegValued(unittest.TestCase):
    def test_peg_valued_coin_does_not_hold_snapshot(self):
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 10, 8, 22, 31, tzinfo=timezone.utc)
        prev = {"meta": {"generated_at": (now - timedelta(hours=5)).isoformat()},
                "totals": {"circulating_usd": 315e9},
                "assets": [{"id": "214", "symbol": "USDX", "circulating": 6.8e8, "mcap_usd": 6.8e8,
                            "mcap_basis": "peg_usd", "chains": [{"chain": "BSC", "amount": 5.28e8}]}],
                "by_chain": []}
        snap = {"totals": {"circulating_usd": 315e9}, "by_chain": []}
        raw = [{"id": "214", "chainCirculating": {"BSC": {"current": {"peggedUSD": 4e6}}}}]
        self.assertEqual(fetch.source_anomalies(prev, snap, raw, now=now), [])


class TestYieldBearingNotEscalated(unittest.TestCase):
    def test_nav_rounding_gap_does_not_make_watch(self):
        a = asset(339, "reUSD", 3e8, 1.106, "re-protocol-reusd")
        rows = fetch.build_snapshot([a] + market()[:2], [], issuers={},
                                    yield_bearing={"REUSD": {"kind": "x", "defillama_id": "339"}},
                                    external_prices={"339": 1.11})["assets"]
        r = [x for x in rows if x["id"] == "339"][0]
        self.assertEqual(r["price_quality"], "degraded")
        self.assertEqual(r["grade"], "sound")


class TestValuationUsesSecondPrice(unittest.TestCase):
    """DefiLlama 가격이 빠진 종목의 금액 계상(2026-10-09 USDN·USDX 사례)."""

    def mk(self):
        return [asset(1, "USDT", 180e9, 0.9995, "tether"),
                asset(12, "USDN", 4.0889e8, None, "neutrino"),
                asset(13, "HUSD", 1.92e8, None, None)]

    def build(self, ext):
        return fetch.build_snapshot(self.mk(), [], issuers={}, yield_bearing={}, external_prices=ext)

    def test_crosscheck_price_values_unpriced_coin(self):
        s = self.build({"12": 0.015759})
        total = s["totals"]["circulating_usd"]
        # USDN 은 4.09억 개 × $0.0158 ≈ $644만 → 표시 하한($5,000만) 아래라 표에서 빠지고,
        # 총계에도 $4.09억이 아니라 약 $644만만 들어간다.
        self.assertNotIn("USDN", [r["symbol"] for r in s["assets"]])
        self.assertAlmostEqual(total, 180e9 * 0.9995 + 4.0889e8 * 0.015759 + 1.92e8, delta=1)
        xc = s["face_valued"]["crosscheck_valued"]
        self.assertEqual(xc[0]["symbol"], "USDN")
        self.assertAlmostEqual(xc[0]["mcap_usd"], 4.0889e8 * 0.015759, delta=1)

    def test_without_any_price_face_value_is_disclosed(self):
        s = self.build({})
        fv = s["face_valued"]
        self.assertEqual(fv["count"], 2)
        self.assertEqual([x["symbol"] for x in fv["top"]], ["USDN", "HUSD"])
        self.assertAlmostEqual(fv["usd"], 4.0889e8 + 1.92e8, delta=1)
        usdn = next(r for r in s["assets"] if r["symbol"] == "USDN")
        self.assertEqual(usdn["mcap_basis"], "peg_usd")

    def test_defillama_price_still_preferred(self):
        mk = [asset(1, "USDT", 180e9, 0.9995, "tether")]
        s = fetch.build_snapshot(mk, [], issuers={}, yield_bearing={}, external_prices={"1": 0.90})
        r = s["assets"][0]
        self.assertEqual(r["mcap_basis"], "price")
        self.assertAlmostEqual(r["mcap_usd"], 180e9 * 0.9995, delta=1)
        self.assertEqual(s["face_valued"]["count"], 0)

    def test_value_per_unit_order(self):
        f = fetch.usd_value_per_unit
        self.assertEqual(f(0.99, "USD", {}, alt_price=0.5), (0.99, "price"))
        self.assertEqual(f(None, "USD", {}, alt_price=0.0158), (0.0158, "price_crosscheck"))
        self.assertEqual(f(None, "USD", {}, alt_price=None), (1.0, "peg_usd"))
        self.assertEqual(f(None, "JPY", {"JPY": 158.0}, alt_price=0.0063), (0.0063, "price_crosscheck"))
        self.assertEqual(f(None, "JPY", {"JPY": 158.0})[1], "fx")
