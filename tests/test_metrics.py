#!/usr/bin/env python3
"""위험 지표 단위 테스트. 표준 라이브러리 unittest 만 사용."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "etl"))

from lib.config import load_thresholds  # noqa: E402
from lib.metrics import (  # noqa: E402
    composite_risk_score,
    grade_peg,
    grade_premium,
    grade_redemption,
    hhi,
    median,
    peg_amount,
    pct_change,
    worse_grade,
)


class TestPegAmount(unittest.TestCase):
    def test_dict(self):
        self.assertEqual(peg_amount({"peggedUSD": 100.5}), 100.5)

    def test_number(self):
        self.assertEqual(peg_amount(42), 42.0)

    def test_none(self):
        self.assertEqual(peg_amount(None), 0.0)


class TestPctChange(unittest.TestCase):
    def test_basic(self):
        self.assertAlmostEqual(pct_change(110, 100), 10.0)

    def test_zero_prev(self):
        self.assertIsNone(pct_change(10, 0))


class TestHHI(unittest.TestCase):
    def test_monopoly(self):
        self.assertEqual(hhi([100.0]), 10000.0)

    def test_equal(self):
        # 4개 25% → 4 * 625 = 2500
        self.assertEqual(hhi([25, 25, 25, 25]), 2500.0)


class TestGrades(unittest.TestCase):
    def setUp(self):
        self.thr = load_thresholds()

    def test_peg_sound(self):
        self.assertEqual(grade_peg(10, self.thr), "sound")

    def test_peg_watch(self):
        self.assertEqual(grade_peg(30, self.thr), "watch")

    def test_peg_breach(self):
        self.assertEqual(grade_peg(120, self.thr), "breach")

    def test_redemption(self):
        self.assertEqual(grade_redemption(-5, self.thr), "sound")
        self.assertEqual(grade_redemption(-15, self.thr), "watch")
        self.assertEqual(grade_redemption(-30, self.thr), "breach")

    def test_stable_premium_tighter(self):
        # 0.8% is watch for stable, sound for crypto
        self.assertEqual(grade_premium(0.8, self.thr, stable=True), "watch")
        self.assertEqual(grade_premium(0.8, self.thr, stable=False), "sound")

    def test_worse_grade(self):
        self.assertEqual(worse_grade("sound", "watch"), "watch")
        self.assertEqual(worse_grade("breach", "watch"), "breach")


class TestRiskScore(unittest.TestCase):
    def setUp(self):
        self.thr = load_thresholds()

    def test_calm(self):
        r = composite_risk_score(
            max_abs_dev_bp=5,
            worst_redemption_pct=2.0,
            hhi_issuer=1200,
            algo_share=1.0,
            price_degraded_share=0.0,
            thr=self.thr,
        )
        self.assertEqual(r["grade"], "sound")
        self.assertLess(r["score"], self.thr["risk_watch"])

    def test_stressed(self):
        r = composite_risk_score(
            max_abs_dev_bp=150,
            worst_redemption_pct=-30,
            hhi_issuer=5000,
            algo_share=10.0,
            price_degraded_share=0.5,
            thr=self.thr,
        )
        self.assertIn(r["grade"], ("watch", "breach"))
        self.assertGreaterEqual(r["score"], self.thr["risk_watch"])

    def test_components_present(self):
        r = composite_risk_score(
            max_abs_dev_bp=0, worst_redemption_pct=0,
            hhi_issuer=0, algo_share=0, price_degraded_share=0, thr=self.thr,
        )
        for k in ("peg", "redemption", "concentration", "algorithmic", "price_quality"):
            self.assertIn(k, r["components"])


class TestMedian(unittest.TestCase):
    def test_odd(self):
        self.assertEqual(median([3.0, 1.0, 2.0]), 2.0)

    def test_even(self):
        self.assertEqual(median([1.0, 2.0, 3.0, 4.0]), 2.5)

    def test_empty(self):
        self.assertIsNone(median([]))


if __name__ == "__main__":
    unittest.main()
