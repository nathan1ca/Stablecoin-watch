#!/usr/bin/env python3
"""
샘플 데이터 생성기.

실제 ETL을 돌리기 전에 화면이 비어 보이지 않도록 형태만 같은 더미 데이터를 만든다.
여기서 나온 수치는 전부 가짜다. meta.is_sample = true 로 표시되고,
사이트 상단에 샘플 안내 띠가 뜬다. `python etl/fetch.py` 를 한 번 돌리면 덮어써진다.
"""
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent))
from fetch import THRESHOLDS, MECHANISM_KO, hhi, grade_peg, grade_redemption, WORST  # noqa

random.seed(17)

SPEC = [
    # symbol, name, mechanism, peg, mcap(USD), dev_bp, chg1, chg7, chg30
    ("USDT", "Tether",            "fiat-backed",   "USD", 1.585e11,   4, 0.10,  0.9,  3.1),
    ("USDC", "USD Coin",          "fiat-backed",   "USD", 7.42e10,   -2, 0.22,  1.6,  6.4),
    ("USDe", "Ethena USDe",       "crypto-backed", "USD", 1.11e10,  -31, -0.9, -4.2, -14.8),
    ("USDS", "Sky Dollar",        "crypto-backed", "USD", 8.9e9,      1, 0.05,  0.4,  2.2),
    ("PYUSD", "PayPal USD",       "fiat-backed",   "USD", 3.1e9,     -3, 0.31,  2.8,  9.7),
    ("DAI",  "Dai",               "crypto-backed", "USD", 2.84e9,     6, -0.02, -0.5, -1.9),
    ("FDUSD", "First Digital USD","fiat-backed",   "USD", 1.42e9,   -18, -0.4, -2.1, -8.3),
    ("RLUSD", "Ripple USD",       "fiat-backed",   "USD", 1.05e9,     2, 0.9,   5.1, 18.2),
    ("USD1", "World Liberty USD", "fiat-backed",   "USD", 9.4e8,     -5, 0.1,   0.8,  4.0),
    ("TUSD", "TrueUSD",           "fiat-backed",   "USD", 4.9e8,   -142, -1.8, -7.4, -27.6),
    ("USDD", "USDD",              "crypto-backed", "USD", 3.6e8,    -22, -0.3, -1.2, -6.1),
    ("EURC", "Euro Coin",         "fiat-backed",   "EUR", 2.9e8,   None, 0.4,   2.2,  7.8),
    ("USDG", "Global Dollar",     "fiat-backed",   "USD", 2.4e8,      1, 0.6,   3.9, 12.4),
    ("FRAX", "Frax",              "algorithmic",   "USD", 1.9e8,    -14, -0.2, -1.0, -3.3),
    ("EURS", "STASIS EURO",       "fiat-backed",   "EUR", 1.4e8,   None, 0.0,   0.1,  0.5),
    ("XSGD", "XSGD",              "fiat-backed",   "SGD", 9.1e7,   None, 0.2,   1.1,  3.0),
    ("USDX", "Stables Labs USDX", "algorithmic",   "USD", 7.2e7,    -47, -1.1, -5.5, -19.0),
    ("BUCK", "Bucket USD",        "crypto-backed", "USD", 5.5e7,      8, 0.1,   0.6,  1.4),
]

CHAINS = [
    ("Ethereum", 0.512), ("Tron", 0.221), ("Solana", 0.061), ("BSC", 0.048),
    ("Arbitrum", 0.031), ("Base", 0.029), ("Hyperliquid", 0.021), ("Avalanche", 0.017),
    ("Polygon", 0.013), ("TON", 0.011), ("Aptos", 0.009), ("Sui", 0.008),
    ("Optimism", 0.007), ("Berachain", 0.006), ("Near", 0.005),
]


