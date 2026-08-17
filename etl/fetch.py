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
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

# 공유 라이브러리 (표준 라이브러리만)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.config import load_thresholds, thresholds_for_meta  # noqa: E402
from lib.http import UA  # noqa: E402
from lib.metrics import (  # noqa: E402
    composite_risk_score,
    grade_peg as _grade_peg,
    grade_redemption as _grade_redemption,
    hhi,
    median,
    peg_amount,
    peg_currency,
    pct_change,
    worse_grade,
)

BASE = "https://stablecoins.llama.fi"

# CoinGecko 심볼 → id 매핑 (다중 가격 교차검증용, 상위 종목만)
CG_STABLE_IDS = {
    "USDT": "tether",
    "USDC": "usd-coin",
    "DAI": "dai",
    "USDE": "ethena-usde",
    "USDS": "usds",
    "PYUSD": "paypal-usd",
    "FDUSD": "first-digital-usd",
    "TUSD": "true-usd",
    "FRAX": "frax",
    "GUSD": "gemini-dollar",
    "RLUSD": "ripple-usd",
    "EURC": "euro-coin",
}

# 발행사·발행국가 대조표. DefiLlama 응답에는 이 정보가 없어 손으로 채운다.
ISSUERS_PATH = Path(__file__).resolve().parent / "issuers.json"
UNKNOWN_ISSUER = "확인 필요"

# 이자부(가격 누적형) 토큰화 상품 목록. 편집은 이 JSON에서 한다.
YIELD_BEARING_PATH = Path(__file__).resolve().parent / "yield_bearing.json"

# 응답 필드로 이자부 상품을 구분할 수 있는지 먼저 본다.
#
# 2026-08 기준 /stablecoins?includePrices=true 응답에는 구분 필드가 없다.
# USYC·USDY 는 pegType 이 "peggedUSD", pegMechanism 이 "fiat-backed" 로,
# USDT·USDC 와 완전히 같은 값으로 내려온다. 별도 카테고리 플래그도 없다.
# 그래서 실제 구분은 아래 심볼 목록(yield_bearing.json)이 담당한다.
#
# 다만 나중에 필드가 생길 수 있으니, 아래 키가 참으로 오면 목록보다 먼저 믿는다.
# 새 필드를 발견하면 --probe 로 이름을 확인해 여기 추가하는 쪽이 우선이다.
YIELD_BEARING_FIELD_HINTS = ("isYieldBearing", "yieldBearing", "isInterestBearing")

# pegMechanism 이 이런 값으로 오면 그 자체로 이자부 상품 신호다. 현재는 셋 다
# 관측되지 않지만, 위와 같은 이유로 미리 열어 둔다.
YIELD_BEARING_MECHANISMS = ("yield-bearing", "interest-bearing", "rwa-yield")

# 종목별 시계열을 따로 받아올 개수 (발행잔액 상위). 화면 드롭다운 항목 수와 같다.
SERIES_ASSET_COUNT = 12

# ── 종목 아이콘 ────────────────────────────────────────────────────────────
# DefiLlama 가 자기 사이트에서 쓰는 아이콘 CDN. 슬러그 하나만 끼워 넣으면 된다.
ICON_CDN = "https://icons.llamao.fi/icons/pegged"
ICON_SIZE = 48

# 슬러그로 쓸 수 있는 응답 필드 후보. 앞에서부터 먼저 채워져 있는 것을 쓴다.
#
# 여기 없는 필드로는 슬러그를 만들지 않는다 — 특히 name/symbol 을 소문자+하이픈으로
# 바꿔 추측하지 않는다. 추측한 URL 은 404 로 끝나면 그나마 다행이고, 우연히 다른
# 종목의 아이콘을 물어오면 화면이 조용히 틀린 그림을 보여준다. 확실한 필드가 없으면
# icon_url 을 빈 문자열로 두고, 화면은 심볼 텍스트만 표시하는 쪽으로 떨어진다.
#
# 후보가 실제로 응답에 있는지, 그 값으로 만든 URL 이 200 을 주는지는
# `python etl/fetch.py --probe` 의 "아이콘 URL 구성 필드 확인" 절이 답한다.
ICON_SLUG_FIELDS = ("slug", "gecko_id")


