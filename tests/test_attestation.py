#!/usr/bin/env python3
"""어테스테이션 시차 계산 테스트. 네트워크 없음."""

from __future__ import annotations

import json
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "etl"))

import fetch_attestation as fa  # noqa: E402


def ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


class TestCadenceGrade(unittest.TestCase):
    def test_monthly_matches_old_fixed_limits(self):
        self.assertEqual(fa.day_limits(30), (45, 75))
        self.assertEqual(fa.day_limits(None), (45, 75))     # 주기 미기재 = 월간

    def test_quarterly_not_flagged_on_normal_schedule(self):
        self.assertEqual(fa.grade(99, None, 91), "sound")   # 2026-10-07 USDT 사례
        self.assertEqual(fa.grade(99, None, 30), "breach")  # 월간 잣대였다면 경보
        self.assertEqual(fa.grade(110, None, 91), "watch")
        self.assertEqual(fa.grade(140, None, 91), "breach")

    def test_drift_still_counts(self):
        self.assertEqual(fa.grade(10, 9.0, 30), "breach")
        self.assertEqual(fa.grade(10, -4.0, 30), "watch")


class TestBuild(unittest.TestCase):
    def test_case_insensitive_symbol_and_fields(self):
        entries = [{"issuer": "X", "symbol": "USDtb", "as_of_date": ago(37), "cadence_days": 30,
                    "reported_circulating": 100.0, "attestor": "Firm", "verified": True},
                   {"issuer": "Y", "symbol": "AUSD", "as_of_date": ago(37), "reported_circulating": None}]
        r = fa.build(entries, {"USDTB": 110.0, "AUSD": 5.0}, [{"symbol": "USDe", "kind": "other"}])
        a = {e["symbol"]: e for e in r["entries"]}
        self.assertEqual(a["USDtb"]["current_circulating"], 110.0)
        self.assertEqual(a["USDtb"]["drift_pct"], 10.0)
        self.assertEqual(a["USDtb"]["grade"], "breach")       # 드리프트 10%
        self.assertEqual(a["USDtb"]["attestor"], "Firm")
        self.assertIsNone(a["AUSD"]["drift_pct"])             # 보고 수치 없으면 드리프트 없음
        self.assertEqual(a["AUSD"]["grade"], "sound")
        self.assertEqual(r["not_covered"][0]["symbol"], "USDe")

    def test_repo_file_is_valid(self):
        d = json.loads((ROOT / "etl" / "attestations.json").read_text(encoding="utf-8"))
        syms = {e["symbol"] for e in d["entries"]}
        self.assertTrue({"USDC", "USDT", "USD1", "FDUSD", "RLUSD"} <= syms)
        for e in d["entries"]:
            date.fromisoformat(e["as_of_date"])
            self.assertIn(e.get("cadence_days"), (30, 91))
            self.assertTrue(e["source_url"].startswith("https://"))
            if e.get("verified"):
                self.assertIsNotNone(e.get("reported_circulating"))   # 검증 표시는 수치가 있을 때만
        fa.build(d["entries"], {}, d.get("not_covered"))

    def test_reserve_breakdown_sums_to_total(self):
        # 준비금 구성은 보고서 항목을 옮긴 것이라 합계가 보고서 총액과 맞아야 한다(반올림 $10 허용).
        d = json.loads((ROOT / "etl" / "attestations.json").read_text(encoding="utf-8"))
        kinds = {"cash", "tbill", "repo", "fund", "gold", "btc", "equity", "loan", "other", "settle"}
        n = 0
        for e in d["entries"]:
            rows = e.get("reserves_breakdown")
            if not rows:
                continue
            n += 1
            self.assertTrue(e.get("reserves_breakdown_basis"), e["symbol"])
            for r in rows:
                self.assertIn(r["k"], kinds, e["symbol"])
                self.assertTrue(r["label"])
            self.assertAlmostEqual(sum(r["amount"] for r in rows), e["reserves_total"], delta=10,
                                   msg=e["symbol"])
        self.assertGreaterEqual(n, 7)
        out = fa.build(d["entries"], {}, [])
        usdt = next(x for x in out["entries"] if x["symbol"] == "USDT")
        self.assertEqual(len(usdt["reserves_breakdown"]), 11)


if __name__ == "__main__":
    unittest.main()
