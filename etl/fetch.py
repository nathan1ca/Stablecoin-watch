#!/usr/bin/env python3
"""
스테이블코인 감시 대시보드 - ETL

DefiLlama 공개 엔드포인트(무인증)에서 스테이블코인 발행 현황을 수집하고
감독 목적 지표를 계산해 site/ 가 읽을 정적 JSON으로 저장한다.

사용:
    python etl/fetch.py                # data/snapshot.json, data/history.json 생성
    python etl/fetch.py --probe        # 원본 응답 스키마만 출력 (필드명 검증용)
    python etl/fetch.py --out ./data   # 출력 경로 지정

출처: DefiLlama (https://defillama.com) — 무료 티어 이용 시 출처 표기 필요.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "https://stablecoins.llama.fi"
UA = "stablecoin-watch/0.1 (public supervisory dashboard; contact: <your-email>)"

# ── 감독 임계치 ────────────────────────────────────────────────────────────
# 법정 기준이 아니라 이 대시보드의 편의상 설정값이다. 근거를 갖고 조정할 것.
THRESHOLDS = {
    "peg_watch_bp": 25,       # 페그 편차 주의 (basis point)
    "peg_breach_bp": 100,     # 페그 편차 경보
    "redemption_watch": -10.0,  # 30일 순소각률(%) 주의
    "redemption_breach": -25.0,  # 30일 순소각률(%) 경보
    "hhi_concentrated": 2500,   # 집중 시장 판단선 (US DOJ/FTC 수평결합지침 준용)
    "algo_share_watch": 5.0,    # 알고리즘형 비중(%) 주의
    "min_mcap_usd": 50_000_000,  # 계기판 표시 최소 발행잔액
}


# ── HTTP ──────────────────────────────────────────────────────────────────
def get(path: str, params: dict | None = None, retries: int = 3):
    url = BASE + path
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            wait = 2 ** attempt
            print(f"  재시도 {attempt+1}/{retries} ({e}) — {wait}s 대기", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"{url} 수집 실패: {last}")


# ── 값 추출 헬퍼 ──────────────────────────────────────────────────────────
# DefiLlama는 금액을 {"peggedUSD": 1234.5} 처럼 페그통화 키로 감싸서 준다.
def peg_amount(box) -> float:
    """{'peggedUSD': n} 형태에서 숫자를 꺼낸다. 스키마가 바뀌어도 죽지 않게."""
    if box is None:
        return 0.0
    if isinstance(box, (int, float)):
        return float(box)
    if isinstance(box, dict):
        for v in box.values():
            if isinstance(v, (int, float)):
                return float(v)
    return 0.0


def peg_currency(box, fallback: str = "USD") -> str:
    if isinstance(box, dict):
        for k in box:
            if k.startswith("pegged"):
                return k.replace("pegged", "") or fallback
    return fallback


def pct_change(now: float, prev: float) -> float | None:
    if not prev:
        return None
    return (now - prev) / prev * 100.0


def hhi(shares_pct: list[float]) -> float:
    """허핀달-허시만 지수. 점유율(%) 리스트를 받아 0~10000 스케일로 반환."""
    return round(sum(s * s for s in shares_pct), 1)


def grade_peg(dev_bp: float | None) -> str:
    if dev_bp is None:
        return "unknown"
    a = abs(dev_bp)
    if a >= THRESHOLDS["peg_breach_bp"]:
        return "breach"
    if a >= THRESHOLDS["peg_watch_bp"]:
        return "watch"
    return "sound"


def grade_redemption(chg_30d: float | None) -> str:
    if chg_30d is None:
        return "unknown"
    if chg_30d <= THRESHOLDS["redemption_breach"]:
        return "breach"
    if chg_30d <= THRESHOLDS["redemption_watch"]:
        return "watch"
    return "sound"


WORST = {"unknown": 0, "sound": 1, "watch": 2, "breach": 3}


# ── 수집 ──────────────────────────────────────────────────────────────────
def fetch_assets() -> list[dict]:
    raw = get("/stablecoins", {"includePrices": "true"})
    if isinstance(raw, dict):
        return raw.get("peggedAssets") or raw.get("peggedAssets".lower()) or []
    return raw or []


def fetch_history(days: int = 400) -> list[dict]:
    raw = get("/stablecoincharts/all")
    if not isinstance(raw, list):
        return []
    return raw[-days:]


def fetch_chains() -> list[dict]:
    raw = get("/stablecoinchains")
    return raw if isinstance(raw, list) else []


# ── 가공 ──────────────────────────────────────────────────────────────────
MECHANISM_KO = {
    "fiat-backed": "법정화폐 담보",
    "crypto-backed": "가상자산 담보",
    "algorithmic": "알고리즘형",
}


def build_snapshot(assets: list[dict], chains: list[dict]) -> dict:
    rows = []
    for a in assets:
        circ_box = a.get("circulating")
        circ = peg_amount(circ_box)
        if circ <= 0:
            continue

        price = a.get("price")
        price = float(price) if isinstance(price, (int, float)) else None
        cur = peg_currency(circ_box)

        # 비USD 페그는 발행잔액을 가격으로 환산해 USD 기준으로 비교
        mcap_usd = circ * price if price else circ

        prev_d = peg_amount(a.get("circulatingPrevDay"))
        prev_w = peg_amount(a.get("circulatingPrevWeek"))
        prev_m = peg_amount(a.get("circulatingPrevMonth"))

        # 페그 편차: 페그 목표가는 해당 통화 1단위. price는 USD 표시가이므로
        # USD 페그만 1.0 대비 편차가 곧바로 의미를 갖는다.
        dev_bp = None
        if price is not None and cur == "USD":
            dev_bp = round((price - 1.0) * 10_000, 2)

        chg_30d = pct_change(circ, prev_m)
        peg_g = grade_peg(dev_bp)
        red_g = grade_redemption(chg_30d)
        overall = max(peg_g, red_g, key=lambda g: WORST[g])

        # 체인별 분포
        chain_circ = {}
        for ch, box in (a.get("chainCirculating") or {}).items():
            v = peg_amount((box or {}).get("current"))
            if v > 0:
                chain_circ[ch] = v
        top_chains = sorted(chain_circ.items(), key=lambda x: -x[1])[:6]

        mech = a.get("pegMechanism") or "unknown"
        rows.append({
            "id": str(a.get("id", "")),
            "name": a.get("name"),
            "symbol": a.get("symbol"),
            "peg_currency": cur,
            "mechanism": mech,
            "mechanism_ko": MECHANISM_KO.get(mech, mech),
            "circulating": round(circ, 2),
            "mcap_usd": round(mcap_usd, 2),
            "price": price,
            "dev_bp": dev_bp,
            "chg_1d": round(pct_change(circ, prev_d), 3) if pct_change(circ, prev_d) is not None else None,
            "chg_7d": round(pct_change(circ, prev_w), 3) if pct_change(circ, prev_w) is not None else None,
            "chg_30d": round(chg_30d, 3) if chg_30d is not None else None,
            "net_30d_usd": round((circ - prev_m) * (price or 1), 2) if prev_m else None,
            "grade_peg": peg_g,
            "grade_redemption": red_g,
            "grade": overall,
            "chains": [{"chain": c, "amount": round(v, 2)} for c, v in top_chains],
            "chain_count": len(chain_circ),
        })

    rows.sort(key=lambda r: -r["mcap_usd"])
    total = sum(r["mcap_usd"] for r in rows) or 1.0

    for r in rows:
        r["share"] = round(r["mcap_usd"] / total * 100, 3)

    # 담보 유형별 집계 — 알고리즘형 비중이 시스템 리스크의 1차 지표
    by_mech: dict[str, float] = {}
    for r in rows:
        by_mech[r["mechanism"]] = by_mech.get(r["mechanism"], 0.0) + r["mcap_usd"]
    mech_rows = [
        {
            "mechanism": m,
            "label": MECHANISM_KO.get(m, m),
            "amount": round(v, 2),
            "share": round(v / total * 100, 3),
        }
        for m, v in sorted(by_mech.items(), key=lambda x: -x[1])
    ]

    # 페그 통화별 — 원화 스테이블 도입 논의 시 비교 기준
    by_cur: dict[str, float] = {}
    for r in rows:
        by_cur[r["peg_currency"]] = by_cur.get(r["peg_currency"], 0.0) + r["mcap_usd"]
    cur_rows = [
        {"currency": c, "amount": round(v, 2), "share": round(v / total * 100, 3)}
        for c, v in sorted(by_cur.items(), key=lambda x: -x[1])
    ]

    # 체인별 집계
    chain_rows = []
    ctotal = 0.0
    for c in chains:
        v = peg_amount(c.get("totalCirculatingUSD"))
        if v <= 0:
            continue
        chain_rows.append({"chain": c.get("name") or c.get("gecko_id"), "amount": round(v, 2)})
        ctotal += v
    chain_rows.sort(key=lambda x: -x["amount"])
    for c in chain_rows:
        c["share"] = round(c["amount"] / (ctotal or 1) * 100, 3)

    algo_share = next((m["share"] for m in mech_rows if m["mechanism"] == "algorithmic"), 0.0)

    conc = {
        "hhi_issuer": hhi([r["share"] for r in rows]),
        "hhi_chain": hhi([c["share"] for c in chain_rows]),
        "top1_share": rows[0]["share"] if rows else 0.0,
        "top3_share": round(sum(r["share"] for r in rows[:3]), 2),
        "algo_share": algo_share,
    }

    visible = [r for r in rows if r["mcap_usd"] >= THRESHOLDS["min_mcap_usd"]]
    alerts = [r for r in visible if r["grade"] in ("watch", "breach")]
    alerts.sort(key=lambda r: (-WORST[r["grade"]], -r["mcap_usd"]))

    # 시스템 등급 = 개별 경보 + 구조 지표를 함께 본다
    system = "sound"
    if any(r["grade"] == "breach" for r in visible):
        system = "breach"
    elif alerts or conc["hhi_issuer"] >= THRESHOLDS["hhi_concentrated"] or algo_share >= THRESHOLDS["algo_share_watch"]:
        system = "watch"

    total_1d = sum(r["mcap_usd"] for r in rows) - sum(
        (r["mcap_usd"] / (1 + (r["chg_1d"] or 0) / 100)) for r in rows if r["chg_1d"] is not None
    )

    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "DefiLlama",
            "source_url": "https://defillama.com/stablecoins",
            "is_sample": False,
            "thresholds": THRESHOLDS,
            "asset_count": len(rows),
        },
        "totals": {
            "circulating_usd": round(total, 2),
            "net_1d_usd": round(total_1d, 2),
            "breach_count": sum(1 for r in visible if r["grade"] == "breach"),
            "watch_count": sum(1 for r in visible if r["grade"] == "watch"),
            "system_grade": system,
        },
        "concentration": conc,
        "by_mechanism": mech_rows,
        "by_peg_currency": cur_rows,
        "by_chain": chain_rows[:15],
        "alerts": [
            {k: r[k] for k in ("symbol", "name", "grade", "grade_peg", "grade_redemption",
                               "dev_bp", "chg_30d", "mcap_usd")}
            for r in alerts[:12]
        ],
        "assets": visible[:60],
    }


def build_history(raw: list[dict]) -> dict:
    pts = []
    for d in raw:
        ts = d.get("date")
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            continue
        total = peg_amount(d.get("totalCirculatingUSD")) or peg_amount(d.get("totalCirculating"))
        if total <= 0:
            continue
        pts.append({"t": ts, "v": round(total, 2)})
    pts.sort(key=lambda p: p["t"])

    # 30일 순증감률 시계열 — 상환압력의 추세를 본다
    flow = []
    for i, p in enumerate(pts):
        j = i - 30
        if j >= 0 and pts[j]["v"]:
            flow.append({"t": p["t"], "v": round((p["v"] - pts[j]["v"]) / pts[j]["v"] * 100, 3)})
    return {"total_circulating": pts, "net_30d_pct": flow}


# ── 진단 모드 ─────────────────────────────────────────────────────────────
def probe():
    print("== /stablecoins 첫 항목 필드 ==")
    assets = fetch_assets()
    print(f"항목 수: {len(assets)}")
    if assets:
        a = assets[0]
        for k, v in a.items():
            s = json.dumps(v, ensure_ascii=False)
            print(f"  {k:24s} : {s[:110]}")
    print("\n== /stablecoincharts/all 마지막 항목 ==")
    h = fetch_history(1)
    print(json.dumps(h[-1] if h else {}, ensure_ascii=False, indent=2)[:900])
    print("\n== /stablecoinchains 첫 항목 ==")
    c = fetch_chains()
    print(json.dumps(c[0] if c else {}, ensure_ascii=False, indent=2)[:600])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/data", help="출력 디렉터리")
    ap.add_argument("--probe", action="store_true", help="원본 스키마만 출력")
    args = ap.parse_args()

    if args.probe:
        probe()
        return

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print("1/3 발행 현황 수집…")
    assets = fetch_assets()
    print(f"    {len(assets)}개 종목")

    print("2/3 체인별 집계 수집…")
    chains = fetch_chains()

    print("3/3 시계열 수집…")
    history = fetch_history()

    snap = build_snapshot(assets, chains)
    hist = build_history(history)

    (out / "snapshot.json").write_text(
        json.dumps(snap, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (out / "history.json").write_text(
        json.dumps(hist, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    t = snap["totals"]
    print(f"\n완료 — 총 발행잔액 ${t['circulating_usd']/1e9:,.1f}B / "
          f"시스템 등급 {t['system_grade']} / 경보 {t['breach_count']}건 주의 {t['watch_count']}건")
    print(f"       HHI(발행사) {snap['concentration']['hhi_issuer']:,.0f}")


if __name__ == "__main__":
    main()
