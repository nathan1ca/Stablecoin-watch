#!/usr/bin/env python3
"""
스테이블코인 감시 - 김치프리미엄

국경 간 자금흐름을 라벨링된 지갑 없이도 잡을 수 있는 유일한 지표다.
프리미엄 자체가 재정거래 유인의 크기이므로, 실제 순유출입보다 선행하는
경우가 많다. API 키가 전혀 필요 없다.

스테이블코인 페어(USDT·USDC)는 암호화폐 페어보다 임계값이 낮다.
$1 페그에 가깝게 거래되어야 하므로 0.5%만 벌어져도 관측 대상이다.

    python etl/fetch_premium.py --probe   # API 응답 형식 확인 (필수)
    python etl/fetch_premium.py

데이터 출처 (전부 무인증 공개 API):
  - Upbit 시세: https://docs.upbit.com
  - Binance 시세: https://binance-docs.github.io/apidocs/spot/en/
  - USD/KRW: Frankfurter (ECB 등 여러 소스 취합, https://frankfurter.dev)
  - CoinGecko (Binance 지역 차단 시 대체)
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.config import load_thresholds  # noqa: E402
from lib.http import UA  # noqa: E402
from lib.metrics import grade_premium  # noqa: E402

# market(Upbit) : symbol(Binance 또는 None) : 표시 이름 : kind
# kind=stable → 해외 기준가를 1.0으로 둘 수 있음 (USDT/USDC)
# kind=crypto → 해외 CEX 현물 가격과 비교
ASSETS = [
    ("KRW-USDT", "USDTUSDT", "USDT", "stable"),
    ("KRW-USDC", "USDCUSDT", "USDC", "stable"),
    ("KRW-BTC", "BTCUSDT", "BTC", "crypto"),
    ("KRW-ETH", "ETHUSDT", "ETH", "crypto"),
    ("KRW-XRP", "XRPUSDT", "XRP", "crypto"),  # 국내 재정거래에서 역사적으로 가장 많이 쓰인 자산
]

CG_IDS = {
    "BTCUSDT": "bitcoin",
    "ETHUSDT": "ethereum",
    "XRPUSDT": "ripple",
    "USDTUSDT": "tether",
    "USDCUSDT": "usd-coin",
}

THRESHOLDS = load_thresholds()


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


def coingecko_prices(symbols: list[str]) -> dict:
    ids = [CG_IDS[s] for s in symbols if s in CG_IDS]
    if not ids:
        return {}
    url = "https://api.coingecko.com/api/v3/simple/price?" + urlencode({
        "ids": ",".join(ids), "vs_currencies": "usd",
    })
    data = get_json(url)
    out = {}
    for sym in symbols:
        cid = CG_IDS.get(sym)
        if cid and cid in data and "usd" in data[cid]:
            out[sym] = float(data[cid]["usd"])
    return out


def binance_prices(symbols: list[str]) -> tuple[dict, str]:
    """(가격 딕셔너리, 실제 사용한 출처).

    Binance는 미국 소재 IP를 지역 차단한다(HTTP 451). GitHub Actions
    실행 서버가 미국에 있어 여기서 막히므로, 실패하면 CoinGecko로
    자동 전환한다. 스테이블 심볼(USDTUSDT)은 Binance에 없거나 의미 없으므로
    호출 목록에서 제외하고 기준가 1.0을 쓴다.
    """
    # USDTUSDT 는 자기 자신 페어라 Binance에 없거나 무의미 — 호출에서 제외
    query_syms = [s for s in symbols if s not in ("USDTUSDT",)]
    if not query_syms:
        return {}, "peg=1.0"
    sym_param = json.dumps(query_syms, separators=(",", ":"))
    url = "https://api.binance.com/api/v3/ticker/price?" + urlencode({"symbols": sym_param})
    try:
        rows = get_json(url, retries=1)
        return {r["symbol"]: float(r["price"]) for r in rows}, "Binance"
    except RuntimeError as e:
        print(f"  Binance 접속 실패({e}) — CoinGecko로 대체합니다.", file=sys.stderr)
        try:
            return coingecko_prices(query_syms), "CoinGecko(대체)"
        except RuntimeError as e2:
            print(f"  CoinGecko도 실패({e2}) — 스테이블은 페그 1.0, 암호화폐는 결측 처리",
                  file=sys.stderr)
            return {}, "unavailable"


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
    url = "https://api.upbit.com/v1/candles/days?" + urlencode({"market": market, "count": count})
    return get_json(url)


def coingecko_history(symbol: str, days: int) -> dict:
    cid = CG_IDS.get(symbol)
    if not cid:
        return {}
    url = f"https://api.coingecko.com/api/v3/coins/{cid}/market_chart?" + urlencode({
        "vs_currency": "usd", "days": min(days, 365), "interval": "daily",
    })
    data = get_json(url)
    out = {}
    for ts_ms, price in data.get("prices", []):
        d = datetime.fromtimestamp(ts_ms / 1000, timezone.utc).strftime("%Y-%m-%d")
        out[d] = float(price)
    return out


def binance_klines(symbol: str, days: int) -> dict:
    if symbol == "USDTUSDT":
        return {}  # 기준가 1.0 — 히스토리 불필요
    url = "https://api.binance.com/api/v3/klines?" + urlencode({
        "symbol": symbol, "interval": "1d", "limit": min(days, 1000),
    })
    try:
        rows = get_json(url, retries=1)
        out = {}
        for k in rows:
            d = datetime.fromtimestamp(k[0] / 1000, timezone.utc).strftime("%Y-%m-%d")
            out[d] = float(k[4])
        return out
    except RuntimeError as e:
        print(f"  Binance 일봉 조회 실패({e}) — CoinGecko로 대체합니다.", file=sys.stderr)
        return coingecko_history(symbol, days)


def build_snapshot() -> dict:
    markets = [a[0] for a in ASSETS]
    symbols = [a[1] for a in ASSETS]

    upbit = upbit_ticker(markets)
    binance, price_source = binance_prices(symbols)
    fx = usdkrw_rate()

    rows = []
    for market, symbol, label, kind in ASSETS:
        up = upbit.get(market)
        is_stable = kind == "stable"
        # 스테이블: 해외 기준가 = 1.0 (페그 약속). USDC는 Binance 가격이 있으면 교차 사용.
        if is_stable:
            bn = binance.get(symbol)
            if bn is None or label == "USDT":
                bn = 1.0
        else:
            bn = binance.get(symbol)

        if not up or bn is None:
            rows.append({
                "asset": label, "kind": kind, "premium_pct": None, "krw_price": None,
                "usd_equiv": None, "global_usd": bn, "grade": "unknown",
            })
            continue

        krw_price = float(up["trade_price"])
        usd_equiv = krw_price / fx
        premium = (usd_equiv / bn - 1) * 100
        g = grade_premium(premium, THRESHOLDS, stable=is_stable)
        rows.append({
            "asset": label,
            "kind": kind,
            "krw_price": krw_price,
            "global_usd": bn,
            "usd_equiv": round(usd_equiv, 6 if is_stable else 4),
            "premium_pct": round(premium, 3),
            "grade": g,
            "chg_24h_krw_pct": round(float(up.get("signed_change_rate", 0)) * 100, 3),
        })

    stable_rows = [r for r in rows if r.get("kind") == "stable" and r.get("premium_pct") is not None]
    crypto_rows = [r for r in rows if r.get("kind") == "crypto" and r.get("premium_pct") is not None]

    def avg(rs):
        return round(sum(r["premium_pct"] for r in rs) / len(rs), 3) if rs else None

    stable_avg = avg(stable_rows)
    crypto_avg = avg(crypto_rows)
    basket_avg = avg([r for r in rows if r.get("premium_pct") is not None])

    # 스테이블 프리미엄이 시스템 신호에 더 중요 — 재정거래 마찰이 작아 이상 신호가 선명함
    primary_avg = stable_avg if stable_avg is not None else basket_avg
    primary_grade = grade_premium(
        primary_avg, THRESHOLDS, stable=(stable_avg is not None)
    )

    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "is_sample": False,
            "thresholds": {
                "watch_pct": THRESHOLDS["premium_watch_pct"],
                "breach_pct": THRESHOLDS["premium_breach_pct"],
                "inverted_pct": THRESHOLDS["premium_inverted_pct"],
                "stable_watch_pct": THRESHOLDS["stable_premium_watch_pct"],
                "stable_breach_pct": THRESHOLDS["stable_premium_breach_pct"],
            },
            "fx_usdkrw": round(fx, 2),
            "source": f"Upbit · {price_source} · Frankfurter(FX)",
            "note": "스테이블(USDT·USDC) 프리미엄은 해외 기준가 $1 대비. "
                    "임계값은 암호화폐 페어보다 낮음(0.5%/1.5%).",
        },
        "basket_avg_pct": basket_avg,
        "basket_grade": grade_premium(basket_avg, THRESHOLDS, stable=False),
        "stable_avg_pct": stable_avg,
        "stable_grade": grade_premium(stable_avg, THRESHOLDS, stable=True) if stable_avg is not None else "unknown",
        "crypto_avg_pct": crypto_avg,
        "crypto_grade": grade_premium(crypto_avg, THRESHOLDS, stable=False) if crypto_avg is not None else "unknown",
        "primary_avg_pct": primary_avg,
        "primary_grade": primary_grade,
        "assets": rows,
    }


def build_history(days: int = 180) -> list[dict]:
    """BTC 프리미엄 시계열(기존 호환) + USDT 프리미엄 시계열."""
    fx_hist = usdkrw_history(days)
    fx_dates_sorted = sorted(fx_hist.keys())

    def fx_on(date_str: str) -> float | None:
        if date_str in fx_hist:
            return fx_hist[date_str]
        cands = [d for d in fx_dates_sorted if d <= date_str]
        return fx_hist[cands[-1]] if cands else None

    def series_for(market: str, symbol: str, peg_one: bool = False) -> list[dict]:
        up_candles = upbit_candles(market, days)
        bn_by_date = {} if peg_one else binance_klines(symbol, days)
        pts = []
        for c in up_candles:
            d = c["candle_date_time_kst"][:10]
            krw = float(c["trade_price"])
            usd = 1.0 if peg_one else bn_by_date.get(d)
            fx = fx_on(d)
            if usd and fx:
                prem = (krw / fx / usd - 1) * 100
                pts.append({"date": d, "premium_pct": round(prem, 3)})
        pts.sort(key=lambda p: p["date"])
        return pts

    # 기존 호환: points = BTC
    btc_pts = series_for("KRW-BTC", "BTCUSDT", peg_one=False)
    usdt_pts = series_for("KRW-USDT", "USDTUSDT", peg_one=True)
    return {
        "points": btc_pts,
        "points_btc": btc_pts,
        "points_usdt": usdt_pts,
    }


def probe():
    print("== Upbit 티커 ==")
    u = upbit_ticker([a[0] for a in ASSETS])
    for m, r in u.items():
        print(f"  {m:10s} trade_price={r.get('trade_price')} signed_change_rate={r.get('signed_change_rate')}")

    print("\n== Binance 티커 (실패 시 자동으로 CoinGecko 대체) ==")
    b, src = binance_prices([a[1] for a in ASSETS])
    print(f"  실제 사용 출처: {src}")
    for s, p in b.items():
        print(f"  {s:10s} {p}")

    print("\n== Frankfurter USD/KRW ==")
    print(f"  1 USD = {usdkrw_rate():.2f} KRW")

    print("\n== Upbit 일봉(최근 3개, BTC) ==")
    for c in upbit_candles("KRW-BTC", 3):
        print(f"  {c.get('candle_date_time_kst')}  trade_price={c.get('trade_price')}")

    print("\n== 스테이블 프리미엄 샘플 계산 ==")
    snap = build_snapshot()
    print(f"  stable_avg={snap['stable_avg_pct']}% grade={snap['stable_grade']}")
    for r in snap["assets"]:
        print(f"  {r['asset']:5s} kind={r.get('kind')} prem={r.get('premium_pct')} grade={r.get('grade')}")


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
        json.dumps(hist, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    print(f"\n완료 — 스테이블 평균 {snap['stable_avg_pct']}% ({snap['stable_grade']}) "
          f"/ 암호화폐 평균 {snap['crypto_avg_pct']}% ({snap['crypto_grade']}) "
          f"/ USD·KRW {snap['meta']['fx_usdkrw']}")
    for r in snap["assets"]:
        print(f"  {r['asset']:5s} {r.get('premium_pct')}% [{r.get('kind')}] {r.get('grade')}")


if __name__ == "__main__":
    main()