def icon_slug(asset: dict) -> tuple[str, str]:
    """(슬러그, 근거 필드명). 쓸 수 있는 필드가 없으면 ("", "")."""
    for key in ICON_SLUG_FIELDS:
        v = asset.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().lower(), key
    return "", ""


def icon_url(slug: str) -> str:
    """슬러그로 아이콘 URL 을 만든다. 슬러그가 없으면 빈 문자열."""
    if not slug:
        return ""
    return f"{ICON_CDN}/{quote(slug, safe='')}?w={ICON_SIZE}&h={ICON_SIZE}"

# ── 감독 임계치 ────────────────────────────────────────────────────────────
# 법정 기준이 아니라 이 대시보드의 편의상 설정값이다.
# 정본은 etl/thresholds.json — 여기서는 기동 시 한 번 읽어 모듈 전역으로 둔다.
THRESHOLDS = load_thresholds()


# ── HTTP ──────────────────────────────────────────────────────────────────
def get(path: str, params: dict | None = None, retries: int = 3):
    url = BASE + path
    if params:
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


def fetch_coingecko_prices(symbols: list[str]) -> dict[str, float]:
    """심볼 → USD 가격. 실패해도 빈 dict — 교차검증은 보조 신호일 뿐."""
    ids = [CG_STABLE_IDS[s] for s in symbols if s in CG_STABLE_IDS]
    if not ids:
        return {}
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?" + urlencode({
            "ids": ",".join(ids), "vs_currencies": "usd",
        })
        req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        with urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        inv = {v: k for k, v in CG_STABLE_IDS.items()}
        out: dict[str, float] = {}
        for cid, box in data.items():
            sym = inv.get(cid)
            if sym and isinstance(box, dict) and "usd" in box:
                out[sym] = float(box["usd"])
        return out
    except Exception as e:
        print(f"  CoinGecko 교차가격 수집 실패({e}) — DefiLlama 단독으로 진행", file=sys.stderr)
        return {}


# ── 발행사 대조표 ─────────────────────────────────────────────────────────
def load_issuers(path: Path | str | None = None) -> dict:
    """etl/issuers.json 을 {심볼(대문자): {issuer, country, note}} 로 읽는다.

    파일이 없거나 깨져 있어도 수집 자체는 계속한다. 이 경우 모든 종목의
    발행사·발행국가가 '확인 필요'로 표시된다 — 틀린 값을 채우는 것보다 낫다.
    """
    p = Path(path) if path else ISSUERS_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"  issuers.json 없음 ({p}) — 발행사·발행국가는 '{UNKNOWN_ISSUER}'", file=sys.stderr)
        return {}
    except (json.JSONDecodeError, OSError) as e:
        print(f"  issuers.json 읽기 실패 ({e}) — 발행사·발행국가는 '{UNKNOWN_ISSUER}'", file=sys.stderr)
        return {}

    table = raw.get("issuers") if isinstance(raw, dict) and isinstance(raw.get("issuers"), dict) else raw
    if not isinstance(table, dict):
        return {}
    return {
        str(k).strip().upper(): v
        for k, v in table.items()
        if isinstance(v, dict) and not str(k).startswith("_")
    }


def issuer_fields(symbol: str | None, table: dict) -> dict:
    """심볼 하나에 대한 발행사 필드. 미등재 종목은 빈 칸이 아니라 '확인 필요'."""
    entry = table.get(str(symbol or "").strip().upper()) or {}

    def val(key: str) -> str:
        v = entry.get(key)
        return v.strip() if isinstance(v, str) and v.strip() else UNKNOWN_ISSUER

    note = entry.get("note")
    return {
        "issuer": val("issuer"),
        "issuer_country": val("country"),
        "issuer_note": note.strip() if isinstance(note, str) and note.strip() else "",
    }


# ── 이자부(가격 누적형) 상품 판별 ─────────────────────────────────────────
def load_yield_bearing(path: Path | str | None = None) -> dict:
    """etl/yield_bearing.json 을 {심볼(대문자): {name, kind, note}} 로 읽는다.

    파일이 없거나 깨져 있으면 빈 표를 돌려주고 수집은 계속한다. 이 경우
    USYC·USDY 같은 종목이 다시 '페그 이탈'로 잡히므로 경고를 남긴다.
    """
    p = Path(path) if path else YIELD_BEARING_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"  yield_bearing.json 없음 ({p}) — 이자부 상품이 페그 이탈로 잡힐 수 있음",
              file=sys.stderr)
        return {}
    except (json.JSONDecodeError, OSError) as e:
        print(f"  yield_bearing.json 읽기 실패 ({e}) — 이자부 상품이 페그 이탈로 잡힐 수 있음",
              file=sys.stderr)
        return {}

    table = raw.get("yield_bearing") if isinstance(raw, dict) else None
    if not isinstance(table, dict):
        table = raw if isinstance(raw, dict) else {}
    return {
        str(k).strip().upper(): (v if isinstance(v, dict) else {})
        for k, v in table.items()
        if not str(k).startswith("_")
    }


