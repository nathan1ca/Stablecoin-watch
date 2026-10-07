#!/usr/bin/env python3
"""시장 영향 종목(비중 기준) 판정과 이자부 상품 ID 고정 테스트. 네트워크 없음.

2026-10-07 스냅숏에서 reUSD(가격 누적형, +1,047bp)와 AP USDA(비중 0.02%, −999bp)
두 종목이 시스템 등급을 '경보'로, 합성점수 페그 요소를 100으로 고정했다.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "etl"))

import fetch  # noqa: E402
from lib.config import load_thresholds  # noqa: E402


def asset(i, sym, circ, price, prev_m=None, name=None):
    key = "peggedUSD"
    return {
        "id": str(i), "name": name or sym, "symbol": sym, "pegType": key,
        "pegMechanism": "fiat-backed", "price": price,
        "circulating": {key: circ},
        "circulatingPrevDay": {key: circ},
        "circulatingPrevWeek": {key: circ},
        "circulatingPrevMonth": {key: prev_m if prev_m is not None else circ},
        "chainCirculating": {},
    }


def base_market():
    # 합계 약 $2,600억. 1% ≈ $26억.
    return [
        asset(1, "USDT", 180e9, 0.9998),
        asset(2, "USDC", 70e9, 0.9999),
        asset(3, "DAI", 5e9, 0.9999),
    ]


YB = {
    "USYC": {"kind": "토큰화 단기국채 펀드"},
    "REUSD": {"kind": "재보험 수익 누적형 토큰", "defillama_id": "339"},
}


def snap(assets, yb=None):
    return fetch.build_snapshot(assets, [], issuers={}, yield_bearing=YB if yb is None else yb)


class TestYieldBearingIdPin(unittest.TestCase):
    def test_pinned_id_is_excluded(self):
        r = fetch.yield_bearing_reason({"id": "339", "symbol": "reUSD"}, YB)
        self.assertEqual(r, "id")

    def test_same_symbol_other_id_is_not_excluded(self):
        # 같은 심볼의 다른 종목은 실제 이탈을 숨기면 안 된다.
        self.assertIsNone(fetch.yield_bearing_reason({"id": "999", "symbol": "REUSD"}, YB))

    def test_unpinned_entry_matches_by_symbol(self):
        self.assertEqual(fetch.yield_bearing_reason({"id": "7", "symbol": "usyc"}, YB), "symbol")

    def test_api_field_wins(self):
        self.assertEqual(
            fetch.yield_bearing_reason({"id": "1", "symbol": "X", "yieldBearing": True}, {}),
            "field:yieldBearing")

    def test_reusd_accrual_is_not_a_breach(self):
        s = snap(base_market() + [asset(339, "reUSD", 300e6, 1.1047)])
        row = next(a for a in s["assets"] if a["symbol"] == "reUSD")
        self.assertTrue(row["yield_bearing"])
        self.assertIsNone(row["dev_bp"])
        self.assertEqual(s["totals"]["breach_count"], 0)
        self.assertIn("reUSD", [y["symbol"] for y in s["yield_bearing"]])

    def test_repo_file_pins_reusd(self):
        table = fetch.load_yield_bearing()
        self.assertEqual(table["REUSD"].get("defillama_id"), "339")


class TestSystemicRelevance(unittest.TestCase):
    def test_small_breach_is_flagged_but_not_systemic(self):
        # $70M 종목이 −1,000bp: 종목 경보는 남고, 시스템은 '주의'까지만.
        s = snap(base_market() + [asset(50, "USDA", 70e6, 0.90)])
        t = s["totals"]
        self.assertEqual(t["breach_count"], 1)
        self.assertEqual(t["systemic_breach_count"], 0)
        self.assertEqual(t["system_grade"], "watch")
        self.assertIn("USDA", [a["symbol"] for a in s["alerts"]])
        # 페그 요소는 시장 영향 종목(USDT·USDC·DAI) 중 최대 편차(2bp)로 계산
        self.assertLess(s["risk"]["components"]["peg"], 5)
        self.assertEqual(s["risk"]["drivers"]["peg"]["symbol"], "USDT")

    def test_large_depeg_is_systemic(self):
        # 2023-03 USDC 디페그(최저 약 $0.87)와 같은 상황 → 시스템 경보
        mk = base_market()
        mk[1] = asset(2, "USDC", 70e9, 0.88)
        s = snap(mk)
        self.assertEqual(s["totals"]["systemic_breach_count"], 1)
        self.assertEqual(s["totals"]["system_grade"], "breach")
        self.assertEqual(s["risk"]["components"]["peg"], 100.0)
        self.assertEqual(s["risk"]["drivers"]["peg"]["symbol"], "USDC")

    def test_collapsing_large_coin_stays_systemic(self):
        # UST(2022-05)처럼 큰 종목이 $0.10 으로 붕괴하고 수량도 줄면 시장가 비중은
        # 1% 아래로 떨어진다. 액면·30일 전 수량 기준이라 여전히 시스템 판단 대상이어야 한다.
        s = snap(base_market() + [asset(70, "UST", 10e9, 0.10, prev_m=18e9)])
        row = next(a for a in s["assets"] if a["symbol"] == "UST")
        self.assertLess(row["share"], 1.0)  # 시장가 비중은 기준 미만
        self.assertEqual(s["totals"]["systemic_breach_count"], 1)
        self.assertEqual(s["totals"]["system_grade"], "breach")
        self.assertEqual(s["risk"]["drivers"]["peg"]["symbol"], "UST")

    def test_small_redemption_does_not_drive_component(self):
        # 비중 0.2% 종목의 30일 −40% 순소각은 종목 경보이지만 상환 요소를 정하지 않는다.
        s = snap(base_market() + [asset(60, "SMALL", 0.6e9, 1.0, prev_m=1.0e9)])
        self.assertEqual(s["totals"]["breach_count"], 1)
        self.assertEqual(s["risk"]["components"]["redemption"], 0.0)

    def test_large_redemption_drives_component(self):
        mk = base_market()
        mk[1] = asset(2, "USDC", 50e9, 1.0, prev_m=70e9)  # −28.6%
        s = snap(mk)
        self.assertEqual(s["risk"]["drivers"]["redemption"]["symbol"], "USDC")
        self.assertEqual(s["risk"]["components"]["redemption"], 100.0)
        self.assertEqual(s["totals"]["system_grade"], "breach")

    def test_coverage_reported(self):
        s = snap(base_market())
        self.assertEqual(s["risk"]["systemic_count"], 3)
        self.assertGreater(s["risk"]["systemic_coverage_pct"], 99)
        self.assertEqual(s["meta"]["thresholds"]["systemic_share_pct"], 1.0)


class TestConfig(unittest.TestCase):
    def test_default_when_missing(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.json"
            p.write_text(json.dumps({"peg": {"watch_bp": 25}}), encoding="utf-8")
            self.assertEqual(load_thresholds(p)["systemic_share_pct"], 1.0)

    def test_override(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.json"
            p.write_text(json.dumps({"systemic": {"share_pct": 2.5}}), encoding="utf-8")
            self.assertEqual(load_thresholds(p)["systemic_share_pct"], 2.5)

    def test_repo_file(self):
        self.assertEqual(load_thresholds()["systemic_share_pct"], 1.0)


if __name__ == "__main__":
    unittest.main()
