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
from lib.fx import local_peg_dev_bp, supported_currencies, usd_fx_rates  # noqa: E402
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

# 시총 하한과 무관하게 항상 보여 줄 감시목록(원화·엔화 스테이블코인 등).
WATCHLIST_PATH = Path(__file__).resolve().parent / "watchlist.json"
FX_LAG_TOLERANCE_BP_DEFAULT = 50
# 이보다 작은 유통량의 가격은 시장이 매긴 값으로 보기 어렵다(호가가 얇거나 가격이
# 갱신되지 않는다). 편차는 보여 주되 등급은 매기지 않는다. watchlist.json 에서 조정.
MIN_RELIABLE_MCAP_USD_DEFAULT = 1_000_000

# 종목별 시계열을 따로 받아올 개수 (발행잔액 상위). 화면 드롭다운 항목 수와 같다.
# 종목별 시계열(발행잔액 차트의 '표시 대상' 목록). 2026-10-08 네이선: "왜 14종만 나오냐" —
# 상위 12종 + 감시목록이었다. 상위 30종 + 국내 원화마켓 상장 종목 + 감시목록으로 넓힌다.
# 종목당 DefiLlama 호출 1회, history.json 은 종목당 약 23KB.
SERIES_ASSET_COUNT = 30
LISTINGS_PATH = Path(__file__).resolve().parents[1] / "site" / "data" / "listings.json"


def krw_listed_symbols(path: Path = LISTINGS_PATH) -> set[str]:
    """국내 원화마켓에 상장된 스테이블코인 심볼(대문자). 파일이 없으면 빈 집합."""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()
    return {str(sym).upper() for sym, a in (d.get("assets") or {}).items()
            if any("KRW" in (v.get("markets") or []) for v in (a.get("exchanges") or {}).values())}

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


# 교차검증 대상 상한 — CoinGecko simple/price 한 번에 넘길 id 수(무료 API 부담 고려).
CROSSCHECK_MAX_IDS = 100
CROSSCHECK_CHUNK = 50


def crosscheck_targets(assets: list[dict], top_n: int = 30,
                       watch_bp: float | None = None,
                       extra_symbols: set[str] | None = None,
                       cap: int = CROSSCHECK_MAX_IDS) -> dict[str, str]:
    """교차 가격을 받을 종목 → {DefiLlama id: CoinGecko id}.

    대상: (1) 발행잔액 상위 top_n, (2) DefiLlama 가격만으로 USD 페그 편차가 주의선
    (watch_bp) 이상인 종목 — 경보·주의로 뜰 종목은 규모와 관계없이 두 번째 가격으로
    확인한다, (3) extra_symbols(국내 원화마켓 상장 등).
    CoinGecko id 는 DefiLlama 응답의 gecko_id 를 쓴다. 심볼로 찾으면 같은 심볼의
    다른 종목(USDA 는 4종 이상)에 남의 가격이 붙는다. gecko_id 가 없으면 상위
    종목에 한해 CG_STABLE_IDS(수기 대조표)로 보충한다.
    """
    watch_bp = THRESHOLDS["peg_watch_bp"] if watch_bp is None else watch_bp
    extra = {s.upper() for s in (extra_symbols or set())}
    ranked = sorted(assets, key=lambda x: -peg_amount(x.get("circulating")))
    out: dict[str, str] = {}
    used: set[str] = set()
    for rank, a in enumerate(ranked):
        if len(out) >= cap:
            break
        aid = str(a.get("id") or "")
        if not aid or peg_amount(a.get("circulating")) <= 0:
            continue
        sym = str(a.get("symbol") or "").strip().upper()
        price = a.get("price")
        off_peg = (isinstance(price, (int, float)) and peg_currency(a.get("circulating")) == "USD"
                   and abs(float(price) - 1.0) * 10_000 >= watch_bp)
        if not (rank < top_n or off_peg or sym in extra):
            continue
        gid = str(a.get("gecko_id") or "").strip()
        if not gid and rank < top_n:
            gid = CG_STABLE_IDS.get(sym, "")
        if gid and gid not in used:
            out[aid] = gid
            used.add(gid)
    return out


def fetch_coingecko_prices_by_id(targets: dict[str, str]) -> dict[str, float]:
    """{DefiLlama id: CoinGecko id} → {DefiLlama id: USD 가격}. 실패해도 빈 dict."""
    if not targets:
        return {}
    by_gid: dict[str, str] = {g: aid for aid, g in targets.items()}
    gids = list(by_gid)
    out: dict[str, float] = {}
    for i in range(0, len(gids), CROSSCHECK_CHUNK):
        chunk = gids[i:i + CROSSCHECK_CHUNK]
        try:
            url = "https://api.coingecko.com/api/v3/simple/price?" + urlencode({
                "ids": ",".join(chunk), "vs_currencies": "usd",
            })
            req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print(f"  CoinGecko 교차가격 수집 실패({e}) — 이 묶음은 DefiLlama 단독", file=sys.stderr)
            continue
        for gid, box in (data or {}).items():
            aid = by_gid.get(gid)
            if aid and isinstance(box, dict) and isinstance(box.get("usd"), (int, float)) and box["usd"] > 0:
                out[aid] = float(box["usd"])
        if i + CROSSCHECK_CHUNK < len(gids):
            time.sleep(2)
    return out