def yield_bearing_reason(asset: dict, table: dict) -> str | None:
    """이자부 상품이면 판별 근거('field:...' 또는 'symbol')를, 아니면 None.

    응답 필드를 먼저 보고, 필드로 구분되지 않을 때만 심볼 목록으로 떨어진다.
    """
    for key in YIELD_BEARING_FIELD_HINTS:
        if asset.get(key):
            return f"field:{key}"

    mech = str(asset.get("pegMechanism") or "").strip().lower()
    if mech in YIELD_BEARING_MECHANISMS:
        return "field:pegMechanism"

    if str(asset.get("symbol") or "").strip().upper() in table:
        return "symbol"
    return None


def grade_peg(dev_bp: float | None) -> str:
    return _grade_peg(dev_bp, THRESHOLDS)


def grade_redemption(chg_30d: float | None) -> str:
    return _grade_redemption(chg_30d, THRESHOLDS)


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


def fetch_asset_history(asset_id: str, days: int = 400) -> list[dict]:
    """종목 하나의 전체 체인 합산 시계열.

    /stablecoincharts/all 에 stablecoin={id} 를 붙이면 그 종목만 걸러서 준다.
    응답 모양은 시장 전체 시계열과 같다(date + totalCirculating*).
    """
    raw = get("/stablecoincharts/all", {"stablecoin": str(asset_id)})
    if not isinstance(raw, list):
        return []
    return raw[-days:]


# ── 가공 ──────────────────────────────────────────────────────────────────
MECHANISM_KO = {
    "fiat-backed": "법정화폐 담보",
    "crypto-backed": "가상자산 담보",
    "algorithmic": "알고리즘형",
}


