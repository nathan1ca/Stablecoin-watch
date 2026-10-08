#!/usr/bin/env python3
"""최근 30일 온체인 이체 건수 가공 테스트. 네트워크 없음."""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "etl"))

import fetch_onchain_tx as ot  # noqa: E402


def series(n, tx, active=None, start=date(2026, 8, 1)):
    return [{"asset": "x", "time": f"{start + timedelta(days=i)}T00:00:00.000000000Z",
             "TxTfrCnt": str(tx), **({"AdrActCnt": str(active)} if active is not None else {})}
            for i in range(n)]


class TestOnchainTx(unittest.TestCase):
    def test_summary_and_change(self):
        rows = series(30, 100) + series(30, 150, 40, start=date(2026, 8, 31))
        r = ot.summarize("usdt_eth", "USDT", "Ethereum", ot.parse_rows(rows))
        self.assertEqual(r["tx_30d"], 4500)
        self.assertEqual(r["tx_prev_30d"], 3000)
        self.assertEqual(r["chg_pct"], 50.0)
        self.assertEqual(r["tx_avg"], 150)
        self.assertEqual(r["active_avg"], 40)
        self.assertEqual(len(r["daily"]), 30)

    def test_short_history_no_change(self):
        r = ot.summarize("pyusd_eth", "PYUSD", "Ethereum", ot.parse_rows(series(40, 10)))
        self.assertIsNone(r["chg_pct"])      # 직전 30일이 다 차지 않으면 비교하지 않음
        self.assertEqual(r["tx_30d"], 300)

    def test_bad_values_skipped(self):
        rows = series(2, 5) + [{"time": "2026-08-03T00:00:00Z", "TxTfrCnt": None}]
        self.assertEqual(len(ot.parse_rows(rows)), 2)

    def test_build_status_and_totals(self):
        res = {"usdt_eth": series(30, 100), "usdt_trx": series(30, 300), "usdc_eth": RuntimeError("403"),
               "dai": []}
        b = ot.build(res)
        self.assertEqual([r["code"] for r in b["rows"]], ["usdt_trx", "usdt_eth"])
        self.assertEqual(b["totals"]["by_symbol"]["USDT"], 12000)
        self.assertTrue(b["meta"]["status"]["usdc_eth"].startswith("실패"))
        self.assertEqual(b["meta"]["status"]["dai"], "데이터 없음")
        self.assertIn("Solana", b["meta"]["chains_missing"])


import json as _json
import tempfile as _tempfile
from datetime import datetime as _dt, timezone as _tz

import onchain_eth_counts as ec  # noqa: E402


