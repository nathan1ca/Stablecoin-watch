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


class TestIcons(unittest.TestCase):
    def test_find_icon_links(self):
        html = """<head><link rel="icon" href="/f16.png" sizes="16x16">
        <link href='https://cdn.x.com/touch.png' rel='apple-touch-icon' sizes='180x180'>
        <link rel="mask-icon" href="/m.svg"><link rel="icon" href="/logo.svg">
        <link rel="stylesheet" href="/a.css"><link rel="shortcut icon" href="favicon.ico"></head>"""
        self.assertEqual(fl.find_icon_links(html, "https://ex.co.kr/"), [
            "https://cdn.x.com/touch.png", "https://ex.co.kr/f16.png", "https://ex.co.kr/favicon.ico"])

    def test_sniff(self):
        self.assertEqual(fl.sniff_image(b"\x89PNG\r\n\x1a\n" + b"0" * 10), ".png")
        self.assertEqual(fl.sniff_image(b"\x00\x00\x01\x00" + b"0" * 10), ".ico")
        self.assertIsNone(fl.sniff_image(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"))   # SVG 거부
        self.assertIsNone(fl.sniff_image(b"<!doctype html><html>"))

    def test_ensure_icons_keeps_existing_and_falls_back(self):
        import tempfile
        cfg = {"exchanges": [{"id": "upbit", "url": "https://upbit.com"}, {"id": "gopax", "url": "https://gopax.co.kr"}]}
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / "ex-icons").mkdir()
            (out / "ex-icons" / "upbit.png").write_bytes(b"x")
            with mock.patch.object(fl, "download_icon", return_value=None) as dl:
                paths = fl.ensure_icons(cfg, out)
            self.assertEqual(paths, {"upbit": "data/ex-icons/upbit.png"})   # 있는 것은 다시 받지 않음
            self.assertEqual(dl.call_count, 1)                                # 고팍스만 시도, 실패 → 배지
            png = b"\x89PNG\r\n\x1a\n" + b"0" * 200
            with mock.patch.object(fl, "download_icon", return_value=(png, ".png", "u")):
                paths = fl.ensure_icons(cfg, out, refresh=True)
            self.assertEqual(paths["gopax"], "data/ex-icons/gopax.png")
            self.assertEqual((out / "ex-icons" / "gopax.png").read_bytes(), png)


class TestConfig(unittest.TestCase):
    def test_repo_config(self):
        cfg = fl.load_config()
        self.assertEqual([e["id"] for e in cfg["exchanges"]], list(fl.FETCHERS))
        self.assertTrue({"U", "M", "FRAX"} <= cfg["deny"])   # 동명 티커 제외

    def test_broken_config_falls_back(self):
        cfg = fl.load_config("/nonexistent/kr.json")
        self.assertEqual(len(cfg["exchanges"]), 5)


if __name__ == "__main__":
    unittest.main()