def build_snapshot(assets: list[dict], chains: list[dict], issuers: dict | None = None,
                   yield_bearing: dict | None = None,
                   external_prices: dict[str, float] | None = None) -> dict:
    issuers = load_issuers() if issuers is None else issuers
    yield_bearing = load_yield_bearing() if yield_bearing is None else yield_bearing
    external_prices = external_prices or {}
    rows = []
    for a in assets:
        circ_box = a.get("circulating")
        circ = peg_amount(circ_box)
        if circ <= 0:
            continue

        price = a.get("price")
        price = float(price) if isinstance(price, (int, float)) else None
        cur = peg_currency(circ_box)
        symbol = str(a.get("symbol") or "").strip().upper()

        # 다중 소스 교차검증: DefiLlama + CoinGecko 중위값으로 페그 편차 계산
        # 소스 간 편차가 크면 price_quality=degraded
        prices_for_med: list[float] = []
        if price is not None:
            prices_for_med.append(price)
        cg = external_prices.get(symbol)
        if cg is not None:
            prices_for_med.append(cg)
        median_price = median(prices_for_med) if prices_for_med else None
        price_spread_bp = None
        price_quality = "ok"
        if len(prices_for_med) >= 2 and median_price:
            price_spread_bp = round((max(prices_for_med) - min(prices_for_med)) * 10_000, 1)
            if price_spread_bp >= THRESHOLDS["source_disagreement_bp"]:
                price_quality = "degraded"
        # 페그 판정에는 중위값을 우선 사용 (단일 소스 이상치 완화)
        peg_price = median_price if median_price is not None else price

        # 비USD 페그는 발행잔액을 가격으로 환산해 USD 기준으로 비교
        mcap_usd = circ * (price or 1.0) if price else circ

        prev_d = peg_amount(a.get("circulatingPrevDay"))
        prev_w = peg_amount(a.get("circulatingPrevWeek"))
        prev_m = peg_amount(a.get("circulatingPrevMonth"))

        # 이자부(가격 누적형) 상품인가. DefiLlama 는 이런 상품도 peggedUSD 로
        # 함께 내려주지만, 목표가가 $1이 아니라 시간이 지날수록 오르는 NAV다.
        yb_reason = yield_bearing_reason(a, yield_bearing)
        is_yb = yb_reason is not None

        # 페그 편차: 페그 목표가는 해당 통화 1단위. price는 USD 표시가이므로
        # USD 페그만 1.0 대비 편차가 곧바로 의미를 갖는다.
        # 이자부 상품은 목표가 자체가 $1이 아니므로 편차를 재지 않는다 — 재면
        # 정상적인 이자 누적이 +1300bp 대의 '페그 이탈'로 잘못 잡힌다.
        dev_bp = None
        if peg_price is not None and cur == "USD" and not is_yb:
            dev_bp = round((peg_price - 1.0) * 10_000, 2)

        chg_30d = pct_change(circ, prev_m)
        peg_g = grade_peg(dev_bp)
        red_g = grade_redemption(chg_30d)
        overall = worse_grade(peg_g, red_g)
        # 가격 품질 저하만으로 breach 로 올리지는 않는다 — 관측 신뢰도 신호.
        if price_quality == "degraded" and overall == "sound":
            overall = "watch"

        # 체인별 분포
        chain_circ = {}
        for ch, box in (a.get("chainCirculating") or {}).items():
            v = peg_amount((box or {}).get("current"))
            if v > 0:
                chain_circ[ch] = v
        top_chains = sorted(chain_circ.items(), key=lambda x: -x[1])[:6]

        mech = a.get("pegMechanism") or "unknown"
        slug, slug_field = icon_slug(a)
        rows.append({
            "id": str(a.get("id", "")),
            "name": a.get("name"),
            "symbol": a.get("symbol"),
            # 슬러그를 만들 수 있는 필드가 없으면 빈 문자열이다. 화면은 빈 값을 보면
            # 아이콘을 아예 그리지 않고 심볼 텍스트만 남긴다.
            "icon_url": icon_url(slug),
            "icon_slug_basis": slug_field,
            **issuer_fields(a.get("symbol"), issuers),
            "peg_currency": cur,
            "mechanism": mech,
            "mechanism_ko": MECHANISM_KO.get(mech, mech),
            "yield_bearing": is_yb,
            "yield_bearing_kind": (yield_bearing.get(str(a.get("symbol") or "").upper(), {}).get("kind")
                                   or "이자부 토큰화 상품") if is_yb else None,
            "yield_bearing_basis": yb_reason,
            "circulating": round(circ, 2),
            "mcap_usd": round(mcap_usd, 2),
            "price": price,
            "price_median": round(peg_price, 6) if peg_price is not None else None,
            "price_sources": len(prices_for_med),
            "price_spread_bp": price_spread_bp,
            "price_quality": price_quality,
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

    # 계기판에서 빠진 이자부 상품 — 화면에 "왜 안 보이는지"를 적어 주기 위한 목록
    yb_rows = [
        {
            "symbol": r["symbol"],
            "name": r["name"],
            "kind": r["yield_bearing_kind"],
            "basis": r["yield_bearing_basis"],
            "price": r["price"],
            "mcap_usd": r["mcap_usd"],
        }
        for r in visible if r["yield_bearing"]
    ]

    alerts = [r for r in visible if r["grade"] in ("watch", "breach")]
    alerts.sort(key=lambda r: (-WORST[r["grade"]], -r["mcap_usd"]))

    # 합성 위험점수 입력값
    peg_devs = [abs(r["dev_bp"]) for r in visible if r["dev_bp"] is not None and not r["yield_bearing"]]
    max_abs_dev = max(peg_devs) if peg_devs else None
    reds = [r["chg_30d"] for r in visible if r["chg_30d"] is not None]
    worst_red = min(reds) if reds else None  # 가장 깊은 순소각
    priced = [r for r in visible if r.get("price_sources", 0) >= 1 and not r["yield_bearing"]]
    degraded_n = sum(1 for r in priced if r.get("price_quality") == "degraded")
    degraded_share = (degraded_n / len(priced)) if priced else 0.0

    risk = composite_risk_score(
        max_abs_dev_bp=max_abs_dev,
        worst_redemption_pct=worst_red,
        hhi_issuer=conc["hhi_issuer"],
        algo_share=algo_share,
        price_degraded_share=degraded_share,
        thr=THRESHOLDS,
    )

    # 시스템 등급 = 개별 경보 + 구조 지표 + 합성 위험점수를 함께 본다
    system = "sound"
    if any(r["grade"] == "breach" for r in visible) or risk["grade"] == "breach":
        system = "breach"
    elif (alerts or conc["hhi_issuer"] >= THRESHOLDS["hhi_concentrated"]
          or algo_share >= THRESHOLDS["algo_share_watch"] or risk["grade"] == "watch"):
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
            "thresholds": thresholds_for_meta(THRESHOLDS),
            "asset_count": len(rows),
            "price_basis": "DefiLlama 오라클 + CoinGecko 교차검증(중위값). 소스 간 편차 ≥ "
                           f"{THRESHOLDS['source_disagreement_bp']}bp 이면 price_quality=degraded",
            "price_crosscheck": "CoinGecko simple/price (상위 스테이블코인)",
            "issuer_source": "etl/issuers.json (수기 관리)",
            "issuer_unknown_label": UNKNOWN_ISSUER,
            "yield_bearing_source": "etl/yield_bearing.json (수기 관리 — 응답에 구분 필드 없음)",
            "yield_bearing_note": "이자부 토큰화 상품은 $1 고정이 목표가 아니므로 페그 편차 계산에서 제외한다.",
            "icon_source": ICON_CDN,
            "icon_slug_fields": list(ICON_SLUG_FIELDS),
            "icon_note": "아이콘 슬러그는 응답 필드에서만 가져온다. 필드가 없으면 icon_url 은 "
                         "빈 값이고 화면은 심볼 텍스트만 표시한다(이름을 소문자+하이픈으로 추측하지 않는다).",
            "icon_coverage": f"{sum(1 for r in rows if r['icon_url'])}/{len(rows)}",
            "risk_methodology": "합성점수 = 페그·상환·집중도·알고리즘비중·가격품질 가중평균 (etl/thresholds.json)",
        },
        "totals": {
            "circulating_usd": round(total, 2),
            "net_1d_usd": round(total_1d, 2),
            "breach_count": sum(1 for r in visible if r["grade"] == "breach"),
            "watch_count": sum(1 for r in visible if r["grade"] == "watch"),
            "system_grade": system,
            "risk_score": risk["score"],
            "risk_grade": risk["grade"],
            "price_degraded_count": degraded_n,
        },
        "risk": risk,
        "concentration": conc,
        "yield_bearing": yb_rows,
        "by_mechanism": mech_rows,
        "by_peg_currency": cur_rows,
        "by_chain": chain_rows[:15],
        "alerts": [
            {k: r[k] for k in ("symbol", "name", "grade", "grade_peg", "grade_redemption",
                               "dev_bp", "chg_30d", "mcap_usd", "price_quality", "price_spread_bp")
             if k in r}
            for r in alerts[:12]
        ],
        "assets": visible[:60],
    }


