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
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.http import get_json  # noqa: E402

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


def build(results: dict[str, list[dict] | Exception], days: int = DAYS) -> dict:
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
        rows.append(summarize(code, sym, chain, daily, days))
    rows.sort(key=lambda r: -r["tx_30d"])
    by_sym: dict[str, int] = {}
    for r in rows:
        by_sym[r["symbol"]] = by_sym.get(r["symbol"], 0) + r["tx_30d"]
    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "is_sample": False,
            "window_days": days,
            "source": "Coin Metrics Community API (TxTfrCnt·AdrActCnt, 일 단위 UTC)",
            "chains_covered": sorted({r["chain"] for r in rows}),
            "chains_missing": ["Solana", "BNB Chain", "Base", "Arbitrum", "Polygon", "Optimism"],
            "status": status,
            "note": "이체 건수는 토큰 Transfer 기록 수다. 트랜잭션 1건에 이체가 여럿일 수 있고 봇·차익거래·"
                    "자기 지갑 간 이동도 포함된다. 커뮤니티 무료 범위라 이더리움·트론·아발란체만 센다.",
        },
        "totals": {"tx_30d": sum(r["tx_30d"] for r in rows), "by_symbol": by_sym},
        "rows": rows,
    }


def collect(days: int = DAYS) -> dict:
    start = (datetime.now(timezone.utc) - timedelta(days=2 * days + 3)).strftime("%Y-%m-%d")
    res: dict[str, list[dict] | Exception] = {}
    for code, sym, chain in ASSETS:
        try:
            res[code] = fetch_asset(code, start)
        except Exception as e:  # 한 자산 실패가 나머지를 막지 않게
            res[code] = e
    return build(res, days)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/data")
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args()
    res = collect()
    for r in res["rows"]:
        print(f"  {r['symbol']:6s} {r['chain']:10s} 30일 {r['tx_30d']:>12,}건 · 일평균 {r['tx_avg']:>10,} · "
              f"전 30일 대비 {r['chg_pct']}% · 활동 주소 {r['active_avg']}")
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
