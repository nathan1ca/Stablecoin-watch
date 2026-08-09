#!/usr/bin/env python3
"""
스테이블코인 감시 - 김치프리미엄

국경 간 자금흐름을 라벨링된 지갑 없이도 잡을 수 있는 유일한 지표다.
프리미엄 자체가 재정거래 유인의 크기이므로, 실제 순유출입보다 선행하는
경우가 많다. API 키가 전혀 필요 없다.

    python etl/fetch_premium.py --probe   # 세 API 응답 형식 확인 (필수)
    python etl/fetch_premium.py

데이터 출처 (전부 무인증 공개 API):
  - Upbit 시세: https://docs.upbit.com
  - Binance 시세: https://binance-docs.github.io/apidocs/spot/en/
  - USD/KRW: Frankfurter (ECB 등 여러 소스 취합, https://frankfurter.dev)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

UA = "stablecoin-watch/0.1"

# market(Upbit) : symbol(Binance) : 표시 이름
ASSETS = [
    ("KRW-BTC", "BTCUSDT", "BTC"),
    ("KRW-ETH", "ETHUSDT", "ETH"),
    ("KRW-XRP", "XRPUSDT", "XRP"),  # 국내 재정거래에서 역사적으로 가장 많이 쓰인 자산
]

# 법정 기준이 아니라 임의의 관측선. etl/fetch.py 의 THRESHOLDS 와 같은 성격.
THRESHOLDS = {
    "watch_pct": 3.0,     # 평균 프리미엄이 이 값을 넘으면 주의
    "breach_pct": 7.0,    # 이 값을 넘으면 경보
    "inverted_pct": -1.0,  # 역프리미엄(마이너스) 진입 기준
}


def get_json(url: str, retries: int = 3):
    last = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"{url} 수집 실패: {last}")


def upbit_ticker(markets: list[str]) -> dict:
    url = "https://api.upbit.com/v1/ticker?" + urlencode({"markets": ",".join(markets)})
    rows = get_json(url)
    return {r["market"]: r for r in rows}


def binance_prices(symbols: list[str]) -> dict:
    # symbols 파라미터는 JSON 배열을 그대로 문자열로 넣는다: ["BTCUSDT","ETHUSDT"]
    sym_param = json.dumps(symbols, separators=(",", ":"))
    url = "https://api.binance.com/api/v3/ticker/price?" + urlencode({"symbols": sym_param})
    rows = get_json(url)
    return {r["symbol"]: float(r["price"]) for r in rows}


def usdkrw_rate() -> float:
    url = "https://api.frankfurter.dev/v1/latest?" + urlencode({"base": "USD", "symbols": "KRW"})
    r = get_json(url)
    return float(r["rates"]["KRW"])


def usdkrw_history(days: int) -> dict:
    """날짜(YYYY-MM-DD) → 환율. 주말·휴일은 직전 영업일 값이 이어진다."""
    start = (datetime.now(timezone.utc) - timedelta(days=days + 5)).strftime("%Y-%m-%d")
    url = f"https://api.frankfurter.dev/v1/{start}..?" + urlencode({"base": "USD", "symbols": "KRW"})
    r = get_json(url)
    rates = r.get("rates", {})
    return {d: v["KRW"] for d, v in rates.items() if "KRW" in v}


def upbit_candles(market: str, count: int) -> list[dict]:
    url = f"https://api.upbit.com/v1/candles/days?" + urlencode({"market": market, "count": count})
    return get_json(url)  # 최신순(내림차순)으로 온다


def binance_klines(symbol: str, days: int) -> list[list]:
    url = "https://api.binance.com/api/v3/klines?" + urlencode({
        "symbol": symbol, "interval": "1d", "limit": min(days, 1000),
    })
    return get_json(url)  # 오래된순(오름차순)


def grade(pct: float | None) -> str:
    if pct is None:
        return "unknown"
    if pct <= THRESHOLDS["inverted_pct"]:
        return "watch"  # 역프리미엄도 이례적 신호이므로 주의로 취급
    if pct >= THRESHOLDS["breach_pct"]:
        return "breach"
    if pct >= THRESHOLDS["watch_pct"]:
        return "watch"
    return "sound"


def build_snapshot() -> dict:
    markets = [a[0] for a in ASSETS]
    symbols = [a[1] for a in ASSETS]

    upbit = upbit_ticker(markets)
    binance = binance_prices(symbols)
    fx = usdkrw_rate()

    rows = []
    for market, symbol, label in ASSETS:
        up = upbit.get(market)
        bn = binance.get(symbol)
        if not up or not bn:
            rows.append({"asset": label, "premium_pct": None, "krw_price": None,
                         "usd_equiv": None, "global_usd": bn})
            continue
        krw_price = float(up["trade_price"])
        usd_equiv = krw_price / fx
        premium = (usd_equiv / bn - 1) * 100
        rows.append({
            "asset": label,
            "krw_price": krw_price,
            "global_usd": bn,
            "usd_equiv": round(usd_equiv, 4),
            "premium_pct": round(premium, 3),
            "chg_24h_krw_pct": round(float(up.get("signed_change_rate", 0)) * 100, 3),
        })

    valid = [r["premium_pct"] for r in rows if r["premium_pct"] is not None]
    basket_avg = round(sum(valid) / len(valid), 3) if valid else None

    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "is_sample": False,
            "thresholds": THRESHOLDS,
            "fx_usdkrw": round(fx, 2),
            "source": "Upbit · Binance · Frankfurter(FX)",
        },
        "basket_avg_pct": basket_avg,
        "basket_grade": grade(basket_avg),
        "assets": rows,
    }


def build_history(days: int = 180) -> list[dict]:
    fx_hist = usdkrw_history(days)
    fx_dates_sorted = sorted(fx_hist.keys())

    def fx_on(date_str: str) -> float | None:
        # 해당 날짜 값이 없으면(주말 등) 직전 영업일 값을 쓴다
        if date_str in fx_hist:
            return fx_hist[date_str]
        cands = [d for d in fx_dates_sorted if d <= date_str]
        return fx_hist[cands[-1]] if cands else None

    # BTC 하나만 시계열로 다룬다. 유동성이 가장 크고 왜곡이 적다.
    market, symbol, label = ASSETS[0]
    up_candles = upbit_candles(market, days)  # 내림차순
    bn_klines = binance_klines(symbol, days)  # 오름차순

    bn_by_date = {}
    for k in bn_klines:
        d = datetime.fromtimestamp(k[0] / 1000, timezone.utc).strftime("%Y-%m-%d")
        bn_by_date[d] = float(k[4])  # 종가

    pts = []
    for c in up_candles:
        d = c["candle_date_time_kst"][:10]
        krw = float(c["trade_price"])
        usd = bn_by_date.get(d)
        fx = fx_on(d)
        if usd and fx:
            prem = (krw / fx / usd - 1) * 100
            pts.append({"date": d, "premium_pct": round(prem, 3)})
    pts.sort(key=lambda p: p["date"])
    return pts


def probe():
    print("== Upbit 티커 ==")
    u = upbit_ticker([a[0] for a in ASSETS])
    for m, r in u.items():
        print(f"  {m:10s} trade_price={r.get('trade_price')} signed_change_rate={r.get('signed_change_rate')}")

    print("\n== Binance 티커 ==")
    b = binance_prices([a[1] for a in ASSETS])
    for s, p in b.items():
        print(f"  {s:10s} {p}")

    print("\n== Frankfurter USD/KRW ==")
    print(f"  1 USD = {usdkrw_rate():.2f} KRW")

    print("\n== Upbit 일봉(최근 3개) ==")
    for c in upbit_candles("KRW-BTC", 3):
        print(f"  {c.get('candle_date_time_kst')}  trade_price={c.get('trade_price')}")

    print("\n== Binance 일봉(최근 3개) ==")
    for k in binance_klines("BTCUSDT", 3)[-3:]:
        d = datetime.fromtimestamp(k[0] / 1000, timezone.utc).strftime("%Y-%m-%d")
        print(f"  {d}  close={k[4]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/data")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args()

    if args.probe:
        probe()
        return

    print("1/2 현재 프리미엄 계산…")
    snap = build_snapshot()
    print("2/2 시계열 계산… (Upbit·Binance·Frankfurter 병합, 잠시 걸립니다)")
    hist = build_history(args.days)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "premium.json").write_text(
        json.dumps(snap, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (out / "premium_history.json").write_text(
        json.dumps({"points": hist}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    print(f"\n완료 — 평균 프리미엄 {snap['basket_avg_pct']}% ({snap['basket_grade']}) "
          f"/ USD·KRW {snap['meta']['fx_usdkrw']}")
    for r in snap["assets"]:
        print(f"  {r['asset']:4s} {r['premium_pct']}%")


if __name__ == "__main__":
    main()