def series_points(raw: list[dict]) -> list[dict]:
    """차트 응답을 {t, v} 시계열로 정규화한다. 시장 전체·종목별 모두 같은 모양이다."""
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
    return pts


def net_30d_series(pts: list[dict]) -> list[dict]:
    """30일 순증감률 시계열 — 상환압력의 추세를 본다."""
    flow = []
    for i, p in enumerate(pts):
        j = i - 30
        if j >= 0 and pts[j]["v"]:
            flow.append({"t": p["t"], "v": round((p["v"] - pts[j]["v"]) / pts[j]["v"] * 100, 3)})
    return flow


def build_history(raw: list[dict], series: list[dict] | None = None) -> dict:
    pts = series_points(raw)
    return {
        "total_circulating": pts,
        "net_30d_pct": net_30d_series(pts),
        # 종목별 시계열. 화면 드롭다운에서 "전체 시장" 다음 항목들로 쓴다.
        "series": series or [],
    }


def fetch_asset_series(rows: list[dict], count: int = SERIES_ASSET_COUNT) -> list[dict]:
    """발행잔액 상위 종목의 시계열을 하나씩 받아온다.

    한 종목이 실패해도 나머지는 그대로 살린다 — 드롭다운에서 그 종목만 빠진다.
    """
    out = []
    for r in rows[:count]:
        aid = r.get("id")
        if not aid:
            continue
        try:
            pts = series_points(fetch_asset_history(aid))
        except RuntimeError as e:
            print(f"    {r['symbol']} 시계열 수집 실패 — 건너뜀 ({e})", file=sys.stderr)
            continue
        if len(pts) < 2:
            print(f"    {r['symbol']} 시계열 데이터 부족 — 건너뜀", file=sys.stderr)
            continue
        out.append({
            "id": str(aid),
            "symbol": r.get("symbol"),
            "name": r.get("name"),
            "total_circulating": pts,
            "net_30d_pct": net_30d_series(pts),
        })
    return out