def fetch_coingecko_prices(symbols: list[str]) -> dict[str, float]:
    """심볼 → USD 가격. 실패해도 빈 dict — 교차검증은 보조 신호일 뿐.

    (예전 방식. 지금 수집은 fetch_coingecko_prices_by_id 를 쓴다.)"""
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

    entry = table.get(str(asset.get("symbol") or "").strip().upper())
    if entry is None:
        return None
    # 심볼은 종목끼리 겹친다(USDA 만 해도 여러 종목). 항목에 defillama_id 가
    # 있으면 그 ID 의 종목만 제외해, 같은 심볼의 다른 종목의 실제 이탈을 숨기지 않는다.
    pinned = str((entry or {}).get("defillama_id") or "").strip()
    if pinned:
        return "id" if str(asset.get("id", "")).strip() == pinned else None
    return "symbol"


def usd_value_per_unit(price: float | None, cur: str, fx_rates: dict | None) -> tuple[float, str]:
    """토큰 1개의 USD 가치와 그 근거.

    - 가격이 있으면 가격 그대로 ('price')
    - 가격이 없고 USD 페그면 1달러 ('peg_usd')
    - 가격이 없고 비USD 페그면 1 / 환율 ('fx'), 환율도 없으면 0 ('unpriced')
    """
    if isinstance(price, (int, float)) and price > 0:
        return float(price), "price"
    if cur == "USD":
        return 1.0, "peg_usd"
    rate = (fx_rates or {}).get(cur)
    if rate:
        return 1.0 / rate, "fx"
    return 0.0, "unpriced"


# ── 감시목록 (원화·엔화 등 소형 비달러 스테이블코인) ──────────────────────
def load_watchlist(path: Path | str | None = None) -> dict:
    """etl/watchlist.json 을 읽는다. 없거나 깨져 있으면 빈 설정 — 수집은 계속한다.

    반환: {"entries": {심볼: {...}}, "fx_lag_tolerance_bp": n, "pinned_currencies": [...]}
    """
    p = Path(path) if path else WATCHLIST_PATH
    empty = {"entries": {}, "fx_lag_tolerance_bp": FX_LAG_TOLERANCE_BP_DEFAULT,
             "pinned_currencies": [], "min_reliable_mcap_usd": MIN_RELIABLE_MCAP_USD_DEFAULT}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"  watchlist.json 없음 ({p}) — 감시목록 없이 진행", file=sys.stderr)
        return empty
    except (json.JSONDecodeError, OSError) as e:
        print(f"  watchlist.json 읽기 실패 ({e}) — 감시목록 없이 진행", file=sys.stderr)
        return empty
    if not isinstance(raw, dict):
        return empty

    table = raw.get("watchlist") if isinstance(raw.get("watchlist"), dict) else {}
    entries = {}
    for k, v in table.items():
        if str(k).startswith("_") or not isinstance(v, dict):
            continue
        sym = str(k).strip().upper()
        cur = str(v.get("peg_currency") or "").strip().upper()
        if not cur:
            continue
        syms = v.get("match_symbols") if isinstance(v.get("match_symbols"), list) else [sym]
        entries[sym] = {
            "peg_currency": cur,
            "label": str(v.get("label") or ""),
            "match_symbols": [str(s).strip().upper() for s in syms if str(s).strip()] or [sym],
            "exclude_ids": [str(i) for i in (v.get("exclude_ids") or [])],
            "note": str(v.get("note") or ""),
            "caveat": str(v.get("data_caveat") or ""),
        }
    floor = raw.get("min_reliable_mcap_usd")
    tol = raw.get("fx_lag_tolerance_bp")
    pinned = raw.get("pinned_currencies") if isinstance(raw.get("pinned_currencies"), list) else []
    return {
        "entries": entries,
        "fx_lag_tolerance_bp": float(tol) if isinstance(tol, (int, float)) and tol >= 0
        else FX_LAG_TOLERANCE_BP_DEFAULT,
        "pinned_currencies": [str(c).strip().upper() for c in pinned if str(c).strip()],
        "min_reliable_mcap_usd": float(floor) if isinstance(floor, (int, float)) and floor >= 0
        else MIN_RELIABLE_MCAP_USD_DEFAULT,
    }


def watchlist_currencies(watch: dict | None) -> list[str]:
    """환율을 받아 와야 할 통화 목록 (감시목록 페그 통화 + 고정 표시 통화)."""
    if not watch:
        return []
    curs = {e["peg_currency"] for e in watch.get("entries", {}).values()}
    curs.update(watch.get("pinned_currencies") or [])
    return sorted(c for c in curs if c and c != "USD")


def fx_currencies(assets: list[dict], watch: dict | None) -> list[str]:
    """환율을 받아 올 통화 = 응답에 나온 비USD 페그 통화 + 감시목록 통화 중 고시되는 것.

    가격이 빠진 비USD 종목도 환율로 USD 가치를 매길 수 있게 전 통화를 받는다.
    """
    curs = {peg_currency(a.get("circulating")) for a in assets}
    curs.update(watchlist_currencies(watch))
    curs.discard("USD")
    return supported_currencies(curs)


def grade_peg_local(dev_bp: float | None, thr: dict, tolerance_bp: float) -> str:
    """비달러 페그 등급. USD 임계값에 환율 시차 허용폭을 더한다."""
    widened = dict(thr)
    widened["peg_watch_bp"] = thr["peg_watch_bp"] + tolerance_bp
    widened["peg_breach_bp"] = thr["peg_breach_bp"] + tolerance_bp
    return _grade_peg(dev_bp, widened)