class FakeEtherscan:
    """블록 번호 = 초 단위 시각 / 12 인 가짜 체인. logs: {블록: 이체 수}."""

    def __init__(self, logs, tip):
        self.logs, self.tip, self.calls = logs, tip, 0

    def __call__(self, p, key):
        self.calls += 1
        a = p["action"]
        if a == "eth_blockNumber":
            return {"result": hex(self.tip)}
        if a == "getblocknobytime":
            return {"result": str(int(p["timestamp"]) // 12)}
        if a == "getLogs":
            frm, to, page = int(p["fromBlock"]), int(p["toBlock"]), int(p["page"])
            out = []
            for b in range(frm, to + 1):
                out += [{"timeStamp": hex(b * 12)}] * self.logs.get(b, 0)
            sl = out[(page - 1) * 1000: page * 1000]
            return {"status": "1" if sl else "0", "message": "OK" if sl else "No records found", "result": sl}
        raise AssertionError(a)


class TestEthCounts(unittest.TestCase):
    NOW = _dt(2026, 10, 8, 12, 0, tzinfo=_tz.utc)

    def test_counts_by_day_and_split_when_capped(self):
        start = int(_dt(2026, 9, 7, tzinfo=_tz.utc).timestamp()) // 12
        tip = int(self.NOW.timestamp()) // 12
        logs = {start + 10: 3, start + 7200: 12_000, tip - 5: 2}   # 한 블록 1.2만 건은 나눌 수 없음 → 실패
        fake = FakeEtherscan(logs, tip)
        tok = {"symbol": "X", "address": "0x1"}
        with self.assertRaises(RuntimeError):
            ec.collect_token(tok, {}, "k", tip, 1e18, call=fake, clock=lambda: 0, now=self.NOW)
        # 1.2만 건이 여러 블록에 흩어지면 구간을 나눠 모두 센다
        logs = {start + 10: 3, **{start + 7200 + i: 1000 for i in range(12)}, tip - 5: 2}
        fake = FakeEtherscan(logs, tip)
        st = {}
        ec.collect_token(tok, st, "k", tip, 1e18, call=fake, clock=lambda: 0, now=self.NOW)
        self.assertTrue(st["caught_up"])
        self.assertEqual(sum(st["daily"].values()), 3 + 12_000 + 2)
        self.assertEqual(st["start_date"], "2026-09-07")
        r = ec.summarize("X", st, now=self.NOW)
        self.assertEqual(r["days"], 30)
        self.assertTrue(r["complete"])
        self.assertEqual(r["tx_30d"], 12_000)   # 09-07(창 밖 31일째) 3건·오늘(미완결) 2건 제외
        self.assertEqual(r["to"], "2026-10-07")

    def test_time_budget_resumes(self):
        start = int(_dt(2026, 9, 7, tzinfo=_tz.utc).timestamp()) // 12
        tip = int(self.NOW.timestamp()) // 12
        fake = FakeEtherscan({start + 5: 1, tip - 1: 1}, tip)
        st, t = {}, [0]
        def clock():
            t[0] += 1
            return t[0]
        ec.collect_token({"symbol": "X", "address": "0x1"}, st, "k", tip, 3, call=fake, clock=clock, now=self.NOW)
        self.assertFalse(st["caught_up"])
        first = st["last_block"]
        ec.collect_token({"symbol": "X", "address": "0x1"}, st, "k", tip, 1e18, call=fake, clock=lambda: 0, now=self.NOW)
        self.assertTrue(st["caught_up"])
        self.assertGreater(st["last_block"], first)
        self.assertEqual(sum(st["daily"].values()), 2)

    def test_partial_window_not_complete(self):
        st = {"start_date": "2026-10-01", "caught_up": True, "daily": {"2026-10-02": 5}}
        r = ec.summarize("X", st, now=self.NOW)
        self.assertFalse(r["complete"])
        self.assertEqual(r["days"], 7)

    def test_collect_without_key(self):
        rows, st = ec.collect(None)
        self.assertEqual(rows, [])
        self.assertTrue(all("건너뜀" in v for v in st.values()))

    def test_annotate_chain_share(self):
        with _tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text(_json.dumps({"assets": [{"symbol": "USDG", "circulating": 100, "chain_count": 4, "chains": [
                {"chain": "X Layer", "amount": 46}, {"chain": "Solana", "amount": 20}, {"chain": "Ethereum", "amount": 9},
                {"chain": "Ink", "amount": 25}]},
                {"symbol": "USDC", "circulating": 200, "chain_count": 9, "chains": [
                {"chain": "Ethereum", "amount": 120}, {"chain": "Solana", "amount": 50}]}]}), encoding="utf-8")
            sh = ot.chain_shares(p)
        rows = [{"symbol": "USDG", "chain": "Ethereum"}, {"symbol": "USDG", "chain": "Tron"}]
        ot.annotate(rows, sh)
        self.assertEqual(rows[0]["chain_share_pct"], 9.0)
        self.assertIsNone(rows[1]["chain_share_pct"])
        self.assertEqual(rows[0]["other_chains"][0], "X Layer 46.0%")
        # USDC 아발란체: 상위 체인 목록 밖 → 25% 미만(목록 최소값)으로 표시
        r2 = [{"symbol": "USDC", "chain": "Avalanche"}]
        ot.annotate(r2, sh)
        self.assertIsNone(r2[0]["chain_share_pct"])
        self.assertEqual(r2[0]["chain_share_lt"], 25.0)
        self.assertEqual(sh["USDC"]["Ethereum"], 60.0)          # 분모는 전체 유통량


if __name__ == "__main__":
    unittest.main()
