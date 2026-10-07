#!/usr/bin/env python3
"""감시목록(원화·엔화 스테이블코인)과 비달러 페그 편차 테스트. 네트워크 없음."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "etl"))

import fetch  # noqa: E402
from lib.fx import local_peg_dev_bp  # noqa: E402

USDJPY = 148.0
USDKRW = 1400.0
FX = {"JPY": USDJPY, "KRW": USDKRW}


def asset(i, sym, cur, circ, price, prev_m=None, chains=None, name=None):
    key = f"pegged{cur}"
    return {
        "id": str(i), "name": name or sym, "symbol": sym, "pegType": key,
        "pegMechanism": "fiat-backed", "price": price,
        "circulating": {key: circ},
        "circulatingPrevDay": {key: circ},
        "circulatingPrevWeek": {key: circ},
        "circulatingPrevMonth": {key: prev_m if prev_m is not None else circ},
        "chainCirculating": {c: {"current": {key: v}} for c, v in (chains or {}).items()},
    }


def watch_cfg(**over):
    cfg = {
        "entries": {
            "JPYC": {"peg_currency": "JPY", "label": "엔화", "match_symbols": ["JPYC"],
                     "exclude_ids": [], "note": ""},
            "KRWQ": {"peg_currency": "KRW", "label": "원화", "match_symbols": ["KRWQ"],
                     "exclude_ids": [], "note": ""},
        },
        "fx_lag_tolerance_bp": 50,
        "pinned_currencies": ["KRW", "JPY"],
    }
    cfg.update(over)
    return cfg


def market(jpyc_price=1 / USDJPY, krwq_price=1 / USDKRW):
    return [
        asset(1, "USDT", "USD", 180e9, 1.0),
        asset(2, "USDC", "USD", 70e9, 1.0),
        asset(3, "DAI", "USD", 5e9, 0.9999),
        # 3억 엔 ≈ $2.0M, 2천만 원 ≈ $14k — 둘 다 하한($50M) 미만
        asset(10, "JPYC", "JPY", 3e8, jpyc_price,
              chains={"Ethereum": 1e8, "Polygon": 1.5e8, "Avalanche": 0.5e8}),
        asset(11, "KRWQ", "KRW", 2e7, krwq_price, chains={"Base": 2e7}),
    ]


def snap(assets, watch=None, fx=None):
    return fetch.build_snapshot(assets, [], issuers={}, yield_bearing={},
                                watchlist=watch, fx_rates=fx, fx_date="2026-10-06")


class TestLocalPegDev(unittest.TestCase):
    def test_on_peg_is_zero(self):
        self.assertAlmostEqual(local_peg_dev_bp(1 / USDJPY, USDJPY), 0.0, places=2)

    def test_small_discount(self):
        # 0.999 엔 → −10bp
        self.assertAlmostEqual(local_peg_dev_bp(0.999 / USDJPY, USDJPY), -10.0, places=1)

    def test_not_measured_against_one_dollar(self):
        # $1 기준이었다면 −9,932bp 같은 거대한 '이탈'이 나온다. 그러면 안 된다.
        dev = local_peg_dev_bp(1 / USDJPY, USDJPY)
        self.assertLess(abs(dev), 1.0)

    def test_missing_inputs(self):
        self.assertIsNone(local_peg_dev_bp(None, USDJPY))
        self.assertIsNone(local_peg_dev_bp(0.0067, None))
        self.assertIsNone(local_peg_dev_bp(0.0, USDJPY))


class TestGradeLocal(unittest.TestCase):
    thr = {"peg_watch_bp": 25, "peg_breach_bp": 100}

    def test_tolerance_widens_band(self):
        g = fetch.grade_peg_local
        self.assertEqual(g(60, self.thr, 50), "sound")     # 25+50=75 미만
        self.assertEqual(g(-80, self.thr, 50), "watch")    # 75 이상
        self.assertEqual(g(160, self.thr, 50), "breach")   # 150 이상
        self.assertEqual(g(None, self.thr, 50), "unknown")


class TestWatchlistSnapshot(unittest.TestCase):
    def test_rows_present_and_local_dev(self):
        s = snap(market(), watch_cfg(), FX)
        rows = {r["symbol"]: r for r in s["watchlist"]["rows"]}
        self.assertEqual(rows["JPYC"]["status"], "ok")
        self.assertEqual(rows["KRWQ"]["status"], "ok")
        self.assertAlmostEqual(rows["JPYC"]["dev_bp_local"], 0.0, places=1)
        self.assertEqual(rows["JPYC"]["grade_peg_local"], "sound")
        self.assertEqual(rows["JPYC"]["price_local"], 1.0)
        self.assertAlmostEqual(rows["JPYC"]["mcap_usd"], 3e8 / USDJPY, delta=1)
        self.assertEqual(rows["JPYC"]["chain_count"], 3)
        self.assertEqual(rows["JPYC"]["chains"][0]["chain"], "Polygon")
        self.assertFalse(rows["JPYC"]["in_main_list"])

    def test_below_floor_not_in_main_list_or_alerts(self):
        # 2% 할인된 JPYC: 감시목록 등급은 breach 지만 시스템 경보 건수에는 들어가지 않는다.
        s = snap(market(jpyc_price=0.98 / USDJPY), watch_cfg(), FX)
        w = {r["symbol"]: r for r in s["watchlist"]["rows"]}
        self.assertEqual(w["JPYC"]["grade"], "breach")
        self.assertNotIn("JPYC", [a["symbol"] for a in s["assets"]])
        self.assertNotIn("JPYC", [a["symbol"] for a in s["alerts"]])
        self.assertEqual(s["totals"]["breach_count"], 0)

    def test_aggregates_unchanged_by_watchlist(self):
        base = snap(market(), None, FX)
        withw = snap(market(), watch_cfg(), FX)
        self.assertEqual(base["totals"], withw["totals"])
        self.assertEqual(base["concentration"], withw["concentration"])
        self.assertEqual(base["by_peg_currency"], withw["by_peg_currency"])
        self.assertEqual(base["watchlist"]["rows"], [])

    def test_peg_currency_share_visible(self):
        s = snap(market(), watch_cfg(), FX)
        curs = {c["currency"]: c for c in s["by_peg_currency"]}
        self.assertIn("JPY", curs)
        self.assertIn("KRW", curs)
        self.assertGreater(curs["JPY"]["amount"], 0)

    def test_missing_symbol_is_reported(self):
        assets = [a for a in market() if a["symbol"] != "KRWQ"]
        s = snap(assets, watch_cfg(), FX)
        rows = {r["symbol"]: r for r in s["watchlist"]["rows"]}
        self.assertEqual(rows["KRWQ"]["status"], "missing")
        self.assertEqual(rows["KRWQ"]["grade"], "unknown")

    def test_wrong_peg_currency_not_matched(self):
        # 같은 심볼이라도 USD 페그로 등재된 항목은 엔화 감시목록에 잡지 않는다.
        assets = market() + [asset(99, "JPYC", "USD", 1e6, 1.0)]
        s = snap(assets, watch_cfg(), FX)
        jp = [r for r in s["watchlist"]["rows"] if r["watch_key"] == "JPYC"]
        self.assertEqual([r["id"] for r in jp], ["10"])

    def test_duplicate_symbol_kept_and_flagged(self):
        assets = market() + [asset(12, "JPYC", "JPY", 1e6, 1 / USDJPY, name="JPY Coin v1")]
        s = snap(assets, watch_cfg(), FX)
        jp = [r for r in s["watchlist"]["rows"] if r["watch_key"] == "JPYC"]
        self.assertEqual(len(jp), 2)
        self.assertTrue(all(r["status_note"] for r in jp))
        # exclude_ids 로 하나를 빼면 한 줄만 남는다
        cfg = watch_cfg()
        cfg["entries"]["JPYC"]["exclude_ids"] = ["12"]
        s2 = snap(assets, cfg, FX)
        jp2 = [r for r in s2["watchlist"]["rows"] if r["watch_key"] == "JPYC"]
        self.assertEqual([r["id"] for r in jp2], ["10"])

    def test_no_fx_means_unmeasured_not_false_alarm(self):
        s = snap(market(), watch_cfg(), {})
        rows = {r["symbol"]: r for r in s["watchlist"]["rows"]}
        self.assertIsNone(rows["JPYC"]["dev_bp_local"])
        self.assertEqual(rows["JPYC"]["grade_peg_local"], "unknown")


class TestUnpricedNonUsd(unittest.TestCase):
    def test_usd_value_per_unit(self):
        f = fetch.usd_value_per_unit
        self.assertEqual(f(0.0067, "JPY", FX), (0.0067, "price"))
        self.assertEqual(f(None, "USD", FX), (1.0, "peg_usd"))
        v, basis = f(None, "KRW", FX)
        self.assertAlmostEqual(v, 1 / USDKRW)
        self.assertEqual(basis, "fx")
        self.assertEqual(f(None, "KRW", {}), (0.0, "unpriced"))

    def test_unpriced_won_token_not_counted_as_dollars(self):
        # 가격 없는 원화 토큰 1천억 원. 예전 식이면 $1,000억으로 잡혀 시장 1위가 됐다.
        assets = market() + [asset(20, "KRWX", "KRW", 1e11, None)]
        s = snap(assets, watch_cfg(), FX)
        krw = next(c for c in s["by_peg_currency"] if c["currency"] == "KRW")
        self.assertLess(krw["amount"], 1e9)               # 1천억 원 ≈ $7,100만
        self.assertEqual(s["assets"][0]["symbol"], "USDT")
        s0 = snap(assets, watch_cfg(), {})
        self.assertNotIn("KRWX", [a["symbol"] for a in s0["assets"]])


class TestWatchlistFile(unittest.TestCase):
    def test_repo_file_has_jpyc_and_krwq(self):
        w = fetch.load_watchlist()
        self.assertEqual(w["entries"]["JPYC"]["peg_currency"], "JPY")
        self.assertEqual(w["entries"]["KRWQ"]["peg_currency"], "KRW")
        self.assertIn("KRW", w["pinned_currencies"])
        self.assertEqual(fetch.watchlist_currencies(w), ["JPY", "KRW"])

    def test_broken_file_is_safe(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{ not json")
        w = fetch.load_watchlist(fh.name)
        self.assertEqual(w["entries"], {})
        self.assertEqual(fetch.load_watchlist("/nonexistent/watchlist.json")["entries"], {})

    def test_output_is_json_serialisable(self):
        s = snap(market(), watch_cfg(), FX)
        json.dumps(s, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