def main():
    out = Path(__file__).resolve().parent.parent / "site" / "data"
    out.mkdir(exist_ok=True)

    rows = []
    for sym, name, mech, cur, mcap, dev, c1, c7, c30 in SPEC:
        peg_g = grade_peg(dev)
        red_g = grade_redemption(c30)
        overall = max(peg_g, red_g, key=lambda g: WORST[g])
        chain_share = [0.55, 0.21, 0.09, 0.06, 0.05, 0.04]
        picks = random.sample([c for c, _ in CHAINS], 6)
        rows.append({
            "id": str(len(rows) + 1), "name": name, "symbol": sym,
            "peg_currency": cur, "mechanism": mech,
            "mechanism_ko": MECHANISM_KO.get(mech, mech),
            "circulating": round(mcap, 2), "mcap_usd": round(mcap, 2),
            "price": None if dev is None else round(1 + dev / 10_000, 5),
            "dev_bp": dev, "chg_1d": c1, "chg_7d": c7, "chg_30d": c30,
            "net_30d_usd": round(mcap - mcap / (1 + c30 / 100), 2),
            "grade_peg": peg_g, "grade_redemption": red_g, "grade": overall,
            "chains": [{"chain": p, "amount": round(mcap * s, 2)}
                       for p, s in zip(picks, chain_share)],
            "chain_count": 6 + int(mcap / 2e10),
        })

    total = sum(r["mcap_usd"] for r in rows)
    for r in rows:
        r["share"] = round(r["mcap_usd"] / total * 100, 3)

    by_mech = {}
    for r in rows:
        by_mech[r["mechanism"]] = by_mech.get(r["mechanism"], 0) + r["mcap_usd"]
    mech_rows = [{"mechanism": m, "label": MECHANISM_KO.get(m, m),
                  "amount": round(v, 2), "share": round(v / total * 100, 3)}
                 for m, v in sorted(by_mech.items(), key=lambda x: -x[1])]

    by_cur = {}
    for r in rows:
        by_cur[r["peg_currency"]] = by_cur.get(r["peg_currency"], 0) + r["mcap_usd"]
    cur_rows = [{"currency": c, "amount": round(v, 2), "share": round(v / total * 100, 3)}
                for c, v in sorted(by_cur.items(), key=lambda x: -x[1])]

    chain_rows = [{"chain": c, "amount": round(total * s, 2), "share": round(s * 100, 3)}
                  for c, s in CHAINS]

    algo = next((m["share"] for m in mech_rows if m["mechanism"] == "algorithmic"), 0.0)
    conc = {
        "hhi_issuer": hhi([r["share"] for r in rows]),
        "hhi_chain": hhi([c["share"] for c in chain_rows]),
        "top1_share": rows[0]["share"], "top3_share": round(sum(r["share"] for r in rows[:3]), 2),
        "algo_share": algo,
    }
    alerts = sorted([r for r in rows if r["grade"] in ("watch", "breach")],
                    key=lambda r: (-WORST[r["grade"]], -r["mcap_usd"]))

    snap = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "샘플", "source_url": "https://defillama.com/stablecoins",
            "is_sample": True, "thresholds": THRESHOLDS, "asset_count": len(rows),
        },
        "totals": {
            "circulating_usd": round(total, 2),
            "net_1d_usd": round(total * 0.0014, 2),
            "breach_count": sum(1 for r in rows if r["grade"] == "breach"),
            "watch_count": sum(1 for r in rows if r["grade"] == "watch"),
            "system_grade": "breach" if any(r["grade"] == "breach" for r in rows) else "watch",
        },
        "concentration": conc, "by_mechanism": mech_rows, "by_peg_currency": cur_rows,
        "by_chain": chain_rows,
        "alerts": [{k: r[k] for k in ("symbol", "name", "grade", "grade_peg",
                                      "grade_redemption", "dev_bp", "chg_30d", "mcap_usd")}
                   for r in alerts[:12]],
        "assets": rows,
    }

    # 시계열 — 완만한 성장에 변동을 얹은 가짜 곡선
    now = int(datetime.now(timezone.utc).timestamp())
    pts, v = [], total * 0.62
    for i in range(365):
        v *= 1 + 0.0013 + math.sin(i / 29) * 0.0016 + random.gauss(0, 0.0011)
        pts.append({"t": now - (364 - i) * 86400, "v": round(v, 2)})
    flow = [{"t": p["t"], "v": round((p["v"] - pts[i - 30]["v"]) / pts[i - 30]["v"] * 100, 3)}
            for i, p in enumerate(pts) if i >= 30]

    (out / "snapshot.json").write_text(json.dumps(snap, ensure_ascii=False,
                                                  separators=(",", ":")), encoding="utf-8")
    (out / "history.json").write_text(json.dumps({"total_circulating": pts, "net_30d_pct": flow},
                                                 ensure_ascii=False, separators=(",", ":")),
                                      encoding="utf-8")

    # 동결 조치 샘플
    rows_or_assets = rows
    ISS = [("Tether", "USDT", 168, 41, 6, 42_900_000.0),
           ("Circle", "USDC", 46, 11, 0, 0.0),
           ("Paxos", "PYUSD", 4, 1, 0, 0.0)]
    ev, rows = [], []
    for issuer, sym, fz, uf, sz, amt in ISS:
        rows.append({"issuer": issuer, "symbol": sym, "address": "0x" + "0" * 40,
                     "verified": True, "freeze": fz, "unfreeze": uf, "seize": sz,
                     "seized_units": amt, "matched_signatures": ["샘플"]})
        for kind, n in (("freeze", fz), ("unfreeze", uf), ("seize", sz)):
            for _ in range(n):
                ev.append({
                    "t": now - random.randint(0, 364) * 86400 - random.randint(0, 86000),
                    "issuer": issuer, "symbol": sym, "kind": kind,
                    "addr": "0x" + "".join(random.choice("0123456789abcdef") for _ in range(40)),
                    "units": round(random.uniform(1e4, 9e6), 2) if kind == "seize" else None,
                    "tx": "0x" + "".join(random.choice("0123456789abcdef") for _ in range(64)),
                })
    ev.sort(key=lambda e: -e["t"])
    monthly = {}
    for e in ev:
        m = datetime.fromtimestamp(e["t"], timezone.utc).strftime("%Y-%m")
        monthly.setdefault(m, {})
        if e["kind"] != "unfreeze":
            monthly[m][e["issuer"]] = monthly[m].get(e["issuer"], 0) + 1

    (out / "freeze.json").write_text(json.dumps({
        "meta": {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "lookback_days": 365, "chain": "Ethereum", "is_sample": True,
                 "source": "샘플", "notes": []},
        "totals": {"freeze": sum(r["freeze"] for r in rows),
                   "unfreeze": sum(r["unfreeze"] for r in rows),
                   "seize": sum(r["seize"] for r in rows),
                   "seized_units": round(sum(r["seized_units"] for r in rows), 2),
                   "issuers": len(rows)},
        "issuers": rows,
        "monthly": [{"m": m, **v} for m, v in sorted(monthly.items())],
        "events": ev[:200],
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    print(f"샘플 생성 완료 — {len(rows_or_assets)}종목 / 총 ${total/1e9:,.1f}B / 동결 {len(ev)}건")

    # ── 김치프리미엄 샘플 ──────────────────────────────────
    PREM_ASSETS = [("BTC", 2.8, 0.3, (1.3e8, 1.7e8)), ("ETH", 1.9, -0.1, (4.5e6, 6.5e6)),
                   ("XRP", 5.4, 1.2, (2800, 4200))]
    prows = []
    for name, prem, chg, krw_range in PREM_ASSETS:
        krw = round(random.uniform(*krw_range), 2)
        usd_equiv = krw / 1391.5
        global_usd = usd_equiv / (1 + prem / 100)
        prows.append({"asset": name, "krw_price": krw, "global_usd": round(global_usd, 4),
                     "usd_equiv": round(usd_equiv, 4), "premium_pct": prem, "chg_24h_krw_pct": chg})
    basket = round(sum(r["premium_pct"] for r in prows) / len(prows), 3)
    (out / "premium.json").write_text(json.dumps({
        "meta": {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "is_sample": True, "thresholds": {"watch_pct": 3.0, "breach_pct": 7.0, "inverted_pct": -1.0},
                 "fx_usdkrw": 1391.5, "source": "샘플"},
        "basket_avg_pct": basket,
        "basket_grade": "watch" if basket >= 3 else "sound",
        "assets": prows,
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    pv, ppts = 1.5, []
    for i in range(180):
        pv += math.sin(i / 22) * 0.35 + random.gauss(0, 0.4)
        pv = max(-2, min(9, pv))
        d = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=179 - i)).strftime("%Y-%m-%d")
        ppts.append({"date": d, "premium_pct": round(pv, 3)})
    (out / "premium_history.json").write_text(json.dumps({"points": ppts},
        ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # ── 온체인 코너 자금흐름 샘플 (USDT+USDC, 3개 해외 거래소) ────
    KRW_ADDR = {"0x390de26d772d2e2005c6d1d24afc902bae37a4bb": "Upbit 1",
                "0xba826fec90cefdf6706858e5fbafcb27a290fbe0": "Upbit 2",
                "0x17e5545b11b468072283cee1f066a059fb0dbf24": "Bithumb Hot Wallet"}
    GLB_ADDR = {"0x28c6c06298d514db089934071355e5743bf21d60": "Binance 14",
                "0xa9ac43f5b5e38155a288d1a01d2cbc4478e14573": "OKX Hot Wallet 3",
                "0xf89d7b9c864f589bbf53a82105107622b35eaa40": "Bybit Hot Wallet"}
    fev = []
    for _ in range(140):
        kr = random.choice(list(KRW_ADDR.values()))
        glb = random.choices(list(GLB_ADDR.values()), weights=[0.55, 0.28, 0.17])[0]
        asset = random.choices(["USDT", "USDC"], weights=[0.72, 0.28])[0]
        direction = random.choices(["outflow", "inflow"], weights=[0.58, 0.42])[0]
        fev.append({
            "t": now - random.randint(0, 179) * 86400 - random.randint(0, 86000),
            "asset": asset, "kr_wallet": kr, "global_wallet": glb, "direction": direction,
            "amount": round(random.uniform(5e3, 2.2e6), 2),
            "tx": "0x" + "".join(random.choice("0123456789abcdef") for _ in range(64)),
        })
    fev.sort(key=lambda e: -e["t"])
    daily = {}
    for e in fev:
        d = datetime.fromtimestamp(e["t"], timezone.utc).strftime("%Y-%m-%d")
        sign = 1 if e["direction"] == "outflow" else -1
        daily[d] = daily.get(d, 0.0) + sign * e["amount"]
    inflow = sum(e["amount"] for e in fev if e["direction"] == "inflow")
    outflow = sum(e["amount"] for e in fev if e["direction"] == "outflow")

    by_asset = []
    for s in ["USDT", "USDC"]:
        ai = sum(e["amount"] for e in fev if e["direction"] == "inflow" and e["asset"] == s)
        ao = sum(e["amount"] for e in fev if e["direction"] == "outflow" and e["asset"] == s)
        by_asset.append({"asset": s, "inflow": round(ai, 2), "outflow": round(ao, 2),
                         "net_outflow": round(ao - ai, 2)})
    by_exchange = {}
    for e in fev:
        row = by_exchange.setdefault(e["global_wallet"], {"inflow": 0.0, "outflow": 0.0})
        row[e["direction"]] += e["amount"]
    by_exchange_rows = [{"exchange": n, "inflow": round(v["inflow"], 2), "outflow": round(v["outflow"], 2),
                         "net_outflow": round(v["outflow"] - v["inflow"], 2)}
                        for n, v in sorted(by_exchange.items(), key=lambda x: -(x[1]["inflow"] + x[1]["outflow"]))]

    (out / "flow.json").write_text(json.dumps({
        "meta": {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "lookback_days": 180, "chain": "Ethereum", "assets": ["USDT", "USDC"], "is_sample": True,
                 "kr_wallets": KRW_ADDR, "global_wallets": GLB_ADDR,
                 "coverage_note": "업비트·빗썸 태그 지갑 ↔ Binance·OKX·Bybit 각 1개 핫월렛 사이의 "
                                  "USDT·USDC 직접 이체만 집계. 트론·XRP 통로, 코인원·코빗·고팍스, "
                                  "그리고 각 거래소의 다른 지갑들은 제외."},
        "totals": {"inflow_usd": round(inflow, 2), "outflow_usd": round(outflow, 2),
                   "net_outflow_usd": round(outflow - inflow, 2), "event_count": len(fev)},
        "by_asset": by_asset,
        "by_exchange": by_exchange_rows,
        "daily": [{"date": d, "net_outflow_usd": round(v, 2)} for d, v in sorted(daily.items())],
        "events": fev[:150],
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    print(f"샘플 추가 완료 — 프리미엄 평균 {basket}% / 코너 순유출 {outflow-inflow:,.0f} USD")

    # ── 어테스테이션 시차 샘플 ────────────────────────────────
    from datetime import date as _date
    today = datetime.now(timezone.utc).date()
    attest_rows = []
    for issuer, sym, days_ago, reported, cur in [
        ("Circle", "USDC", 18, 79_170_267_853, None),
        ("Tether", "USDT", 52, 158_000_000_000, None),
    ]:
        as_of = today - __import__("datetime").timedelta(days=days_ago)
        cur_v = cur or reported * random.uniform(0.99, 1.04)
        drift_pct = round((cur_v - reported) / reported * 100, 3)
        grade = "breach" if days_ago >= 75 else "watch" if days_ago >= 45 else "sound"
        attest_rows.append({
            "issuer": issuer, "symbol": sym, "as_of_date": as_of.isoformat(),
            "days_since": days_ago, "reported_circulating": reported,
            "current_circulating": round(cur_v, 2), "drift": round(cur_v - reported, 2),
            "drift_pct": drift_pct, "source_url": "https://example.com", "verified": True,
            "grade": grade,
        })
    (out / "attestation.json").write_text(json.dumps({
        "meta": {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "is_sample": True, "thresholds": {"watch_days": 45, "breach_days": 75,
                                                   "drift_watch_pct": 3.0, "drift_breach_pct": 8.0},
                 "maintenance_note": "샘플 데이터입니다. 실제로는 etl/attestations.json 을 손으로 갱신해야 합니다."},
        "entries": attest_rows,
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # ── XRP 코너 자금흐름 샘플 ────────────────────────────────
    KR_XRP = ["Upbit (rN7n7otQ…)", "Upbit (r9hUMZBc…)", "Bithumb (rPMM1dRp…)", "Bithumb (rNTkgxs5…)"]
    GL_XRP = ["Binance", "OKX", "Bybit"]
    xev = []
    for _ in range(110):
        direction = random.choices(["outflow", "inflow"], weights=[0.56, 0.44])[0]
        xev.append({
            "t": now - random.randint(0, 179) * 86400 - random.randint(0, 86000),
            "kr_wallet": random.choice(KR_XRP),
            "global_wallet": random.choices(GL_XRP, weights=[0.6, 0.25, 0.15])[0],
            "direction": direction, "amount": round(random.uniform(2e4, 6e6), 2),
            "tx": "".join(random.choice("0123456789ABCDEF") for _ in range(64)),
        })
    xev.sort(key=lambda e: -e["t"])
    xdaily = {}
    for e in xev:
        d = datetime.fromtimestamp(e["t"], timezone.utc).strftime("%Y-%m-%d")
        sign = 1 if e["direction"] == "outflow" else -1
        xdaily[d] = xdaily.get(d, 0.0) + sign * e["amount"]
    xinflow = sum(e["amount"] for e in xev if e["direction"] == "inflow")
    xoutflow = sum(e["amount"] for e in xev if e["direction"] == "outflow")
    xby_exchange = {}
    for e in xev:
        row = xby_exchange.setdefault(e["global_wallet"], {"inflow": 0.0, "outflow": 0.0})
        row[e["direction"]] += e["amount"]
    xby_exchange_rows = [{"exchange": n, "inflow": round(v["inflow"], 2), "outflow": round(v["outflow"], 2),
                          "net_outflow": round(v["outflow"] - v["inflow"], 2)}
                         for n, v in sorted(xby_exchange.items(), key=lambda x: -(x[1]["inflow"] + x[1]["outflow"]))]

    (out / "flow_xrp.json").write_text(json.dumps({
        "meta": {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "lookback_days": 180, "chain": "XRPL", "asset": "XRP", "is_sample": True,
                 "kr_account_count": 4, "global_labels_seen": GL_XRP,
                 "coverage_note": "이름이 정확히 'Upbit'/'Bithumb'인 xrpscan 라벨 계정과 "
                                  "'Binance'/'OKX'/'Bybit'인 계정 사이의 네이티브 XRP 결제만 집계. "
                                  "발행 통화(RLUSD 등), 계열사 라벨(Bithumb Global 등)은 제외."},
        "totals": {"inflow_xrp": round(xinflow, 2), "outflow_xrp": round(xoutflow, 2),
                   "net_outflow_xrp": round(xoutflow - xinflow, 2), "event_count": len(xev)},
        "by_exchange": xby_exchange_rows,
        "daily": [{"date": d, "net_outflow_xrp": round(v, 2)} for d, v in sorted(xdaily.items())],
        "events": xev[:150],
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    print(f"샘플 추가 완료 — 어테스테이션 {len(attest_rows)}건 / XRP 코너 순유출 {xoutflow-xinflow:,.0f} XRP")


if __name__ == "__main__":
    main()
