#!/usr/bin/env python3
"""국내 거래소 거래지원 현황 가공 테스트. 네트워크 없음."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "etl"))

import fetch_listings as fl  # noqa: E402

CFG = {
    "exchanges": [{"id": "upbit", "name": "업비트"}, {"id": "bithumb", "name": "빗썸"}, {"id": "korbit", "name": "코빗"}],
    "deny": {"U"},
    "aliases": {"USDTX": "USDT"},
}
TRACKED = {"USDT", "USDC", "U", "PYUSD"}


class TestParsers(unittest.TestCase):
    def test_upbit_style(self):
        rows = [
            {"market": "KRW-USDT", "market_event": {"warning": False}},
            {"market": "BTC-USDC", "market_warning": "CAUTION"},
            {"market": "broken"},
        ]
        self.assertEqual(fl._pairs_from_upbit_style(rows), [
            ("USDT", "KRW", {"warning": False, "name": ""}),
            ("USDC", "BTC", {"warning": True, "name": ""}),
        ])

    def test_bithumb_fallback(self):
        def fake(url, **_):
            if "v1/market/all" in url:
                raise RuntimeError("down")
            if url.endswith("ALL_KRW"):
                return {"status": "0000", "data": {"USDT": {"closing_price": "1400"}, "date": "1"}}
            raise RuntimeError("no market")
        with mock.patch.object(fl, "get_json", side_effect=fake):
            self.assertEqual(fl.fetch_bithumb(), [("USDT", "KRW", {})])

    def test_coinone_skips_halted(self):
        r = {"markets": [{"target_currency": "usdt", "quote_currency": "krw", "trade_status": 1},
                         {"target_currency": "usdc", "quote_currency": "KRW", "trade_status": 0}]}
        with mock.patch.object(fl, "get_json", return_value=r):
            self.assertEqual(fl.fetch_coinone(), [("USDT", "KRW", {})])

    def test_korbit_v2_then_v1(self):
        with mock.patch.object(fl, "get_json", return_value={"data": [{"symbol": "usdc_krw", "status": "launched"},
                                                                     {"symbol": "usdt_krw", "status": "delisted"}]}):
            self.assertEqual(fl.fetch_korbit(), [("USDC", "KRW", {})])

        def fake(url, **_):
            if "v2" in url:
                raise RuntimeError("down")
            return {"usdt_krw": {}, "timestamp": 1}
        with mock.patch.object(fl, "get_json", side_effect=fake):
            self.assertEqual(fl.fetch_korbit(), [("USDT", "KRW", {})])

    def test_gopax(self):
        r = [{"name": "USDT-KRW", "baseAsset": "USDT", "quoteAsset": "KRW"}, {"name": "USDC-KRW"}]
        with mock.patch.object(fl, "get_json", return_value=r):
            self.assertEqual(fl.fetch_gopax(), [("USDT", "KRW", {}), ("USDC", "KRW", {})])


class TestBuild(unittest.TestCase):
    def raw(self):
        return {
            "upbit": [("USDT", "KRW", {"warning": True}), ("USDT", "BTC", {}), ("USDC", "KRW", {}),
                      ("BTC", "KRW", {}), ("U", "KRW", {})],
            "bithumb": [("USDTX", "KRW", {}), ("USDC", "USDT", {})],
            "korbit": RuntimeError("timeout"),
        }

    def test_baseline(self):
        r = fl.build_listings(self.raw(), CFG, TRACKED, None, today="2026-10-07")
        self.assertTrue(r["meta"]["baseline"])
        self.assertEqual(r["events"], [])                       # 첫 수집은 이벤트를 만들지 않는다
        usdt = r["assets"]["USDT"]
        self.assertEqual(usdt["count"], 2)                      # 별칭으로 빗썸 USDTX → USDT
        self.assertEqual(usdt["krw_count"], 2)
        self.assertEqual(usdt["exchanges"]["upbit"]["markets"], ["KRW", "BTC"])
        self.assertTrue(usdt["exchanges"]["upbit"]["warning"])
        self.assertNotIn("U", r["assets"])                      # 거부 목록
        self.assertNotIn("BTC", r["assets"])                    # 대상 밖
        self.assertEqual(r["assets"]["USDC"]["krw_count"], 1)   # 빗썸은 USDT 마켓만
        st = {e["id"]: e["status"] for e in r["meta"]["exchanges"]}
        self.assertEqual(st, {"upbit": "ok", "bithumb": "ok", "korbit": "fail"})
        self.assertEqual(r["first_seen"]["upbit:USDT:KRW"], "2026-10-07")

    def test_events(self):
        prev = {
            "first_seen": {"upbit:USDT:KRW": "2026-09-01", "upbit:PYUSD:KRW": "2026-09-01",
                           "korbit:USDT:KRW": "2026-09-01"},
            "events": [{"date": "2026-09-20", "exchange": "upbit", "symbol": "X", "market": "KRW", "kind": "listed"}],
        }
        r = fl.build_listings(self.raw(), CFG, TRACKED, prev, today="2026-10-08")
        self.assertFalse(r["meta"]["baseline"])
        ev = {(e["exchange"], e["symbol"], e["market"], e["kind"]) for e in r["events"] if e["date"] == "2026-10-08"}
        self.assertIn(("upbit", "USDC", "KRW", "listed"), ev)
        self.assertIn(("upbit", "PYUSD", "KRW", "delisted"), ev)
        # 코빗은 수집 실패 — 종료로 보지 않고 직전 상태를 잇는다
        self.assertNotIn(("korbit", "USDT", "KRW", "delisted"), ev)
        self.assertEqual(r["first_seen"]["korbit:USDT:KRW"], "2026-09-01")
        self.assertNotIn("korbit", r["assets"]["USDT"]["exchanges"])   # 직전 assets 없으면 비움

        prev["assets"] = {"USDT": {"exchanges": {"korbit": {"markets": ["KRW"], "warning": False}}}}
        r = fl.build_listings(self.raw(), CFG, TRACKED, prev, today="2026-10-08")
        self.assertTrue(r["assets"]["USDT"]["exchanges"]["korbit"]["stale"])
        self.assertEqual(r["assets"]["USDT"]["count"], 3)
        self.assertEqual(r["first_seen"]["upbit:USDT:KRW"], "2026-09-01")  # 처음 본 날짜 유지
        self.assertEqual(r["events"][-1]["date"], "2026-09-20")             # 과거 이벤트 보존, 최신순

    def test_tracked_symbols(self):
        snap = {"assets": [{"symbol": "usdt"}, {"symbol": ""}], "watchlist": {"rows": [{"symbol": "JPYC"}]}}
        self.assertEqual(fl.tracked_symbols(snap), {"USDT", "JPYC"})
        self.assertEqual(fl.tracked_symbols(None), set())


if __name__ == "__main__":
    unittest.main()
