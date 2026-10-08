#!/usr/bin/env python3
"""
스테이블코인 감시 - 최근 30일 온체인 이체 건수

종목·체인마다 하루에 토큰 이체(Transfer)가 몇 건 기록됐는지를 Coin Metrics 무료
커뮤니티 API(키 불필요, IP당 6초에 10회)의 TxTfrCnt 지표로 모은다. 2026-10-08
네이선 요청: "최근 30일 분산원장상 스마트 컨트랙트·메인넷의 거래 기록이 몇 건인지".

    python etl/fetch_onchain_tx.py --probe   # 응답 확인만(쓰기 없음)
    python etl/fetch_onchain_tx.py           # site/data/onchain_tx.json

읽는 법
  · '이체 건수'는 토큰 컨트랙트가 남긴 Transfer 기록 수다. 거래(트랜잭션) 하나에 이체가
    여럿 들어 있을 수 있고(예: DEX 경유), 봇·차익거래·자기 지갑 간 이동도 모두 센다.
  · 커뮤니티 API 범위: 이더리움·트론·아발란체 C체인. 솔라나·BNB·베이스·아비트럼 등은
    없다(유료 범위) — 화면에 '체인 범위 밖'으로 밝힌다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.http import get_json  # noqa: E402
import onchain_eth_counts  # noqa: E402

API = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
METRICS = ("TxTfrCnt", "AdrActCnt")   # 이체 건수, 활동 주소 수(없으면 이체 건수만)
DAYS = 30
PAUSE = 0.7                           # 6초 10회 제한 아래로
# (Coin Metrics 자산 코드, 종목, 체인). 2026-10-08 커뮤니티 카탈로그에서 TxTfrCnt 1일 주기 확인.
ASSETS = [
    ("usdt_eth", "USDT", "Ethereum"),
    ("usdt_trx", "USDT", "Tron"),
    ("usdt_avaxc", "USDT", "Avalanche"),
    ("usdc_eth", "USDC", "Ethereum"),
    ("usdc_trx", "USDC", "Tron"),
    ("usdc_avaxc", "USDC", "Avalanche"),
    ("dai", "DAI", "Ethereum"),
    ("usde_eth", "USDe", "Ethereum"),
    ("pyusd_eth", "PYUSD", "Ethereum"),
    ("fdusd_eth", "FDUSD", "Ethereum"),
    ("usdd_eth", "USDD", "Ethereum"),
    ("eurc_eth", "EURC", "Ethereum"),
    ("tusd_eth", "TUSD", "Ethereum"),
    ("tusd_trx", "TUSD", "Tron"),
]
RAW: dict[str, str] = {}
SNAPSHOT = Path(__file__).resolve().parents[1] / "site" / "data" / "snapshot.json"
# DefiLlama 체인 이름 ↔ 이 표의 체인 이름
LLAMA_CHAIN = {"Ethereum": "Ethereum", "Tron": "Tron", "Avalanche": "Avalanche"}


def chain_shares(snapshot_path: Path = SNAPSHOT) -> dict[str, dict]:
    """종목별 {체인: 발행량 비중 %, '_total': 발행잔액 USD}. 스냅숏이 없으면 빈 dict."""
    try:
        snap = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    out: dict[str, dict] = {}
    for a in snap.get("assets") or []:
        chains = a.get("chains") or []
        if not isinstance(chains, list) or not chains:
            continue
        # 스냅숏에는 상위 6개 체인만 실려 온다 — 비중의 분모는 전체 유통량으로 잡는다.
        tot = a.get("circulating") or sum(c.get("amount") or 0 for c in chains)
        if not tot:
            continue
        sym = str(a.get("symbol") or "").upper()
        if sym in out:          # 같은 심볼이 여럿이면 큰 것(목록이 발행잔액 순)
            continue
        out[sym] = {c["chain"]: round((c.get("amount") or 0) / tot * 100, 1) for c in chains if c.get("chain")}
        out[sym]["_top"] = [c["chain"] for c in sorted(chains, key=lambda c: -(c.get("amount") or 0))[:4]]
        out[sym]["_min"] = min(v for k, v in out[sym].items() if not k.startswith("_"))
        out[sym]["_partial_list"] = (a.get("chain_count") or len(chains)) > len(chains)
    return out


def annotate(rows: list[dict], shares: dict[str, dict]) -> None:
    """각 줄에 '이 체인이 발행량에서 차지하는 비중'과 빠진 체인 메모를 붙인다(한계 표시)."""
    for r in rows:
        sh = shares.get(r["symbol"].upper()) or {}
        r["chain_share_pct"] = sh.get(LLAMA_CHAIN.get(r["chain"], r["chain"]))
        # 상위 6개 체인 목록에 없으면 비중은 그 목록의 가장 작은 값보다 작다
        r["chain_share_lt"] = sh.get("_min") if r["chain_share_pct"] is None and sh.get("_partial_list") else None
        others = [c for c in sh.get("_top", []) if c != LLAMA_CHAIN.get(r["chain"], r["chain"]) and not c.startswith("_")]
        r["other_chains"] = [f"{c} {sh.get(c)}%" for c in others if sh.get(c)]


def fetch_asset(code: str, start: str) -> list[dict]:
    """한 자산의 일별 지표. 활동 주소 수가 커뮤니티 범위 밖이면 이체 건수만 다시 받는다."""
    rows: list[dict] = []
    for metrics in (METRICS, METRICS[:1]):
        q = {"assets": code, "metrics": ",".join(metrics), "frequency": "1d",
             "start_time": start, "page_size": 100}
        try:
            time.sleep(PAUSE)
            r = get_json(f"{API}?{urlencode(q)}", retries=2, timeout=30)
        except RuntimeError as e:
            if len(metrics) > 1:      # 지표 하나가 막혀 전체가 거절된 경우
                continue
            raise
        RAW[code] = json.dumps(r, ensure_ascii=False)[:300]
        rows = r.get("data") or []
        break
    return rows


def parse_rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        d = str(r.get("time", ""))[:10]
        try:
            n = int(float(r["TxTfrCnt"]))
        except (KeyError, TypeError, ValueError):
            continue
        a = r.get("AdrActCnt")
        try:
            a = int(float(a)) if a is not None else None
        except (TypeError, ValueError):
            a = None
        out.append({"date": d, "tx": n, "active": a})
    return sorted(out, key=lambda x: x["date"])


def summarize(code: str, sym: str, chain: str, daily: list[dict], days: int = DAYS) -> dict:
    last = daily[-days:]
    prev = daily[-2 * days:-days] if len(daily) > days else []
    tot = sum(x["tx"] for x in last)
    ptot = sum(x["tx"] for x in prev) if len(prev) == days else None
    act = [x["active"] for x in last if x["active"] is not None]
    return {
        "code": code, "symbol": sym, "chain": chain,
        "days": len(last), "from": last[0]["date"] if last else None, "to": last[-1]["date"] if last else None,
        "tx_30d": tot, "tx_avg": round(tot / len(last)) if last else None,
        "tx_prev_30d": ptot,
        "chg_pct": round((tot / ptot - 1) * 100, 1) if ptot else None,
        "active_avg": round(sum(act) / len(act)) if act else None,
        "daily": [[x["date"], x["tx"]] for x in last],
    }


def build(results: dict[str, list[dict] | Exception], days: int = DAYS,
          extra_rows: list[dict] | None = None, extra_status: dict | None = None,
          shares: dict[str, dict] | None = None) -> dict:
    rows, status = [], {}
    for code, sym, chain in ASSETS:
        got = results.get(code)
        if isinstance(got, Exception) or got is None:
            status[code] = f"실패: {str(got)[:120]}"
            continue
        daily = parse_rows(got)
        if not daily:
            status[code] = "데이터 없음"
            continue
        status[code] = "ok"
        row = summarize(code, sym, chain, daily, days)
        row["source"] = "Coin Metrics"
        row["complete"] = True
        rows.append(row)
    for r in extra_rows or []:
        rows.append(dict(r, code=f"etherscan:{r['symbol']}"))
    for k, v in (extra_status or {}).items():
        status[f"etherscan:{k}"] = v
    annotate(rows, shares or {})
    rows.sort(key=lambda r: -r["tx_30d"])
    by_sym: dict[str, int] = {}
    for r in rows:
        by_sym[r["symbol"]] = by_sym.get(r["symbol"], 0) + r["tx_30d"]
    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "is_sample": False,
            "window_days": days,
            "source": "Coin Metrics Community API (TxTfrCnt·AdrActCnt, 일 단위 UTC) · "
                      "Etherscan getLogs 직접 집계(USDS·USD1·USDG·RLUSD 이더리움 몫)",
            "chains_covered": sorted({r["chain"] for r in rows}),
            "chains_missing": ["Solana", "BNB Chain", "Base", "Arbitrum", "Polygon", "Optimism"],
            "status": status,
            "note": "이체 건수는 토큰 Transfer 기록 수다. 트랜잭션 1건에 이체가 여럿일 수 있고 봇·차익거래·"
                    "자기 지갑 간 이동도 포함된다. 무료 출처 범위라 이더리움·트론·아발란체만 센다.",
            "not_covered": [
                {"symbol": "USD1", "missing": "솔라나·BNB 체인 등(발행량의 약 3분의 2)"},
                {"symbol": "USDG", "missing": "X Layer·Robinhood Chain·솔라나 등(발행량의 약 90%)"},
                {"symbol": "RLUSD", "missing": "XRP 원장(발행량의 약 절반)"},
                {"symbol": "USDS", "missing": "아비트럼·솔라나·베이스(발행량의 1% 남짓)"},
                {"symbol": "USDT·USDC", "missing": "솔라나·BNB 체인·베이스·아비트럼·폴리곤 등"},
            ],
        },
        "totals": {"tx_30d": sum(r["tx_30d"] for r in rows), "by_symbol": by_sym},
        "rows": rows,
    }


def collect(days: int = DAYS, eth_state: Path | None = None) -> dict:
    start = (datetime.now(timezone.utc) - timedelta(days=2 * days + 3)).strftime("%Y-%m-%d")
    res: dict[str, list[dict] | Exception] = {}
    for code, sym, chain in ASSETS:
        try:
            res[code] = fetch_asset(code, start)
        except Exception as e:  # 한 자산 실패가 나머지를 막지 않게
            res[code] = e
    # Coin Metrics 무료 범위 밖 종목(USDS·USD1·USDG·RLUSD)의 이더리움 몫 — Etherscan 직접 집계
    try:
        eth_rows, eth_status = onchain_eth_counts.collect(
            os.environ.get("ETHERSCAN_API_KEY"), eth_state or onchain_eth_counts.STATE)
    except Exception as e:
        eth_rows, eth_status = [], {"전체": f"실패: {str(e)[:120]}"}
    return build(res, days, eth_rows, eth_status, chain_shares())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/data")
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args()
    # --probe 는 저장소 상태 파일을 건드리지 않게 임시 경로를 쓴다.
    eth_state = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "onchain_eth_state_probe.json" if args.probe else None
    res = collect(eth_state=eth_state)
    for r in res["rows"]:
        print(f"  {r['symbol']:6s} {r['chain']:10s} 30일 {r['tx_30d']:>12,}건 · 일평균 {r['tx_avg']:>10,} · "
              f"전 30일 대비 {r['chg_pct']}% · 활동 주소 {r['active_avg']} · 이 체인 비중 {r.get('chain_share_pct')}%"
              f" · {r['days']}일{'' if r.get('complete') else '(채우는 중)'} · {r.get('source')}")
    bad = {k: v for k, v in res["meta"]["status"].items() if v != "ok"}
    if bad:
        print(f"  실패·없음: {bad}", file=sys.stderr)
    if args.probe:
        for k, v in list(RAW.items())[:3]:
            print(f"  [원문 {k}] {v}")
        return
    if not res["rows"]:
        print("수집된 자산이 없어 파일을 쓰지 않음", file=sys.stderr)
        sys.exit(1)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "onchain_tx.json").write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":")),
                                         encoding="utf-8")


if __name__ == "__main__":
    main()
