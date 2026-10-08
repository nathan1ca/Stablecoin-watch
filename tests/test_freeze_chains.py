#!/usr/bin/env python3
"""동결 조치 다중 체인(트론·솔라나·체인별 집계) 테스트. 네트워크 없음."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "etl"))

import fetch_freeze  # noqa: E402
import freeze_solana as fs  # noqa: E402
import freeze_tron as ft  # noqa: E402

NOW = 1_791_400_000          # 2026-10-08 무렵
SINCE = NOW - 365 * 86400


class TestTron(unittest.TestCase):
    def fake(self, url):
        if "AddedBlackList" in url and "fingerprint" not in url:
            return {"data": [{"event_name": "AddedBlackList", "block_timestamp": (NOW - 100) * 1000,
                              "transaction_id": "t1", "result": {"_user": "TXabc"}}],
                    "meta": {"fingerprint": "fp2"}}
        if "AddedBlackList" in url:
            return {"data": [{"event_name": "AddedBlackList", "block_timestamp": (NOW - 200) * 1000,
                              "transaction_id": "t2", "result": {"_user": "TXdef"}}], "meta": {}}
        if "DestroyedBlackFunds" in url:
            return {"data": [{"event_name": "DestroyedBlackFunds", "block_timestamp": (NOW - 50) * 1000,
                              "transaction_id": "t3",
                              "result": {"_blackListedUser": "TXabc", "_balance": "2500000000"}}], "meta": {}}
        return {"data": [], "meta": {}}

    def test_pages_and_amount(self):
        ev, st = ft.collect(SINCE, fetch=self.fake)
        self.assertTrue(st["ok"])
        kinds = sorted(e["kind"] for e in ev)
        self.assertEqual(kinds, ["freeze", "freeze", "seize"])   # 두 쪽에 걸친 동결 2건
        seize = next(e for e in ev if e["kind"] == "seize")
        self.assertEqual(seize["units"], 2500.0)
        self.assertEqual(seize["chain"], "Tron")
        self.assertEqual(seize["addr"], "TXabc")

    def test_failure_is_reported_not_raised(self):
        def boom(url):
            raise RuntimeError("HTTP 429")
        ev, st = ft.collect(SINCE, fetch=boom)
        self.assertEqual(ev, [])
        self.assertFalse(st["ok"])


MINT = "MINTxxx"
FA = "AUTHyyy"


def sol_tx(kind, acct, mint=MINT, err=None):
    return {"meta": {"err": err, "innerInstructions": []},
            "transaction": {"message": {"instructions": [
                {"program": "spl-token", "parsed": {"type": kind, "info": {"account": acct, "mint": mint}}},
                {"program": "system", "parsed": {"type": "advanceNonce", "info": {}}}]}}}


class FakeSolana:
    """동결 권한 주소의 거래 목록(최신순)과 거래 내용을 흉내 낸다."""

    def __init__(self, sigs, txs):
        self.sigs, self.txs, self.opened = sigs, txs, 0

    def __call__(self, method, params):
        if method == "getAccountInfo":
            return {"value": {"data": {"parsed": {"info": {"freezeAuthority": FA}}}}}
        if method == "getSignaturesForAddress":
            opts = params[1]
            names = [s["signature"] for s in self.sigs]
            lo = names.index(opts["before"]) + 1 if opts.get("before") else 0
            hi = names.index(opts["until"]) if opts.get("until") else len(names)
            return self.sigs[lo:hi][: opts.get("limit", 1000)]
        if method == "getTransaction":
            self.opened += 1
            return self.txs.get(params[0])
        raise AssertionError(method)


class TestSolana(unittest.TestCase):
    def test_parse_only_matching_mint(self):
        self.assertEqual(fs.freeze_events_in_tx(sol_tx("freezeAccount", "A1"), MINT), [("freeze", "A1")])
        self.assertEqual(fs.freeze_events_in_tx(sol_tx("thawAccount", "A1"), MINT), [("unfreeze", "A1")])
        self.assertEqual(fs.freeze_events_in_tx(sol_tx("freezeAccount", "A1", mint="OTHER"), MINT), [])
        self.assertEqual(fs.freeze_events_in_tx(sol_tx("freezeAccount", "A1", err={"x": 1}), MINT), [])

    def test_incremental_backfill_then_new(self):
        spec = {"issuer": "Tether", "symbol": "USDT", "mint": MINT}
        sigs = [{"signature": f"s{i}", "blockTime": NOW - i * 1000, "err": None} for i in range(10)]
        txs = {"s2": sol_tx("freezeAccount", "A2"), "s7": sol_tx("freezeAccount", "A7"),
               "s8": sol_tx("thawAccount", "A2")}
        fake = FakeSolana(sigs, txs)
        st = {}
        old = fs.BACKFILL_PER_RUN
        fs.BACKFILL_PER_RUN = 4
        try:
            ev, info = fs.collect_mint(spec, st, SINCE, rpc=fake)   # 1회차: 최신 4건(s0~s3)
            self.assertEqual([e["addr"] for e in ev], ["A2"])
            self.assertFalse(info["done"])
            ev, info = fs.collect_mint(spec, st, SINCE, rpc=fake)   # 2회차: s4~s7
            ev, info = fs.collect_mint(spec, st, SINCE, rpc=fake)   # 3회차: s8~s9, 끝
            self.assertTrue(info["done"])
            self.assertEqual(sorted((e["kind"], e["addr"]) for e in ev),
                             [("freeze", "A2"), ("freeze", "A7"), ("unfreeze", "A2")])
            # 새 거래가 생기면 그것만 연다
            fake.sigs.insert(0, {"signature": "n1", "blockTime": NOW + 10, "err": None})
            fake.txs["n1"] = sol_tx("freezeAccount", "N1")
            before = fake.opened
            ev, info = fs.collect_mint(spec, st, SINCE, rpc=fake)
            self.assertEqual(fake.opened - before, 1)
            self.assertIn("N1", [e["addr"] for e in ev])
        finally:
            fs.BACKFILL_PER_RUN = old

    def test_stops_at_lookback(self):
        spec = {"issuer": "Circle", "symbol": "USDC", "mint": MINT}
        sigs = [{"signature": "a", "blockTime": NOW, "err": None},
                {"signature": "b", "blockTime": SINCE - 10, "err": None}]
        fake = FakeSolana(sigs, {"a": sol_tx("freezeAccount", "X"), "b": sol_tx("freezeAccount", "OLD")})
        ev, info = fs.collect_mint(spec, {}, SINCE, rpc=fake)
        self.assertTrue(info["done"])
        self.assertEqual([e["addr"] for e in ev], ["X"])

    def test_state_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            fake = FakeSolana([{"signature": "a", "blockTime": NOW, "err": None}],
                              {"a": sol_tx("freezeAccount", "X")})
            old = fs.MINTS
            fs.MINTS = [{"issuer": "Circle", "symbol": "USDC", "mint": MINT}]
            try:
                ev, st = fs.collect(SINCE, p, rpc=fake)
            finally:
                fs.MINTS = old
            self.assertEqual(len(ev), 1)
            self.assertTrue(json.loads(p.read_text())[MINT]["done"])


class TestSummary(unittest.TestCase):
    def test_by_chain_and_totals(self):
        ev = [
            {"t": NOW, "chain": "Ethereum", "issuer": "Tether", "symbol": "USDT", "kind": "freeze", "addr": "a", "units": None, "tx": "1"},
            {"t": NOW, "chain": "Tron", "issuer": "Tether", "symbol": "USDT", "kind": "freeze", "addr": "b", "units": None, "tx": "2"},
            {"t": NOW, "chain": "Tron", "issuer": "Tether", "symbol": "USDT", "kind": "seize", "addr": "b", "units": 100.0, "tx": "3"},
            {"t": NOW, "chain": "Solana", "issuer": "Circle", "symbol": "USDC", "kind": "freeze", "addr": "c", "units": None, "tx": "4"},
        ]
        st = [{"chain": "Ethereum", "symbol": "USDT", "ok": True},
              {"chain": "Base", "symbol": "USDC", "ok": False, "note": "조회 불가"},
              {"chain": "Tron", "symbol": "USDT", "ok": True},
              {"chain": "Solana", "symbol": "USDC", "ok": True, "complete": False, "note": "채우는 중"}]
        d = fetch_freeze.summarize(ev, st, 365, [])
        self.assertEqual(d["totals"]["freeze"], 3)
        self.assertEqual(d["totals"]["seize"], 1)
        self.assertEqual(d["totals"]["seized_units"], 100.0)
        usdt = next(r for r in d["issuers"] if r["symbol"] == "USDT")
        self.assertEqual(usdt["chains"], {"Ethereum": 1, "Tron": 2})
        bc = {(r["chain"], r["symbol"]): r for r in d["by_chain"]}
        self.assertFalse(bc[("Base", "USDC")]["ok"])
        self.assertFalse(bc[("Solana", "USDC")]["complete"])
        self.assertEqual(bc[("Tron", "USDT")]["seize"], 1)
        self.assertNotIn("Base", d["meta"]["chains"])
        self.assertEqual(d["meta"]["chain"], "3개 체인")
        day = list(d["daily"]["USDT"].values())
        self.assertEqual(sum(c[0] for c in day), 2)   # 동결 2(해제 제외)
        self.assertEqual(sum(c[1] for c in day), 1)   # 소각 1

    def test_paid_chains_skipped_without_calls(self):
        calls = []
        old = fetch_freeze.call
        fetch_freeze.call = lambda params, key, retries=3: (
            calls.append(params["chainid"]) or ("1" if params["module"] == "block" else []))
        try:
            ev, st, notes = fetch_freeze.collect_evm(SINCE, "k")
        finally:
            fetch_freeze.call = old
        self.assertFalse(set(calls) & fetch_freeze.PAID_CHAINS)
        skipped = [x for x in st if x["chain"] in ("Base", "Optimism", "Avalanche")]
        self.assertTrue(skipped and all(not x["ok"] for x in skipped))

    def test_no_bnb_peg_tokens(self):
        self.assertNotIn(56, {s["chainid"] for s in fetch_freeze.ISSUERS})


if __name__ == "__main__":
    unittest.main()
