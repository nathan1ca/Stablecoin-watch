#!/usr/bin/env python3
"""발행잔액 차트 종목 목록(국내 원화마켓 상장 종목 포함) 테스트. 네트워크 없음."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "etl"))

import fetch  # noqa: E402


class TestKrwListed(unittest.TestCase):
    def test_only_krw_markets(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "listings.json"
            p.write_text(json.dumps({"assets": {
                "usdt": {"exchanges": {"upbit": {"markets": ["KRW", "BTC"]}}},
                "TUSD": {"exchanges": {"upbit": {"markets": ["BTC"]}}},
                "FDUSD": {"exchanges": {"digitalx": {"markets": ["KRW"]}}},
            }}), encoding="utf-8")
            self.assertEqual(fetch.krw_listed_symbols(p), {"USDT", "FDUSD"})

    def test_missing_file(self):
        self.assertEqual(fetch.krw_listed_symbols(Path("/nonexistent/listings.json")), set())

    def test_count(self):
        self.assertGreaterEqual(fetch.SERIES_ASSET_COUNT, 30)


if __name__ == "__main__":
    unittest.main()