# ── 진단 모드 ─────────────────────────────────────────────────────────────
def head_status(url: str, timeout: int = 15) -> str:
    """URL 을 HEAD 로 한 번 두드려 본 결과를 사람이 읽을 문자열로."""
    try:
        req = Request(url, headers={"User-Agent": UA}, method="HEAD")
        with urlopen(req, timeout=timeout) as r:
            return f"HTTP {r.status} {r.headers.get('Content-Type', '')}".strip()
    except HTTPError as e:
        return f"HTTP {e.code}"
    except (URLError, TimeoutError, OSError) as e:
        return f"실패 ({e})"


def verify_icons(snap: dict, sample_size: int = 3) -> None:
    """수집 직후, 만든 아이콘 URL 이 정말 그림을 주는지 표본으로 확인한다.

    ICON_SLUG_FIELDS 후보가 슬러그가 아니었다면 URL 은 전부 404 가 된다. 그대로
    내보내면 브라우저가 종목 수만큼 404 를 때리게 되므로(화면상으로는 onerror
    덕에 빈 원으로 보이지만 낭비다), 그럴 때는 icon_url 을 비워서 내보낸다.

    404 가 아닌 실패(연결 실패·시간 초과)는 CDN 일시 장애일 수 있으니 값을
    건드리지 않는다 — 잠깐 죽은 것 때문에 멀쩡한 슬러그를 버리지 않기 위해서다.
    """
    sample = [r for r in snap["assets"] if r["icon_url"]][:sample_size]
    if not sample:
        return

    results = [(r["symbol"], head_status(r["icon_url"])) for r in sample]
    ok = [s for s, st in results if st.startswith("HTTP 2")]
    if ok:
        print(f"       아이콘 확인: 표본 {len(ok)}/{len(results)}종 정상")
        return

    if all(st == "HTTP 404" for _, st in results):
        for r in snap["assets"]:
            r["icon_url"] = ""
        snap["meta"]["icon_coverage"] = f"0/{len(snap['assets'])}"
        snap["meta"]["icon_note"] += (
            " 이번 수집에서는 표본 URL 이 전부 404 라 슬러그 필드가 틀린 것으로 보고 비웠다.")
        print(f"       아이콘 확인: 표본 {len(results)}종이 모두 404 — "
              f"{'/'.join(ICON_SLUG_FIELDS)} 가 슬러그가 아니다. icon_url 을 전부 비웠다. "
              "--probe 로 실제 필드명을 확인하십시오.", file=sys.stderr)
    else:
        detail = ", ".join(f"{s} {st}" for s, st in results)
        print(f"       아이콘 확인: 판정 불가({detail}) — CDN 일시 장애로 보고 값은 그대로 둔다.",
              file=sys.stderr)


