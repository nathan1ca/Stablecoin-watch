#!/usr/bin/env python3
"""
스테이블코인 감시 - 국내 원화마켓 스테이블코인 거래대금

국내 거래소에서 원화로 USDT·USDC 를 사고판 금액(일별)을 거래소 공개 일봉 API 로
모은다. 원화 ↔ 달러 스테이블코인 전환(온·오프램프)의 규모를 보여 주는 지표로,
지갑 라벨에 기대는 온체인 코너 흐름(etl/fetch_flow.py)보다 범위가 넓고 키가
필요 없다. 단, 거래대금은 매수·매도를 합친 회전량이라 순유출입이 아니다.

    python etl/fetch_krw_volume.py --probe
    python etl/fetch_krw_volume.py            # site/data/krw_volume.json

출처(무인증 공개 API, 하루 단위·한국시간 기준 일봉)
  업비트  https://api.upbit.com/v1/candles/days?market=KRW-USDT&count=200
  빗썸    https://api.bithumb.com/v1/candles/days?market=KRW-USDT&count=200
  코인원  https://api.coinone.co.kr/public/v2/chart/KRW/USDT?interval=1d&size=200
  디지털엑스(옛 코빗) https://api.korbit.co.kr/v2/candles?symbol=usdt_krw&interval=1D&limit=200
  고팍스(스트리미)    https://api.gopax.co.kr/trading-pairs/USDT-KRW/candles?interval=1440
  ※ 디지털엑스·고팍스 일봉에는 원화 거래대금이 없어 수량 × 종가로 어림한다. 고팍스 일봉은
    UTC 0시(한국시간 오전 9시) 기준이라 다른 거래소와 하루 경계가 9시간 어긋난다.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.http import get_json  # noqa: E402

KST = timezone(timedelta(hours=9))
ASSETS = ("USDT", "USDC")
EXCHANGES = [
    {"id": "upbit", "name": "업비트", "core": True},
    {"id": "bithumb", "name": "빗썸", "core": True},
    {"id": "coinone", "name": "코인원", "core": True},
    {"id": "digitalx", "name": "디지털엑스"},   # 옛 코빗(2026-08 사명 변경) — 공개 API 는 api.korbit.co.kr
    {"id": "gopax", "name": "고팍스"},          # 운영사 스트리미
]
DAYS = 200
RAW_SAMPLE: dict[str, str] = {}  # --probe 때 응답 형식을 눈으로 확인하려고 앞부분을 남긴다


def _kst_date(ms) -> str:
    return datetime.fromtimestamp(int(float(ms)) / 1000, KST).strftime("%Y-%m-%d")


def parse_digitalx(resp) -> dict[str, dict]:
    """디지털엑스(옛 코빗) v2 일봉. volume 은 코인 수량이다(2026-10-07 확인: 24시간 시세의
    volume 51,163,739 USDT · quoteVolume ₩69,311,143,400 → 비율 1,354 = USDT 가격).
    일봉에는 원화 거래대금 필드가 없어 수량 × 종가로 어림한다(estimated)."""
    rows = resp.get("data") if isinstance(resp, dict) else resp
    out = {}
    for r in rows or []:
        try:
            if isinstance(r, dict):
                ts = r.get("timestamp") or r.get("time") or r.get("t")
                close = float(r.get("close") or r.get("c") or 0)
                quote = r.get("quoteVolume") or r.get("quote_volume") or r.get("value")
                krw = float(quote) if quote is not None else float(r.get("volume") or r.get("v") or 0) * close
                est = quote is None
            else:  # [time, open, high, low, close, volume]
                ts, close, krw, est = r[0], float(r[4]), float(r[5]) * float(r[4]), True
            out[_kst_date(ts)] = {"krw": krw, "close": close, "estimated": est}
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return out


def parse_gopax(rows) -> dict[str, dict]:
    """고팍스 일봉 [시각(ms), 저가, 고가, 시가, 종가, 거래량]. 원화 거래대금 = 거래량 × 종가(어림)."""
    out = {}
    for r in rows or []:
        try:
            if isinstance(r, dict):
                ts, close, vol = r.get("time") or r.get("timestamp"), float(r.get("close")), float(r.get("volume"))
            else:
                ts, close, vol = r[0], float(r[4]), float(r[5])
            out[_kst_date(ts)] = {"krw": vol * close, "close": close, "estimated": True}
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return out


def parse_upbit_style(rows) -> dict[str, dict]:
    """업비트·빗썸 v1 일봉 → {날짜(KST): {krw, close}}"""
    out = {}
    for r in rows or []:
        d = str(r.get("candle_date_time_kst") or "")[:10]
        if len(d) != 10:
            continue
        try:
            out[d] = {"krw": float(r.get("candle_acc_trade_price") or 0), "close": float(r.get("trade_price") or 0)}
        except (TypeError, ValueError):
            continue
    return out


def parse_coinone(resp) -> dict[str, dict]:
    out = {}
    for r in (resp.get("chart") if isinstance(resp, dict) else None) or []:
        try:
            d = datetime.fromtimestamp(int(r["timestamp"]) / 1000, KST).strftime("%Y-%m-%d")
            out[d] = {"krw": float(r.get("quote_volume") or 0), "close": float(r.get("close") or 0)}
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fetch_one(ex: str, asset: str) -> dict[str, dict]:
    if ex == "upbit":
        return parse_upbit_style(get_json(f"https://api.upbit.com/v1/candles/days?market=KRW-{asset}&count={DAYS}", timeout=20))
    if ex == "bithumb":
        return parse_upbit_style(get_json(f"https://api.bithumb.com/v1/candles/days?market=KRW-{asset}&count={DAYS}", timeout=20))
    if ex == "coinone":
        return parse_coinone(get_json(f"https://api.coinone.co.kr/public/v2/chart/KRW/{asset}?interval=1d&size={DAYS}", timeout=20))
    if ex == "digitalx":
        r = get_json(f"https://api.korbit.co.kr/v2/candles?symbol={asset.lower()}_krw&interval=1D&limit={DAYS}", timeout=20)
        RAW_SAMPLE[f"{ex} {asset}"] = json.dumps(r, ensure_ascii=False)[:400]
        return parse_digitalx(r)
    if ex == "gopax":
        end = int(datetime.now(timezone.utc).timestamp() * 1000)
        start = end - DAYS * 86400 * 1000
        r = get_json(f"https://api.gopax.co.kr/trading-pairs/{asset}-KRW/candles?start={start}&end={end}&interval=1440", timeout=20)
        RAW_SAMPLE[f"{ex} {asset}"] = json.dumps(r, ensure_ascii=False)[:400]
        return parse_gopax(r)
    raise ValueError(ex)


def build(raw: dict[tuple[str, str], dict | Exception], today: str | None = None) -> dict:
    """raw: {(거래소, 자산): {날짜: {krw, close}} 또는 예외} → 일별 합계와 요약."""
    today = today or datetime.now(KST).strftime("%Y-%m-%d")
    status = {}
    days: dict[str, dict] = {}
    for (ex, asset), got in raw.items():
        if isinstance(got, Exception) or got is None:
            status.setdefault(ex, {})[asset] = f"실패: {str(got)[:120]}"
            continue
        status.setdefault(ex, {})[asset] = "ok"
        for d, v in got.items():
            row = days.setdefault(d, {"date": d, "total_krw": 0.0, "by": {}})
            row["by"].setdefault(ex, {})[asset] = round(v["krw"])
            row["total_krw"] += v["krw"]
    # 대형 거래소(core)가 다 나온 날만 합계를 믿는다(일부만 있는 앞쪽 날짜는 뺀다).
    # 소형 거래소는 거래가 없는 날 일봉 자체가 빠지기도 해서, 있는 날만 더하고 없으면 0 으로 본다.
    core = {e["id"] for e in EXCHANGES if e.get("core")}
    ok_pairs = {(ex, a) for ex, st in status.items() for a, s in st.items() if s == "ok" and ex in core}
    daily = []
    for d in sorted(days):
        row = days[d]
        have = {(ex, a) for ex, by in row["by"].items() for a in by}
        if ok_pairs and not ok_pairs <= have:
            continue
        row["total_krw"] = round(row["total_krw"])
        row["partial"] = d == today  # 오늘은 아직 하루가 끝나지 않았다
        daily.append(row)

    full = [r for r in daily if not r["partial"]]
    def avg(rows):
        return round(sum(r["total_krw"] for r in rows) / len(rows)) if rows else None
    last30, prev30 = full[-30:], full[-60:-30]
    share = {}
    if last30:
        tot = sum(r["total_krw"] for r in last30) or 1
        for ex in {e for r in last30 for e in r["by"]}:
            share[ex] = round(sum(sum(r["by"].get(ex, {}).values()) for r in last30) / tot * 100, 1)
        asset_share = {a: round(sum(sum(v.get(a, 0) for v in r["by"].values()) for r in last30) / tot * 100, 1)
                       for a in ASSETS}
    else:
        asset_share = {}
    a30, p30 = avg(last30), avg(prev30)
    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "is_sample": False,
            "exchanges": EXCHANGES,
            "assets": list(ASSETS),
            "status": status,
            "source": "업비트·빗썸·코인원·디지털엑스·고팍스 공개 일봉 API",
            "note": "원화마켓 USDT·USDC 거래대금(매수+매도 합계). 원화와 달러 스테이블코인 사이 전환 규모의 지표이며, "
                    "순유출입(국내→해외 송금액)이 아니다. 오늘 값은 진행 중인 하루.",
        },
        "summary": {
            "last_full_date": full[-1]["date"] if full else None,
            "last_full_krw": full[-1]["total_krw"] if full else None,
            "avg30_krw": a30,
            "avg_prev30_krw": p30,
            "chg30_pct": round((a30 / p30 - 1) * 100, 1) if a30 and p30 else None,
            "share30_pct": share,
            "asset_share30_pct": asset_share,
        },
        "daily": daily,
    }


# ── 국내 거래 비중(24시간) ────────────────────────────────────────────
# 종목별로 국내 5개 거래소 원화마켓의 최근 24시간 거래대금을 모아, 같은 종목의
# 전 세계 24시간 거래대금(CoinGecko, 그 종목이 들어간 모든 거래쌍 합계)과 비교한다.
# 양쪽 모두 '지금부터 24시간 전까지'라 기간이 맞는다. 거래소 보유량은 지갑 주소가
# 공개되지 않아 정확히 알 수 없으므로 거래대금으로 대신한다(2026-10-08 네이선 확인).
LISTINGS_PATH = Path(__file__).resolve().parents[1] / "site" / "data" / "listings.json"
SNAPSHOT_PATH = Path(__file__).resolve().parents[1] / "site" / "data" / "snapshot.json"
CG_MARKETS = ("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd"
              "&category=stablecoins&order=market_cap_desc&per_page=250&page=1")


def _num(*vals):
    for v in vals:
        try:
            if v is not None and v != "":
                return float(v)
        except (TypeError, ValueError):
            continue
    return None


def parse_ticker(ex: str, r) -> float | None:
    """거래소 24시간 시세 응답 → 원화 거래대금. 형식이 다르면 None."""
    if ex in ("upbit", "bithumb"):           # [{"acc_trade_price_24h": ...}]
        row = r[0] if isinstance(r, list) and r else r if isinstance(r, dict) else {}
        return _num(row.get("acc_trade_price_24h"))
    if ex == "coinone":                       # {"tickers":[{"quote_volume": "..."}]}
        t = (r or {}).get("tickers") or []
        return _num(t[0].get("quote_volume")) if t else None
    if ex == "digitalx":                      # {"success":true,"data":[{"quoteVolume": "..."}]}
        d = (r or {}).get("data")
        row = d[0] if isinstance(d, list) and d else d if isinstance(d, dict) else {}
        return _num(row.get("quoteVolume"), row.get("quote_volume"))
    if ex == "gopax":                         # {"volume": 코인 수량, "close": 원}
        vol, close = _num((r or {}).get("volume")), _num((r or {}).get("close"))
        return vol * close if vol is not None and close is not None else None
    return None


def fetch_ticker(ex: str, sym: str) -> float | None:
    if ex == "upbit":
        r = get_json(f"https://api.upbit.com/v1/ticker?markets=KRW-{sym}", timeout=20)
    elif ex == "bithumb":
        r = get_json(f"https://api.bithumb.com/v1/ticker?markets=KRW-{sym}", timeout=20)
    elif ex == "coinone":
        r = get_json(f"https://api.coinone.co.kr/public/v2/ticker_new/KRW/{sym}?additional_data=false", timeout=20)
    elif ex == "digitalx":
        r = get_json(f"https://api.korbit.co.kr/v2/tickers?symbol={sym.lower()}_krw", timeout=20)
    elif ex == "gopax":
        r = get_json(f"https://api.gopax.co.kr/trading-pairs/{sym}-KRW/stats", timeout=20)
    else:
        raise ValueError(ex)
    RAW_SAMPLE[f"시세 {ex} {sym}"] = json.dumps(r, ensure_ascii=False)[:300]
    return parse_ticker(ex, r)


def krw_pairs(listings: dict) -> list[tuple[str, str]]:
    """listings.json 에서 원화마켓이 있는 (거래소, 종목) 쌍."""
    out = []
    for sym, a in (listings.get("assets") or {}).items():
        for ex, v in (a.get("exchanges") or {}).items():
            if "KRW" in (v.get("markets") or []):
                out.append((ex, sym.upper()))
    return sorted(out)


def global_volumes(markets: list) -> dict[str, dict]:
    """CoinGecko 스테이블코인 시장 목록 → 심볼별(시가총액 가장 큰 것) 24시간 거래대금."""
    best: dict[str, dict] = {}
    for m in markets or []:
        sym = str(m.get("symbol") or "").upper()
        if not sym or m.get("total_volume") is None:
            continue
        if sym not in best or (m.get("market_cap") or 0) > (best[sym].get("market_cap") or 0):
            best[sym] = m
    return {s: {"usd": float(m["total_volume"]), "cg_id": m.get("id")} for s, m in best.items()}


def build_share(local: dict[tuple[str, str], float | Exception | None], glob: dict[str, dict] | Exception,
                fx_usdkrw: float | None) -> dict:
    """종목별 국내 24시간 원화 거래대금, 달러 환산, 전 세계 대비 비중."""
    status = {}
    assets: dict[str, dict] = {}
    for (ex, sym), v in local.items():
        if isinstance(v, Exception) or v is None:
            status[f"{ex}:{sym}"] = "실패" + (f": {str(v)[:80]}" if isinstance(v, Exception) else " — 응답 형식")
            continue
        status[f"{ex}:{sym}"] = "ok"
        a = assets.setdefault(sym, {"krw_24h": 0.0, "by": {}})
        a["by"][ex] = round(v)
        a["krw_24h"] += v
    gl_ok = not isinstance(glob, Exception)
    for sym, a in assets.items():
        a["krw_24h"] = round(a["krw_24h"])
        a["usd_24h"] = round(a["krw_24h"] / fx_usdkrw) if fx_usdkrw else None
        g = glob.get(sym) if gl_ok else None
        a["global_usd_24h"] = round(g["usd"]) if g else None
        a["cg_id"] = g["cg_id"] if g else None
        a["share_pct"] = (round(a["usd_24h"] / g["usd"] * 100, 3)
                          if g and g["usd"] and a["usd_24h"] is not None else None)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": "최근 24시간(거래소 24시간 시세 기준)",
        "fx_usdkrw": fx_usdkrw,
        "assets": assets,
        "status": status,
        "global_status": "ok" if gl_ok else f"실패: {str(glob)[:120]}",
        "source": "국내: 업비트·빗썸·코인원·디지털엑스·고팍스 24시간 시세 API · 전 세계: CoinGecko(스테이블코인 분류)",
        "note": "전 세계 거래대금은 그 종목이 들어간 모든 거래쌍(예: BTC/USDT 처럼 결제 통화로 쓰인 거래 포함)의 합계라 "
                "USDT·USDC 처럼 결제 통화로 많이 쓰이는 종목은 비중이 작게 나온다. 거래소 보유량이 아니라 거래 회전량이다.",
    }


def collect_share() -> dict:
    try:
        listings = json.loads(LISTINGS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"  listings.json 없음({e}) — 국내 거래 비중 생략", file=sys.stderr)
        return {}
    fx = None
    try:
        snap = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        fx = ((snap.get("watchlist") or {}).get("meta") or {}).get("fx_rates", {}).get("KRW")
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    local = {}
    for ex, sym in krw_pairs(listings):
        try:
            local[(ex, sym)] = fetch_ticker(ex, sym)
        except Exception as e:  # 한 곳 장애가 나머지를 막지 않게
            local[(ex, sym)] = e
    try:
        glob = global_volumes(get_json(CG_MARKETS, timeout=30))
    except Exception as e:
        glob = e
    res = build_share(local, glob, fx)
    for sym, a in sorted(res["assets"].items(), key=lambda kv: -kv[1]["krw_24h"]):
        print(f"  국내 24시간 {sym}: ₩{a['krw_24h']:,.0f} · 전 세계 ${a['global_usd_24h'] or 0:,.0f} · 비중 {a['share_pct']}%")
    bad = [k for k, v in res["status"].items() if v != "ok"]
    if bad:
        print(f"  국내 시세 실패 {len(bad)}건: {', '.join(bad)}", file=sys.stderr)
    return res


def collect() -> dict:
    raw = {}
    for ex in EXCHANGES:
        for a in ASSETS:
            try:
                raw[(ex["id"], a)] = fetch_one(ex["id"], a)
                print(f"  {ex['name']} {a}: {len(raw[(ex['id'], a)])}일")
            except Exception as e:  # 한 곳 장애가 나머지를 막지 않게
                raw[(ex["id"], a)] = e
                print(f"  {ex['name']} {a}: 실패 — {e}", file=sys.stderr)
    return raw


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/data")
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args()
    res = build(collect())
    share = collect_share()
    if share:
        res["share24h"] = share
    s = res["summary"]
    print(f"일수 {len(res['daily'])} · 최근 완결일 {s['last_full_date']} ₩{(s['last_full_krw'] or 0):,.0f} · "
          f"30일 평균 ₩{(s['avg30_krw'] or 0):,.0f} ({s['chg30_pct']}%) · 점유 {s['share30_pct']} · 자산 {s['asset_share30_pct']}")
    if args.probe:
        for k, v in RAW_SAMPLE.items():
            print(f"  [원문 {k}] {v}")
        print("KRWVOL_JSON " + json.dumps(res, ensure_ascii=False, separators=(",", ":")))
        return
    if not res["daily"]:
        print("수집된 일자가 없어 파일을 쓰지 않음", file=sys.stderr)
        sys.exit(1)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "krw_volume.json").write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()
