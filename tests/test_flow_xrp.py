#!/usr/bin/env python3
"""XRP 코너 수집의 실제 관측 구간 판정 테스트. 네트워크 없음.

2026-10-07 실측: 180일을 요청했지만 거래소 핫월렛 거래가 많아 계정당 페이지 한도
(15쪽) 안에서 약 4시간치만 훑었는데 화면은 '최근 180일'로 표시했다.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "etl"))

import fetch_flow_xrp as fx  # noqa: E402

NOW = datetime(2026, 10, 7, 18, 0, tzinfo=timezone.utc)
KR = "rKRacct"


def pay(minutes_ago: int, outflow: bool = True, amount_drops: int = 1_000_000_000):
    dt = NOW - timedelta(minutes=minutes_ago)
    return {
        "TransactionType": "Payment",
        "meta": {"TransactionResult": "tesSUCCESS",
                 "delivered_amount": {"currency": "XRP", "value": str(amount_drops)}},
        "date": dt.isoformat().replace("+00:00", "Z"),
        "Account": KR if outflow else "rGLOBAL",
        "Destination": "rGLOBAL" if outflow else KR,
        "AccountName": {"name": "Upbit" if outflow else "Binance"},
        "DestinationName": {"name": "Binance" if outflow else "Upbit"},
        "hash": f"h{minutes_ago}",
    }


def pages_feed(pages):
    """account_transactions 대역: 호출할 때마다 다음 페이지를 준다."""
    it = iter(pages)

    def fake(account, marker=None):
        return next(it)
    return fake


class TestCollectAccountCoverage(unittest.TestCase):
    def setUp(self):
        self.cutoff = NOW - timedelta(days=180)
        p = mock.patch.object(fx, "PAUSE", 0)
        p.start()
        self.addCleanup(p.stop)

    def test_capped_when_page_limit_hit(self):
        # 페이지마다 marker 가 남아 있고 한도(2쪽)에서 멈춤 → capped, 가장 오래된 시각 기록
        pages = [{"transactions": [pay(10), pay(60)], "marker": "m1"},
                 {"transactions": [pay(120), pay(240)], "marker": "m2"}]
        info = {}
        with mock.patch.object(fx, "MAX_PAGES_PER_ACCOUNT", 2), \
                mock.patch.object(fx, "account_transactions", pages_feed(pages)):
            ev = fx.collect_account(KR, "Upbit", self.cutoff, info)
        self.assertEqual(len(ev), 4)
        self.assertTrue(info["capped"])
        self.assertEqual(info["oldest_scanned"], NOW - timedelta(minutes=240))

    def test_not_capped_when_cutoff_reached(self):
        old = NOW - timedelta(days=200)
        tx_old = pay(0)
        tx_old["date"] = old.isoformat().replace("+00:00", "Z")
        pages = [{"transactions": [pay(10), tx_old], "marker": "m1"}]
        info = {}
        with mock.patch.object(fx, "account_transactions", pages_feed(pages)):
            ev = fx.collect_account(KR, "Upbit", self.cutoff, info)
        self.assertEqual(len(ev), 1)  # 180일보다 오래된 건 제외
        self.assertFalse(info["capped"])

    def test_not_capped_when_history_ends(self):
        pages = [{"transactions": [pay(10)]}]  # marker 없음 = 기록 끝
        info = {}
        with mock.patch.object(fx, "account_transactions", pages_feed(pages)):
            fx.collect_account(KR, "Upbit", self.cutoff, info)
        self.assertFalse(info["capped"])


class TestObservedWindow(unittest.TestCase):
    def test_full_window_when_none_capped(self):
        cutoff = NOW - timedelta(days=180)
        since, trunc = fx.observed_window([{"capped": False, "oldest_scanned": cutoff}], cutoff)
        self.assertEqual(since, cutoff)
        self.assertFalse(trunc)

    def test_common_window_is_latest_oldest_among_capped(self):
        cutoff = NOW - timedelta(days=180)
        infos = [
            {"capped": True, "oldest_scanned": NOW - timedelta(hours=4)},
            {"capped": True, "oldest_scanned": NOW - timedelta(hours=30)},
            {"capped": False, "oldest_scanned": NOW - timedelta(days=200)},
        ]
        since, trunc = fx.observed_window(infos, cutoff)
        self.assertTrue(trunc)
        self.assertEqual(since, NOW - timedelta(hours=4))


if __name__ == "__main__":
    unittest.main()