def probe_icons(assets: list[dict]):
    """아이콘 URL 을 어떤 필드로 만들 수 있는지 응답에서 직접 확인한다.

    1) 슬러그로 쓸 만한 이름의 필드가 응답에 있는지 (있으면 이름을 그대로 보여준다)
    2) 후보 필드가 몇 종에 채워져 있는지
    3) 그 값으로 만든 URL 이 실제로 아이콘을 주는지 (CDN 에 HEAD 한 번)
    """
    print("\n== 아이콘 URL 구성 필드 확인 ==")
    if not assets:
        print("  응답 없음")
        return

    # (1) 이름만 보고도 후보가 되는 필드를 전부 긁는다. 지금 모르는 필드가
    #     추가되어도 여기서 눈에 띈다.
    hint = sorted({
        k for a in assets[:80] for k in a
        if any(s in str(k).lower() for s in ("slug", "gecko", "icon", "logo", "image", "symbol"))
    })
    print("  이름에 slug/gecko/icon/logo/image/symbol 이 들어간 필드: " + (", ".join(hint) or "없음"))

    # (2) 실제로 쓰는 후보 필드가 몇 종에 채워져 있는가
    n = len(assets)
    for k in ICON_SLUG_FIELDS:
        filled = sum(1 for a in assets if isinstance(a.get(k), str) and a.get(k).strip())
        sample = next((a.get(k) for a in assets if isinstance(a.get(k), str) and a.get(k).strip()), None)
        print(f"  {k:10s}: {filled}/{n}종 채워짐" + (f" (예: {sample})" if sample else ""))

    top = sorted(assets, key=lambda a: -peg_amount(a.get("circulating")))[:8]
    print("  상위 8종 슬러그 판정:")
    for a in top:
        slug, field = icon_slug(a)
        print(f"    {str(a.get('symbol')):8s} → " +
              (f"{slug}  (근거 {field})" if slug else "없음 — icon_url 비움"))

    # (3) 만든 URL 이 정말 아이콘을 주는지. 여기서 404 가 나오면 그 필드는
    #     슬러그가 아니라는 뜻이므로 ICON_SLUG_FIELDS 를 고쳐야 한다.
    checked = [a for a in top if icon_slug(a)[0]][:5]
    if not checked:
        print(f"  → 후보 필드가 응답에 없다. icon_url 은 전부 빈 값으로 나가고 "
              f"화면은 심볼 텍스트만 표시한다.")
        return
    print(f"  CDN 응답 확인 ({ICON_CDN}):")
    ok = 0
    for a in checked:
        url = icon_url(icon_slug(a)[0])
        st = head_status(url)
        ok += st.startswith("HTTP 2")
        print(f"    {str(a.get('symbol')):8s} {st}  {url}")
    print(f"  → {ok}/{len(checked)}종 성공"
          + ("" if ok == len(checked) else
             " — 실패한 종목은 화면에서 아이콘 없이 심볼만 표시된다."
             " 전부 실패하면 ICON_SLUG_FIELDS 후보가 슬러그가 아니라는 뜻이다."))


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

    # 종목별 히스토리 엔드포인트가 실제로 있는지 확인한다.
    # /stablecoincharts/all 에 stablecoin={id} 를 붙이면 그 종목만 걸러 준다는 전제.
    print("\n== /stablecoincharts/all?stablecoin={id} 종목별 히스토리 ==")
    if assets:
        top = max(assets, key=lambda a: peg_amount(a.get("circulating")))
        aid = top.get("id")
        print(f"대상: {top.get('symbol')} (id={aid})")
        try:
            h1 = fetch_asset_history(aid)
            print(f"  응답 길이: {len(h1)}")
            if h1:
                print("  마지막 항목: " + json.dumps(h1[-1], ensure_ascii=False)[:400])
                pts = series_points(h1)
                print(f"  정규화 시계열: {len(pts)}점")
                if pts:
                    print(f"  최신 값 ${pts[-1]['v']/1e9:,.2f}B "
                          f"(스냅숏 발행잔액과 비슷하면 종목별 필터가 실제로 먹은 것)")
            else:
                print("  빈 배열 — 종목별 필터가 지원되지 않을 수 있다.")
        except RuntimeError as e:
            print(f"  실패: {e}")

    # 이자부 상품을 응답 필드로 구를 수 있는지 확인한다. USYC·USDY 를 USDC 와
    # 나란히 놓고 필드를 비교하면, 값이 갈리는 필드가 있는지 눈으로 판정된다.
    print("\n== 이자부 상품 구분 필드 확인 ==")
    yb_tbl = load_yield_bearing()
    print(f"수동 목록(yield_bearing.json): {len(yb_tbl)}종 — {', '.join(sorted(yb_tbl)) or '없음'}")
    by_sym = {str(a.get("symbol") or "").upper(): a for a in assets}
    probe_syms = [s for s in ("USDC", "USYC", "USDY") if s in by_sym]
    if probe_syms:
        keys = ("symbol", "pegType", "pegMechanism", "price", *YIELD_BEARING_FIELD_HINTS)
        for s in probe_syms:
            a = by_sym[s]
            vals = " ".join(f"{k}={json.dumps(a.get(k), ensure_ascii=False)}" for k in keys)
            print(f"  {vals}")
        # price 는 당연히 갈리므로 판정에서 뺀다. 나머지 중 값이 갈리는 필드가
        # 있으면 그 필드로 자동 구분이 가능하다는 뜻이다.
        splits = [
            k for k in keys
            if k not in ("symbol", "price")
            and len({json.dumps(by_sym[s].get(k)) for s in probe_syms}) > 1
        ]
        print("  → 값이 갈리는 필드: " + (", ".join(splits) if splits else
              "없음 (구분 필드 부재 — yield_bearing.json 심볼 목록으로 제외한다)"))
        for s in probe_syms:
            r = yield_bearing_reason(by_sym[s], yb_tbl)
            print(f"  판정 {s}: {r or '일반 스테이블코인'}")
    else:
        print("  비교 대상 종목이 응답에 없음")

    probe_icons(assets)

    print("\n== etl/issuers.json 대조표 ==")
    tbl = load_issuers()
    unknown = sum(1 for v in tbl.values() if (v.get("issuer") or UNKNOWN_ISSUER) == UNKNOWN_ISSUER)
    print(f"등재 {len(tbl)}종 (그중 발행사 '확인 필요' {unknown}종)")
    if assets:
        top20 = sorted(assets, key=lambda a: -peg_amount(a.get("circulating")))[:20]
        missing = [a.get("symbol") for a in top20
                   if str(a.get("symbol") or "").upper() not in tbl]
        print("상위 20종 중 미등재: " + (", ".join(m for m in missing if m) or "없음"))


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

    print("1/5 발행 현황 수집…")
    assets = fetch_assets()
    print(f"    {len(assets)}개 종목")

    print("2/5 체인별 집계 수집…")
    chains = fetch_chains()

    print("3/5 시장 전체 시계열 수집…")
    history = fetch_history()

    print("4/5 교차 가격 수집 (CoinGecko)…")
    # 상위 심볼 위주로 교차검증. 실패해도 스냅샷은 계속 만든다.
    top_syms = []
    for a in sorted(assets, key=lambda x: -peg_amount(x.get("circulating")))[:30]:
        s = str(a.get("symbol") or "").upper()
        if s in CG_STABLE_IDS:
            top_syms.append(s)
    cg_prices = fetch_coingecko_prices(top_syms)
    print(f"    CoinGecko {len(cg_prices)}/{len(top_syms)}종 확보")

    issuers = load_issuers()
    yb = load_yield_bearing()
    snap = build_snapshot(assets, chains, issuers, yb, external_prices=cg_prices)
    # 슬러그 필드를 잘못 골랐으면 여기서 걸러진다. 표본이 전부 404 면 비우고 간다.
    verify_icons(snap)

    print(f"5/5 종목별 시계열 수집… (상위 {SERIES_ASSET_COUNT}종)")
    series = fetch_asset_series(snap["assets"])
    print(f"    {len(series)}종 확보")

    hist = build_history(history, series)

    (out / "snapshot.json").write_text(
        json.dumps(snap, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (out / "history.json").write_text(
        json.dumps(hist, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    t = snap["totals"]
    print(f"\n완료 — 총 발행잔액 ${t['circulating_usd']/1e9:,.1f}B / "
          f"시스템 등급 {t['system_grade']} / 경보 {t['breach_count']}건 주의 {t['watch_count']}건")
    print(f"       위험점수 {t.get('risk_score', '—')} ({t.get('risk_grade', '—')}) "
          f"/ 가격품질 저하 {t.get('price_degraded_count', 0)}종")
    print(f"       HHI(발행사) {snap['concentration']['hhi_issuer']:,.0f}")
    unknown = sum(1 for r in snap["assets"] if r["issuer"] == UNKNOWN_ISSUER)
    print(f"       발행사 대조: {len(snap['assets']) - unknown}/{len(snap['assets'])}종 확인, "
          f"{unknown}종 '{UNKNOWN_ISSUER}' (etl/issuers.json 에 채우면 줄어든다)")
    ybs = snap["yield_bearing"]
    print(f"       페그 편차 제외(이자부 상품): {len(ybs)}종"
          + (f" — {', '.join(r['symbol'] for r in ybs)}" if ybs else ""))
    iconed = sum(1 for r in snap["assets"] if r["icon_url"])
    print(f"       아이콘 URL: {iconed}/{len(snap['assets'])}종"
          + ("" if iconed else
             f" — 슬러그로 쓸 필드({'/'.join(ICON_SLUG_FIELDS)})가 응답에 없다."
             " --probe 로 실제 필드명을 확인하십시오."))


if __name__ == "__main__":
    main()
