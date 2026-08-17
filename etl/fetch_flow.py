#!/usr/bin/env python3
"""
스테이블코인 감시 - 국경 간 온체인 자금흐름 (이더리움 USDT·USDC 코너, 좁은 대리지표)

김치프리미엄이 재정거래 유인이라면, 이건 그 유인을 따라 실제로 얼마나
움직였는지를 온체인에서 재는 시도다. 다만 아래 한계를 먼저 읽어야 한다.

*** 이 모듈이 재는 것은 전체 자금흐름이 아니라 극히 일부 통로다 ***

  1. 이더리움 메인넷만 본다. 국내 재정거래는 역사적으로 트론(USDT-TRC20)과
     리플(XRP) 통로 비중이 크다고 알려져 있는데, 둘 다 여기 없다.
  2. 한국 거래소 중 업비트·빗썸의 '핫월렛/콜드월렛'으로 태그된 소수 주소만
     본다. 코인원은 Etherscan에 태그된 주소가 5만 개가 넘는데, 이는 이용자별
     입금주소가 각각 태그된 것이라 '거래소 지갑'으로 묶어 볼 수 없다.
     코빗·고팍스는 신뢰할 만한 공개 태그를 확인하지 못해 아예 뺐다.
  3. 해외 비교군은 Binance·OKX·Bybit 세 곳의 최대 단일 핫월렛만 쓴다.
     이 세 곳 각각이 운용하는 다른 지갑들, 그리고 Coinbase·Kraken 등은
     빠져 있다.
  4. 추적 자산은 USDT·USDC 둘이다. BTC는 애초에 이더리움 주소가 없다.

즉 이 숫자가 작다고 해서 실제 순유출이 작다는 뜻이 아니다. '이 좁은
통로로 관측되는 만큼은 최소 이 정도'라는 하한선으로만 읽어야 한다.
Etherscan V2 API 키가 필요하다(발행사 동결 모듈과 같은 키 재사용 가능).

    export ETHERSCAN_API_KEY=...
    python etl/fetch_flow.py --probe
    python etl/fetch_flow.py --days 180
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = "https://api.etherscan.io/v2/api"
UA = "stablecoin-watch/0.2"
PAUSE = 0.25
CHAIN_ID = 1  # 이더리움 메인넷

# 추적 자산. etl/fetch_freeze.py 의 ISSUERS 와 같은 컨트랙트 주소를 쓴다.
ASSETS = {
    "USDT": {"address": "0xdAC17F958D2ee523a2206206994597C13D831ec7", "decimals": 6},
    "USDC": {"address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "decimals": 6},
}

# 아래 주소는 전부 Etherscan의 공개 Name Tag(누구나 열람 가능)로 확인한 것이다.
# 프로젝트 README 의 "국경 간 흐름" 절에 근거 링크를 남겨뒀다.
KR_WALLETS = {
    "0x390de26d772d2e2005c6d1d24afc902bae37a4bb": "Upbit 1",
    "0xba826fec90cefdf6706858e5fbafcb27a290fbe0": "Upbit 2",
    "0x5e032243d507c743b061ef021e2ec7fcc6d3ab89": "Upbit 3",
    "0xc9cf0ec93d764f5c9571fd12f764bae7fc87c84e": "Upbit Cold Wallet",
    "0x17e5545b11b468072283cee1f066a059fb0dbf24": "Bithumb Hot Wallet",
}
# 각 거래소에서 잔액이 가장 큰 단일 핫월렛 하나씩만 골랐다. 같은 거래소가
# 굴리는 다른 지갑들은 여기 없다 — 그만큼 이 코너는 더 좁아진다.
GLOBAL_WALLETS = {
    "0x28c6c06298d514db089934071355e5743bf21d60": "Binance 14",
    "0xa9ac43f5b5e38155a288d1a01d2cbc4478e14573": "OKX Hot Wallet 3",
    "0xf89d7b9c864f589bbf53a82105107622b35eaa40": "Bybit Hot Wallet",
}


def api_key() -> str | None:
    return os.environ.get("ETHERSCAN_API_KEY") or None


def call(params: dict, key: str, retries: int = 3):
    params = {**params, "chainid": CHAIN_ID, "apikey": key}
    url = API + "?" + urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            time.sleep(PAUSE)
            req = Request(url, headers={"User-Agent": UA})
            with urlopen(req, timeout=45) as r:
                body = json.loads(r.read().decode("utf-8"))
            if body.get("status") == "1":
                return body.get("result") or []
            msg = str(body.get("message", "")) + " " + str(body.get("result", ""))
            if "No transactions found" in msg:
                return []
            if "rate limit" in msg.lower():
                time.sleep(1.5 * (attempt + 1))
                last = RuntimeError(msg)
                continue
            raise RuntimeError(msg.strip())
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Etherscan 호출 실패: {last}")


def block_at(ts: int, key: str) -> int:
    r = call({"module": "block", "action": "getblocknobytime", "timestamp": ts, "closest": "before"}, key)
    return int(r)


def token_transfers(address: str, contract: str, from_block: int, key: str) -> list[dict]:
    out, page = [], 1
    while True:
        r = call({"module": "account", "action": "tokentx", "address": address,
                  "contractaddress": contract, "startblock": from_block, "endblock": 99999999,
                  "page": page, "offset": 1000, "sort": "desc"}, key)
        if not r:
            break
        out.extend(r)
        if len(r) < 1000:
            break
        page += 1
        if page > 10:
            break
    return out


def collect(days: int, key: str) -> dict:
    now = int(datetime.now(timezone.utc).timestamp())
    since = now - days * 86400
    from_b = block_at(since, key)

    events = []
    notes = []
    for addr, kr_name in KR_WALLETS.items():
        for symbol, spec in ASSETS.items():
            print(f"  {kr_name} ({addr[:10]}…) {symbol} 이력 조회…")
            try:
                txs = token_transfers(addr, spec["address"], from_b, key)
            except RuntimeError as e:
                notes.append(f"{kr_name} {symbol}: {e}")
                continue
            for tx in txs:
                frm, to = tx.get("from", "").lower(), tx.get("to", "").lower()
                counterparty = to if frm == addr.lower() else frm
                g_name = GLOBAL_WALLETS.get(counterparty)
                if not g_name:
                    continue  # 라벨된 해외 지갑과 직접 오간 것만 코너로 인정
                value = int(tx.get("value", 0)) / 10 ** spec["decimals"]
                direction = "outflow" if frm == addr.lower() else "inflow"  # KR 기준
                events.append({
                    "t": int(tx.get("timeStamp", 0)),
                    "asset": symbol, "kr_wallet": kr_name, "global_wallet": g_name,
                    "direction": direction, "amount": round(value, 2),
                    "tx": tx.get("hash"),
                })

    events.sort(key=lambda e: -e["t"])

    def sum_side(direction: str, symbol: str | None = None) -> float:
        return sum(e["amount"] for e in events
                   if e["direction"] == direction and (symbol is None or e["asset"] == symbol))

    inflow = sum_side("inflow")
    outflow = sum_side("outflow")

    # 일별 순유출(+) / 순유입(-) 시계열. 자산이 다른 걸 그냥 더해도 되는 이유는
    # 둘 다 1달러 페그를 목표로 하는 스테이블코인이라 액면가 합산이 의미를
    # 유지하기 때문이다. 개별 자산 합계는 by_asset 에 별도로 둔다.
    daily: dict[str, float] = {}
    for e in events:
        d = datetime.fromtimestamp(e["t"], timezone.utc).strftime("%Y-%m-%d")
        sign = 1 if e["direction"] == "outflow" else -1
        daily[d] = daily.get(d, 0.0) + sign * e["amount"]
    daily_rows = [{"date": d, "net_outflow_usd": round(v, 2)} for d, v in sorted(daily.items())]

    by_asset = [
        {"asset": s, "inflow": round(sum_side("inflow", s), 2), "outflow": round(sum_side("outflow", s), 2),
         "net_outflow": round(sum_side("outflow", s) - sum_side("inflow", s), 2)}
        for s in ASSETS
    ]
    by_exchange: dict[str, dict] = {}
    for e in events:
        row = by_exchange.setdefault(e["global_wallet"], {"inflow": 0.0, "outflow": 0.0})
        row[e["direction"]] += e["amount"]
    by_exchange_rows = [
        {"exchange": name, "inflow": round(v["inflow"], 2), "outflow": round(v["outflow"], 2),
         "net_outflow": round(v["outflow"] - v["inflow"], 2)}
        for name, v in sorted(by_exchange.items(), key=lambda x: -(x[1]["inflow"] + x[1]["outflow"]))
    ]

    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "lookback_days": days, "chain": "Ethereum", "assets": list(ASSETS), "is_sample": False,
            "kr_wallets": KR_WALLETS, "global_wallets": GLOBAL_WALLETS,
            "coverage_note": "업비트·빗썸 태그 지갑 ↔ Binance·OKX·Bybit 각 1개 핫월렛 사이의 "
                             "USDT·USDC 직접 이체만 집계. 트론·XRP 통로, 코인원·코빗·고팍스, "
                             "그리고 각 거래소의 다른 지갑들은 제외.",
            "notes": notes,
        },
        "totals": {
            "inflow_usd": round(inflow, 2), "outflow_usd": round(outflow, 2),
            "net_outflow_usd": round(outflow - inflow, 2), "event_count": len(events),
        },
        "by_asset": by_asset,
        "by_exchange": by_exchange_rows,
        "daily": daily_rows,
        "events": events[:150],
    }


def probe(key: str):
    print("라벨 지갑 목록")
    print("  한국:", ", ".join(f"{n}({a[:10]}…)" for a, n in KR_WALLETS.items()))
    print("  해외:", ", ".join(f"{n}({a[:10]}…)" for a, n in GLOBAL_WALLETS.items()))
    print("  자산:", ", ".join(f"{s}({v['address'][:10]}…)" for s, v in ASSETS.items()))
    if not key:
        print("\nETHERSCAN_API_KEY 없음 — 실제 호출은 생략합니다.")
        return
    addr = next(iter(KR_WALLETS))
    for symbol, spec in ASSETS.items():
        print(f"\n{KR_WALLETS[addr]} 최근 {symbol} 이체 3건 조회…")
        txs = call({"module": "account", "action": "tokentx", "address": addr,
                    "contractaddress": spec["address"], "page": 1, "offset": 3, "sort": "desc"}, key)
        for tx in txs:
            print(f"  {tx.get('timeStamp')} {tx.get('from')[:10]}…→{tx.get('to')[:10]}… "
                  f"{int(tx.get('value', 0)) / 10**spec['decimals']:,.0f} {symbol}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--out", default="site/data")
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args()

    key = api_key()
    if args.probe:
        probe(key or "")
        return
    if not key:
        print("ETHERSCAN_API_KEY 가 없어 자금흐름 수집을 건너뜁니다.", file=sys.stderr)
        return

    print(f"최근 {args.days}일 KR↔해외 3거래소 USDT·USDC 코너 수집…")
    try:
        data = collect(args.days, key)
    except RuntimeError as e:
        print(f"Etherscan 호출 실패로 자금흐름 수집을 건너뜁니다: {e}", file=sys.stderr)
        return

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "flow.json").write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    t = data["totals"]
    sign = "유출" if t["net_outflow_usd"] >= 0 else "유입"
    print(f"\n완료 — 순{sign} ${abs(t['net_outflow_usd']):,.0f} / 이벤트 {t['event_count']}건")
    for r in data["by_exchange"]:
        print(f"  {r['exchange']:18s} 순유출 ${r['net_outflow']:,.0f}")
    for n in data["meta"]["notes"]:
        print(f"  참고: {n}", file=sys.stderr)


if __name__ == "__main__":
    main()