def build_watchlist(rows: list[dict], watch: dict | None, fx_rates: dict | None,
                    fx_date: str | None, thr: dict, visible_ids: set[str],
                    total_usd: float | None = None) -> dict:
    """감시목록 종목을 rows(전 종목, 하한 적용 전)에서 골라 자기 통화 기준으로 다시 잰다.

    rows 는 build_snapshot 이 만든 행이다. 여기서는 새로 계산하는 값(현지 통화
    가격·편차·등급)만 덧붙이고, 발행잔액·점유율은 원래 행 값을 그대로 쓴다.
    그래서 감시목록이 총계·HHI 를 바꿀 길이 없다.
    """
    watch = watch or {}
    entries = watch.get("entries") or {}
    tol = float(watch.get("fx_lag_tolerance_bp", FX_LAG_TOLERANCE_BP_DEFAULT))
    floor = float(watch.get("min_reliable_mcap_usd", MIN_RELIABLE_MCAP_USD_DEFAULT))
    fx_rates = fx_rates or {}
    out_rows: list[dict] = []

    for key, e in entries.items():
        cur = e["peg_currency"]
        hits = [
            r for r in rows
            if str(r.get("symbol") or "").strip().upper() in e["match_symbols"]
            and r.get("peg_currency") == cur
            and str(r.get("id")) not in e["exclude_ids"]
        ]
        if not hits:
            out_rows.append({
                "watch_key": key,
                "symbol": key,
                "name": None,
                "label": e["label"],
                "peg_currency": cur,
                "status": "missing",
                "status_note": f"DefiLlama 응답에서 symbol∈{e['match_symbols']}, "
                               f"pegType=pegged{cur} 인 항목을 찾지 못함",
                "watch_note": e["note"],
                "data_caveat": e.get("caveat", ""),
                "grade": "unknown",
            })
            continue

        rate = fx_rates.get(cur)
        for r in sorted(hits, key=lambda x: -(x.get("mcap_usd") or 0)):
            # 반올림 전 원가격을 쓴다. price_median 은 소수 6자리로 반올림돼 있어
            # 원화(≈$0.0007)에서는 그 반올림만으로 수 bp 오차가 생긴다.
            dev = local_peg_dev_bp(r.get("price") or r.get("price_median"), rate)
            # 유통량이 아주 작으면 가격이 시장에서 매겨진 값인지 믿기 어렵다.
            # 편차 숫자는 그대로 보여 주고 등급만 '미측정'으로 둔다(오경보 방지).
            thin = (r.get("mcap_usd") or 0) < floor
            g_peg = "unknown" if thin else grade_peg_local(dev, thr, tol)
            g_red = r.get("grade_redemption") or "unknown"
            out_rows.append({
                **{k: r.get(k) for k in (
                    "id", "name", "symbol", "icon_url", "issuer", "issuer_country",
                    "issuer_note", "mechanism", "mechanism_ko", "circulating", "mcap_usd",
                    "share", "price", "chg_1d", "chg_7d", "chg_30d", "grade_redemption",
                    "chains", "chain_count", "mcap_basis")},
                "watch_key": key,
                "label": e["label"],
                "peg_currency": cur,
                "status": "ok",
                "status_note": "" if len(hits) == 1 else
                               f"같은 심볼 {len(hits)}개 — exclude_ids 로 정리 필요 여부 확인",
                "watch_note": e["note"],
                "data_caveat": e.get("caveat", ""),
                "price_reliability": "low" if thin else "ok",
                "fx_rate": rate,
                "price_local": round(r["price"] * rate, 6) if r.get("price") and rate else None,
                "dev_bp_local": dev,
                "grade_peg_local": g_peg,
                # 페그를 잴 수 없는데 상환 쪽만 '정상'이면 행 전체를 '정상'으로 보이지
                # 않게 한다(−648bp 옆에 '정상' 배지가 붙는 혼란 방지). 상환 경보는 그대로 올린다.
                "grade": "unknown" if g_peg == "unknown" and g_red in ("sound", "unknown")
                else worse_grade(g_peg, g_red),
                "in_main_list": str(r.get("id")) in visible_ids,
                # 행 점유율은 소수 3자리 반올림이라 소형 종목은 0 이 된다. 여기서는 유효숫자로 다시 낸다.
                "share": float(f"{(r.get('mcap_usd') or 0) / total_usd * 100:.3g}") if total_usd
                else r.get("share"),
            })

    return {
        "meta": {
            "source": "etl/watchlist.json",
            "fx_source": "Frankfurter (https://frankfurter.dev, 중앙은행 기준환율)",
            "fx_date": fx_date,
            "fx_rates": {c: fx_rates[c] for c in sorted(fx_rates)},
            "fx_lag_tolerance_bp": tol,
            "min_reliable_mcap_usd": floor,
            "peg_watch_bp": thr["peg_watch_bp"] + tol,
            "peg_breach_bp": thr["peg_breach_bp"] + tol,
            "pinned_currencies": watch.get("pinned_currencies") or [],
            "note": "자기 통화 기준 편차 = 가격(USD) × 환율(통화/USD) − 1. 등급 임계값은 USD 페그 "
                    "임계값 + 환율 시차 허용폭. 유통액이 min_reliable_mcap_usd 미만이면 가격 신뢰도가 "
                    "낮아 등급을 매기지 않는다. 감시목록은 표시 규칙이며 총계·HHI·경보 건수·"
                    "합성 위험점수에는 영향을 주지 않는다.",
        },
        "rows": out_rows,
    }


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


# 원본 응답의 오타·표기 흔들림. 그대로 두면 '담보 유형별' 막대에 같은 유형이
# 두 줄로 갈라져 나온다(2026-10-07 'crytpo-backed' 1종, 약 $160만 관측).
MECHANISM_ALIASES = {"crytpo-backed": "crypto-backed"}


def normalize_mechanism(raw) -> str:
    m = str(raw or "").strip().lower()
    if not m:
        return "unknown"
    return MECHANISM_ALIASES.get(m, m)


def build_snapshot(assets: list[dict], chains: list[dict], issuers: dict | None = None,
                   yield_bearing: dict | None = None,
                   external_prices: dict[str, float] | None = None,
                   watchlist: dict | None = None,
                   fx_rates: dict[str, float] | None = None,
                   fx_date: str | None = None) -> dict:
    issuers = load_issuers() if issuers is None else issuers
    yield_bearing = load_yield_bearing() if yield_bearing is None else yield_bearing
    external_prices = external_prices or {}
    fx_rates = fx_rates or {}
    rows = []
    # 시장 영향 종목 판정용 규모(액면 기준, 현재·30일 전 중 큰 값). 아래 설명 참고.
    face_base: dict[str, float] = {}
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
        # 교차 가격은 종목 id 로 찾는다(같은 심볼의 다른 종목에 가격이 섞이지 않게).
        # 심볼 키는 예전 호출 방식과의 호환용.
        cg = external_prices.get(str(a.get("id", "")))
        if cg is None:
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

        # 비USD 페그는 발행잔액을 가격으로 환산해 USD 기준으로 비교한다.
        # 가격이 없을 때 USD 페그는 1달러로 보면 되지만, 비USD 페그를 그대로 두면
        # 엔·원 단위 수량이 달러로 둔갑한다(1억 원 → $1억, 약 1,400배 과대).
        # 그래서 환율이 있으면 환율로 나누고, 없으면 금액을 0으로 두고 표시한다.
        usd_per_unit, mcap_basis = usd_value_per_unit(price, cur, fx_rates)
        mcap_usd = circ * usd_per_unit

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

        # 시장 영향 종목 판정은 '시장가 × 현재 수량'이 아니라 '액면가 × max(현재,
        # 30일 전 수량)'으로 한다. 시장가로 재면 붕괴 중인 종목은 가격·수량이 함께
        # 줄어 비중 기준 아래로 떨어지고, 정작 가장 위험한 순간에 시스템 판단에서
        # 빠진다(2022-05 UST 는 며칠 새 $0.3 아래로 떨어졌다).
        if cur == "USD":
            face_unit = 1.0
        elif fx_rates.get(cur):
            face_unit = 1.0 / fx_rates[cur]
        else:
            face_unit = usd_per_unit
        face_base[str(a.get("id", ""))] = max(circ, prev_m or 0.0) * face_unit

        chg_30d = pct_change(circ, prev_m)
        peg_g = grade_peg(dev_bp)
        red_g = grade_redemption(chg_30d)
        overall = worse_grade(peg_g, red_g)
        # 가격 품질 저하만으로 breach 로 올리지는 않는다 — 관측 신뢰도 신호.
        # 이자부 상품은 NAV 가 $1 위에 있어 교차 대상에 들어오지만, CoinGecko 가 소수 둘째 자리로
        # 반올림해 주는 경우가 많아(reUSD 1.106 vs 1.11) 차이만으로 주의를 붙이지 않는다.
        if price_quality == "degraded" and overall == "sound" and not is_yb:
            overall = "watch"

        # 체인별 분포
        chain_circ = {}
        for ch, box in (a.get("chainCirculating") or {}).items():
            v = peg_amount((box or {}).get("current"))
            if v > 0:
                chain_circ[ch] = v
        top_chains = sorted(chain_circ.items(), key=lambda x: -x[1])[:6]

        mech = normalize_mechanism(a.get("pegMechanism"))
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
            "net_30d_usd": round((circ - prev_m) * usd_per_unit, 2) if prev_m else None,
            "mcap_basis": mcap_basis,
            "grade_peg": peg_g,
            "grade_redemption": red_g,
            "grade": overall,
            "chains": [{"chain": c, "amount": round(v, 2)} for c, v in top_chains],
            "chain_count": len(chain_circ),
        })

    rows.sort(key=lambda r: -r["mcap_usd"])
    total = sum(r["mcap_usd"] for r in rows) or 1.0

    # 가격도 환율도 없어 USD 가치를 매기지 못한 종목. 총계에서는 빠지지만
    # (예전에는 수량이 그대로 달러로 잡혔다) 존재 자체는 화면에 남긴다.
    unvalued = sorted(
        ({"symbol": r["symbol"], "name": r["name"], "peg_currency": r["peg_currency"],
          "circulating": r["circulating"]} for r in rows if r["mcap_basis"] == "unpriced"),
        key=lambda x: -x["circulating"])

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

    # 감시목록 — 하한 미만이어도 따로 보여 준다. visible·alerts·risk 는 건드리지 않는다.
    watch_out = build_watchlist(rows, watchlist, fx_rates, fx_date, THRESHOLDS,
                                {str(r["id"]) for r in visible[:60]}, total)

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

    # 시장 영향 종목(전체 발행잔액 대비 비중 ≥ systemic_share_pct). 합성점수의
    # 페그·상환 요소와 시스템 '경보'는 이 종목들만으로 판단한다. 비중이 작은 종목
    # 하나의 이탈이 시스템 전체를 '경보'로 고정하던 문제(2026-10-07 AP USDA
    # $70M·비중 0.02% 가 페그 요소를 100으로 고정)를 막는다. 작은 종목의 경보는
    # 종목 경보(alerts·breach_count)로 그대로 남고 시스템 등급은 '주의'까지 올린다.
    sys_share = float(THRESHOLDS.get("systemic_share_pct", 1.0))
    systemic = [r for r in visible
                if face_base.get(str(r["id"]), r["mcap_usd"]) / total * 100 >= sys_share]

    # 합성 위험점수 입력값 — 어느 종목이 값을 정했는지도 함께 남긴다(화면 설명용).
    peg_cands = [r for r in systemic if r["dev_bp"] is not None and not r["yield_bearing"]]
    peg_driver = max(peg_cands, key=lambda r: abs(r["dev_bp"])) if peg_cands else None
    max_abs_dev = abs(peg_driver["dev_bp"]) if peg_driver else None
    red_cands = [r for r in systemic if r["chg_30d"] is not None]
    red_driver = min(red_cands, key=lambda r: r["chg_30d"]) if red_cands else None
    worst_red = red_driver["chg_30d"] if red_driver else None  # 가장 깊은 순소각
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

    risk["systemic_share_pct"] = sys_share
    risk["systemic_count"] = len(systemic)
    risk["systemic_coverage_pct"] = round(sum(r["share"] for r in systemic), 2)
    risk["systemic_basis"] = "액면가 × max(현재, 30일 전 발행량) ÷ 총 발행잔액"
    risk["drivers"] = {
        "peg": ({"symbol": peg_driver["symbol"], "dev_bp": peg_driver["dev_bp"]} if peg_driver else None),
        "redemption": ({"symbol": red_driver["symbol"], "chg_30d": red_driver["chg_30d"]} if red_driver else None),
    }

    # 시스템 등급 = 시장 영향 종목의 경보 + 구조 지표 + 합성 위험점수를 함께 본다.
    # 비중이 작은 종목의 경보는 '주의'로만 반영한다(종목 경보 자체는 그대로 표시).
    systemic_breach = [r for r in systemic if r["grade"] == "breach"]
    system = "sound"
    if systemic_breach or risk["grade"] == "breach":
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
            "price_basis": "DefiLlama 오라클 + CoinGecko 교차 확인(발행잔액 상위 30종·편차 주의선 밖 종목·"
                           "국내 원화마켓 상장 종목, 두 가격의 중간값). 두 가격이 "
                           f"{THRESHOLDS['source_disagreement_bp']:g}bp 이상 다르면 가격 품질 저하로 표시",
            "price_crosscheck": "CoinGecko simple/price — 발행잔액 상위 30종 + DefiLlama 가격 기준 페그 "
                                f"편차 {THRESHOLDS['peg_watch_bp']:g}bp 이상 종목 + 국내 원화마켓 상장 종목. "
                                "종목 대응은 DefiLlama gecko_id(없으면 수기 대조표). price_sources=1 이면 "
                                "두 번째 가격을 못 구해 교차 확인이 안 된 값",
            "issuer_source": "etl/issuers.json (수기 관리)",
            "issuer_unknown_label": UNKNOWN_ISSUER,
            "yield_bearing_source": "DefiLlama 응답의 yieldBearing 필드 우선, 필드로 안 잡히는 종목은 etl/yield_bearing.json (수기 관리)",
            "yield_bearing_note": "이자부 토큰화 상품은 $1 고정이 목표가 아니므로 페그 편차 계산에서 제외한다.",
            "icon_source": ICON_CDN,
            "icon_slug_fields": list(ICON_SLUG_FIELDS),
            "icon_note": "아이콘 슬러그는 응답 필드에서만 가져온다. 필드가 없으면 icon_url 은 "
                         "빈 값이고 화면은 심볼 텍스트만 표시한다(이름을 소문자+하이픈으로 추측하지 않는다).",
            "icon_coverage": f"{sum(1 for r in rows if r['icon_url'])}/{len(rows)}",
            "risk_methodology": "합성점수 = 페그·상환·집중도·알고리즘비중·가격품질 가중평균 (etl/thresholds.json). "
                                f"페그·상환 요소와 시스템 '경보'는 시장 비중 {sys_share:g}% 이상 종목만으로 판단",
        },
        "totals": {
            "circulating_usd": round(total, 2),
            "net_1d_usd": round(total_1d, 2),
            "breach_count": sum(1 for r in visible if r["grade"] == "breach"),
            "systemic_breach_count": len(systemic_breach),
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
        "watchlist": watch_out,
        "unvalued": {"count": len(unvalued), "top": unvalued[:8],
                     "fx_date": fx_date,
                     "note": "가격과 환율이 모두 없어 USD 환산·총계에서 제외한 비USD 페그 종목"},
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


def probe_watchlist():
    """감시목록 종목이 DefiLlama 응답에 어떤 id·pegType·체인으로 들어 있는지 확인한다.

    읽기 전용이다. 파일을 쓰지 않는다. 같은 통화(JPY·KRW 등)로 페그된 다른
    종목도 함께 보여 줘서, 심볼이 다르게 등재됐거나 구버전 토큰이 섞였는지를
    눈으로 판정할 수 있게 한다.
    """
    watch = load_watchlist()
    curs = watchlist_currencies(watch)
    print("== 감시목록 확인 ==")
    print("설정: " + ", ".join(f"{k}({e['peg_currency']})" for k, e in watch["entries"].items()))
    assets = fetch_assets()
    fx_rates, fx_date = usd_fx_rates(fx_currencies(assets, watch))
    print(f"환율(Frankfurter {fx_date}): {fx_rates}")
    print(f"DefiLlama 항목 수: {len(assets)}")

    wanted_syms = {s for e in watch["entries"].values() for s in e["match_symbols"]}
    for a in assets:
        cur = peg_currency(a.get("circulating"), fallback=str(a.get("pegType") or "").replace("pegged", ""))
        sym = str(a.get("symbol") or "").strip().upper()
        if cur in curs or sym in wanted_syms:
            chains = {c: peg_amount((b or {}).get("current"))
                      for c, b in (a.get("chainCirculating") or {}).items()}
            print(json.dumps({
                "id": a.get("id"), "name": a.get("name"), "symbol": a.get("symbol"),
                "gecko_id": a.get("gecko_id"), "pegType": a.get("pegType"),
                "pegMechanism": a.get("pegMechanism"), "price": a.get("price"),
                "circulating": peg_amount(a.get("circulating")),
                "circulatingPrevMonth": peg_amount(a.get("circulatingPrevMonth")),
                "chains": {c: round(v, 2) for c, v in sorted(chains.items(), key=lambda x: -x[1]) if v > 0},
                "dev_bp_local": local_peg_dev_bp(a.get("price"), fx_rates.get(cur)),
            }, ensure_ascii=False))

    snap = build_snapshot(assets, [], load_issuers(), load_yield_bearing(),
                          watchlist=watch, fx_rates=fx_rates, fx_date=fx_date)
    print("\n== 산출될 감시목록 ==")
    print(json.dumps(snap["watchlist"], ensure_ascii=False, indent=1)[:6000])
    print("\n== 페그 통화별 (해당 통화) ==")
    print([c for c in snap["by_peg_currency"] if c["currency"] in curs])
    print(f"총 발행잔액 ${snap['totals']['circulating_usd']/1e9:,.2f}B · HHI {snap['concentration']['hhi_issuer']}")
    print(f"환산 불가(가격·환율 없음): {snap['unvalued']['count']}종 — "
          + ", ".join(f"{u['symbol']}({u['peg_currency']} {u['circulating']:,.0f})" for u in snap['unvalued']['top']))
    print("상위 25종: " + ", ".join(f"{a['symbol']} ${a['mcap_usd']/1e6:,.0f}M" for a in snap['assets'][:25]))

    # 가격 없는 비USD 페그 — 예전 계산식(가격 없으면 수량을 그대로 USD로 봄)이
    # 총계를 얼마나 부풀렸는지 확인한다.
    print("\n== 가격 없는 비USD 페그 종목 ==")
    n = 0
    for a in assets:
        cur = peg_currency(a.get("circulating"))
        p = a.get("price")
        if cur != "USD" and not (isinstance(p, (int, float)) and p > 0):
            circ = peg_amount(a.get("circulating"))
            if circ > 0:
                n += 1
                print(f"  {a.get('symbol')} ({cur}) 수량 {circ:,.0f} — 예전 식이면 ${circ:,.0f} 로 계상")
    print(f"  → {n}종")


# ── 원천 데이터 이상 점검 ──────────────────────────────────────────────────
# DefiLlama 응답에서 체인 하나가 통째로 빠지면(2026-10-08 22시 UTC 작업 브랜치 시험:
# JPYC 의 Kaia 몫 약 440만 엔이 사라지고 총 발행잔액이 5시간 만에 $315.2B → $303.3B)
# 30일 증감률이 −30~−50%로 계산돼 USD1·USDG 같은 시장 영향 종목이 '상환 경보'가 되고
# 시스템 등급이 '경보'로 뜬다. 시장이 아니라 원천의 문제이므로 직전 값을 유지한다.
SNAPSHOT_PATH = Path(__file__).resolve().parents[1] / "site" / "data" / "snapshot.json"
SOURCE_TOTAL_DROP_PCT = 3.0       # 직전 회차 대비 총 발행잔액 감소 한도(%)
SOURCE_CHAIN_MIN_USD = 200e6      # 종목·체인 몫이 이 금액 이상일 때만 '체인 누락'을 본다
SOURCE_MARKET_CHAIN_MIN_USD = 1e9  # 체인 전체 합계 비교 하한
SOURCE_CHAIN_DROP_PCT = 50.0      # 이만큼 넘게 줄면 누락으로 본다
SOURCE_COMPARE_MAX_HOURS = 48     # 직전 값이 이보다 오래면 비교하지 않는다
# 직전 값을 유지하는 최대 시간(실제 위기를 오래 가리지 않게). 처음 12시간으로 잡았으나 2026-10-08
# 22시~10-09 02시 UTC DefiLlama 체인 누락(Hyperliquid L1·X Layer·Plasma 등)이 4시간 넘게 이어졌고,
# 예약 수집이 약 7시간 간격이라 12시간은 사실상 1~2회차뿐이다 → 36시간(예약 수집 약 5회차).
SOURCE_HOLD_MAX_HOURS = 36


def _parse_iso(s) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def load_prev_snapshot(out: Path | None = None) -> dict | None:
    """직전 스냅숏. 출력 폴더에 없으면(작업 브랜치 시험) 저장소의 site/data 를 본다."""
    for p in ([Path(out) / "snapshot.json"] if out else []) + [SNAPSHOT_PATH]:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        if isinstance(d, dict) and not (d.get("meta") or {}).get("is_sample"):
            return d
    return None


def source_anomalies(prev: dict, snap: dict, raw_assets: list[dict],
                     now: datetime | None = None) -> list[str]:
    """직전 스냅숏 대비 원천 이상 목록(사람이 읽는 문장). 비어 있으면 정상."""
    now = now or datetime.now(timezone.utc)
    pt = _parse_iso((prev.get("meta") or {}).get("generated_at"))
    if pt is None or (now - pt).total_seconds() / 3600 > SOURCE_COMPARE_MAX_HOURS:
        return []
    out: list[str] = []

    p_tot = (prev.get("totals") or {}).get("circulating_usd") or 0
    n_tot = (snap.get("totals") or {}).get("circulating_usd") or 0
    if p_tot > 0:
        chg = (n_tot / p_tot - 1) * 100
        if chg <= -SOURCE_TOTAL_DROP_PCT:
            out.append(f"총 발행잔액 ${p_tot/1e9:,.1f}B → ${n_tot/1e9:,.1f}B ({chg:+.1f}%)")

    raw = {str(a.get("id")): a for a in raw_assets}
    for r in prev.get("assets") or []:
        circ = r.get("circulating") or 0
        usd_unit = (r.get("mcap_usd") or 0) / circ if circ else 0
        a = raw.get(str(r.get("id")))
        # 시장 가격 없이 액면($1)으로 잰 종목은 금액 자체가 불확실하다. 2026-10-08 22:31 UTC
        # 실제 수집: 가격이 없어 $6.8억으로 잡히던 USDX(실제 시세 약 $0.008)의 BSC 몫 변화
        # 하나로 스냅숏 전체가 묶였다 → 이런 종목은 체인 누락 판정에서 뺀다.
        if a is None or usd_unit <= 0 or r.get("mcap_basis") not in (None, "price"):
            continue
        cc = a.get("chainCirculating") or {}
        for c in r.get("chains") or []:
            p_amt = c.get("amount") or 0
            if p_amt * usd_unit < SOURCE_CHAIN_MIN_USD:
                continue
            n_amt = peg_amount((cc.get(c.get("chain")) or {}).get("current"))
            if n_amt < p_amt * (1 - SOURCE_CHAIN_DROP_PCT / 100):
                out.append(f"{r.get('symbol')}: {c.get('chain')} 체인 몫 ${p_amt*usd_unit/1e6:,.0f}M → "
                           f"${n_amt*usd_unit/1e6:,.0f}M ({(n_amt/p_amt-1)*100:+.0f}%)")

    n_ch = {c.get("chain"): c.get("amount") or 0 for c in snap.get("by_chain") or []}
    for c in prev.get("by_chain") or []:
        p_amt = c.get("amount") or 0
        # 이번 회차 체인 목록이 통째로 비었으면(체인 집계 수집 실패) 이 비교는 건너뛴다.
        if p_amt >= SOURCE_MARKET_CHAIN_MIN_USD and n_ch:
            n_amt = n_ch.get(c.get("chain"), 0)
            if n_amt < p_amt * (1 - SOURCE_CHAIN_DROP_PCT / 100):
                out.append(f"체인 합계 {c.get('chain')} ${p_amt/1e9:,.1f}B → ${n_amt/1e9:,.1f}B")
    return out


def hold_decision(prev: dict, reasons: list[str], now: datetime | None = None) -> dict:
    """직전 값을 유지할지. 직전 값이 SOURCE_HOLD_MAX_HOURS 보다 오래됐으면 새 값을 쓴다."""
    now = now or datetime.now(timezone.utc)
    pt = _parse_iso((prev.get("meta") or {}).get("generated_at"))
    age_h = (now - pt).total_seconds() / 3600 if pt else None
    held = age_h is not None and age_h < SOURCE_HOLD_MAX_HOURS
    return {
        "detected_at": now.isoformat(timespec="seconds"),
        "held": held,
        "prev_generated_at": (prev.get("meta") or {}).get("generated_at"),
        "count": len(reasons),
        "reasons": reasons[:8],
        "note": ("원천(DefiLlama) 응답에서 체인 몫이 통째로 빠지는 등 이상이 보여 직전 수집값을 유지했습니다."
                 if held else
                 f"원천 이상이 {SOURCE_HOLD_MAX_HOURS}시간 넘게 이어져 새 값을 게시했습니다. "
                 "발행잔액·30일 증감·등급이 실제보다 나쁘게 나올 수 있습니다."),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/data", help="출력 디렉터리")
    ap.add_argument("--probe", action="store_true", help="원본 스키마만 출력")
    ap.add_argument("--probe-watchlist", action="store_true",
                    help="감시목록(원화·엔화 등) 종목의 DefiLlama 등재 상태만 출력 (파일 쓰기 없음)")
    args = ap.parse_args()

    if args.probe:
        probe()
        return
    if args.probe_watchlist:
        probe_watchlist()
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
    # 상위 30종 + 페그 편차가 주의선 이상인 종목 + 국내 원화마켓 상장 종목.
    # 실패해도 스냅샷은 계속 만든다(그 종목은 DefiLlama 단독, price_sources=1).
    targets = crosscheck_targets(assets, top_n=30, extra_symbols=krw_listed_symbols())
    cg_prices = fetch_coingecko_prices_by_id(targets)
    print(f"    CoinGecko {len(cg_prices)}/{len(targets)}종 확보")
    # 편차가 큰 종목은 두 가격을 나란히 남겨 둔다(작업 브랜치 시험 로그로 확인).
    by_id = {str(a.get("id")): a for a in assets}
    for aid, gid in targets.items():
        a = by_id.get(aid) or {}
        p = a.get("price")
        if isinstance(p, (int, float)) and abs(p - 1.0) * 10_000 >= THRESHOLDS["peg_watch_bp"]:
            cg = cg_prices.get(aid)
            print(f"      {a.get('symbol')}(id {aid}, {gid}): DefiLlama {p:.6f}"
                  + (f" · CoinGecko {cg:.6f} · 차이 {abs(p - cg) * 10_000:.1f}bp" if cg else " · CoinGecko 없음"))

    issuers = load_issuers()
    yb = load_yield_bearing()
    watch = load_watchlist()
    fx_rates, fx_date = usd_fx_rates(fx_currencies(assets, watch))
    if fx_rates:
        print(f"    환율(Frankfurter {fx_date}): "
              + ", ".join(f"USD/{c} {v:,.2f}" for c, v in sorted(fx_rates.items())))
    snap = build_snapshot(assets, chains, issuers, yb, external_prices=cg_prices,
                          watchlist=watch, fx_rates=fx_rates, fx_date=fx_date)
    # 슬러그 필드를 잘못 골랐으면 여기서 걸러진다. 표본이 전부 404 면 비우고 간다.
    verify_icons(snap)

    print(f"5/5 종목별 시계열 수집… (상위 {SERIES_ASSET_COUNT}종 + 국내 원화마켓 상장 + 감시목록)")
    series = fetch_asset_series(snap["assets"])
    # 상위 목록에 없는 국내 상장 종목·감시목록 종목도 드롭다운에서 고를 수 있게 받는다.
    have = {s["id"] for s in series}
    kr = krw_listed_symbols()
    extra = [r for r in snap["assets"][SERIES_ASSET_COUNT:]
             if str(r.get("symbol") or "").upper() in kr and r.get("id") and str(r["id"]) not in have]
    have |= {str(r["id"]) for r in extra}
    extra += [r for r in snap["watchlist"]["rows"]
              if r.get("status") == "ok" and r.get("id") and str(r["id"]) not in have]
    series += fetch_asset_series(extra, count=len(extra))
    print(f"    {len(series)}종 확보")

    hist = build_history(history, series)

    # 원천 이상 점검: 직전 스냅숏(저장소에 커밋된 값)과 비교해 체인 통째 누락·총액 급감이면
    # 직전 값을 유지한다. 실제 위기일 수도 있으므로 유지는 SOURCE_HOLD_MAX_HOURS 까지만.
    prev = load_prev_snapshot(out)
    found = source_anomalies(prev, snap, assets) if prev else []
    if found:
        print("\n원천 데이터 이상 감지 — " + str(len(found)) + "건")
        for r in found[:12]:
            print("  · " + r)
        decision = hold_decision(prev, found)
        if decision["held"]:
            prev.setdefault("meta", {})["source_anomaly"] = decision
            (out / "snapshot.json").write_text(
                json.dumps(prev, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            print(f"  → 직전 스냅숏({prev['meta'].get('generated_at')}) 유지, history.json 도 그대로 둔다.")
            print(f"     이번 회차 값(쓰지 않음): 총 ${snap['totals']['circulating_usd']/1e9:,.1f}B · "
                  f"경보 {snap['totals']['breach_count']} · 위험점수 {snap['totals'].get('risk_score')}")
            return
        snap["meta"]["source_anomaly"] = decision
        print(f"  → 직전 값이 {SOURCE_HOLD_MAX_HOURS}시간보다 오래돼 새 값을 게시하고 이상 표시를 붙인다.")

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
    rk = snap["risk"]
    drv = rk.get("drivers") or {}
    print(f"       시장 영향 종목(비중 ≥ {rk.get('systemic_share_pct')}%): {rk.get('systemic_count')}종 "
          f"(합계 비중 {rk.get('systemic_coverage_pct')}%), 그중 경보 {t.get('systemic_breach_count', 0)}건 "
          f"/ 페그 요소 기준 {(drv.get('peg') or {}).get('symbol', '—')} "
          f"/ 상환 요소 기준 {(drv.get('redemption') or {}).get('symbol', '—')}")
    print("       경보·주의 상위: " + ", ".join(
        f"{a['symbol']}({a['grade']}, {a.get('dev_bp')}bp, 30일 {a.get('chg_30d')}%)"
        for a in snap["alerts"][:6]))
    unknown = sum(1 for r in snap["assets"] if r["issuer"] == UNKNOWN_ISSUER)
    print(f"       발행사 대조: {len(snap['assets']) - unknown}/{len(snap['assets'])}종 확인, "
          f"{unknown}종 '{UNKNOWN_ISSUER}' (etl/issuers.json 에 채우면 줄어든다)")
    ybs = snap["yield_bearing"]
    print(f"       페그 편차 제외(이자부 상품): {len(ybs)}종"
          + (f" — {', '.join(r['symbol'] for r in ybs)}" if ybs else ""))
    for w in snap["watchlist"]["rows"]:
        if w["status"] != "ok":
            print(f"       감시목록 {w['symbol']}: 미수집 — {w['status_note']}")
        else:
            print(f"       감시목록 {w['symbol']} ({w['name']}, id={w['id']}): "
                  f"{w['circulating']:,.0f} {w['peg_currency']} ≈ ${w['mcap_usd']:,.0f}, "
                  f"편차 {w['dev_bp_local'] if w['dev_bp_local'] is not None else '—'}bp "
                  f"({w['grade']})")
    iconed = sum(1 for r in snap["assets"] if r["icon_url"])
    print(f"       아이콘 URL: {iconed}/{len(snap['assets'])}종"
          + ("" if iconed else
             f" — 슬러그로 쓸 필드({'/'.join(ICON_SLUG_FIELDS)})가 응답에 없다."
             " --probe 로 실제 필드명을 확인하십시오."))


if __name__ == "__main__":
    main()
