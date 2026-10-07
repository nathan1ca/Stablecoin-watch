"""환율 조회와 비달러 페그 편차 계산. 표준 라이브러리만 사용.

환율 출처는 fetch_premium.py 와 같은 Frankfurter(https://frankfurter.dev,
ECB 등 중앙은행 기준환율 취합)다. 키가 필요 없고, 같은 호스트를 쓰므로
수집 환경의 네트워크 허용 목록도 늘지 않는다.

주의: Frankfurter 는 '하루 한 번 고시되는 기준환율'이다(주말·휴일은 직전
영업일 값). 토큰 가격은 실시간이므로, 환율이 고시된 뒤 장중에 움직인 만큼이
편차에 섞여 들어간다. 그래서 비달러 페그의 등급은 USD 페그와 같은 임계값에
'환율 시차 허용폭'을 더해 매긴다(watchlist.json 의 fx_lag_tolerance_bp).
"""

from __future__ import annotations

import sys
from urllib.parse import urlencode

from .http import get_json

FRANKFURTER_LATEST = "https://api.frankfurter.dev/v1/latest"

# Frankfurter(ECB 기준환율)가 고시하는 통화. 목록에 없는 코드를 한 번에 섞어
# 요청하면 요청 전체가 실패할 수 있으므로, 이 집합 안의 통화만 묻는다.
# RUB 는 ECB 가 2022-03 고시를 중단해 없다 → 루블 페그(A7A5 등)는 환산 불가로 남는다.
FRANKFURTER_CODES = frozenset(
    "AUD BGN BRL CAD CHF CNY CZK DKK EUR GBP HKD HUF IDR ILS INR ISK JPY KRW MXN "
    "MYR NOK NZD PHP PLN RON SEK SGD THB TRY ZAR".split()
)

# DefiLlama pegType 이 ISO 코드와 다른 경우 (pegType 'peggedREAL' → BRL)
PEG_TO_ISO = {"REAL": "BRL"}


def iso_currency(peg_cur: str) -> str:
    c = str(peg_cur or "").strip().upper()
    return PEG_TO_ISO.get(c, c)


def supported_currencies(peg_currencies) -> list[str]:
    """환율을 구할 수 있는 페그 통화(DefiLlama 표기 그대로)만 고른다."""
    return sorted({
        str(c).strip().upper() for c in peg_currencies
        if c and iso_currency(c) in FRANKFURTER_CODES
    })


def usd_fx_rates(currencies: list[str]) -> tuple[dict[str, float], str | None]:
    """USD 1달러가 각 통화 몇 단위인지({'JPY': 147.3, ...})와 고시일.

    실패하면 ({}, None). 환율이 없으면 비달러 편차를 '미측정'으로 두면 되므로
    수집 전체를 멈출 이유가 없다.
    """
    # 키는 DefiLlama 표기(REAL 등)로 돌려주고, 요청은 ISO 코드로 한다.
    wanted = {c: iso_currency(c) for c in supported_currencies(currencies) if c != "USD"}
    if not wanted:
        return {}, None
    isos = sorted(set(wanted.values()))
    url = FRANKFURTER_LATEST + "?" + urlencode({"base": "USD", "symbols": ",".join(isos)})
    try:
        r = get_json(url, timeout=20)
    except RuntimeError as e:
        print(f"  Frankfurter 환율 수집 실패({e}) — 비달러 페그 편차는 미측정", file=sys.stderr)
        return {}, None
    rates = r.get("rates") if isinstance(r, dict) else None
    if not isinstance(rates, dict):
        return {}, None
    by_iso = {
        k.upper(): float(v) for k, v in rates.items()
        if isinstance(v, (int, float)) and v > 0
    }
    out = {peg: by_iso[iso] for peg, iso in wanted.items() if iso in by_iso}
    return out, (r.get("date") if isinstance(r.get("date"), str) else None)


def local_peg_dev_bp(price_usd: float | None, units_per_usd: float | None) -> float | None:
    """비달러 페그 토큰의 '자기 통화 기준' 페그 편차(bp).

    가격(USD) × 환율(통화/USD) = 토큰 1개가 그 통화로 얼마인가.
    1엔짜리 약속이면 이 값이 1.0 이어야 하고, 벗어난 만큼이 편차다.

      예) JPYC 가 $0.00675, USD/JPY 148.0 → 0.999 엔 → −10bp
          $1 기준으로 재면 (0.00675 − 1) × 10000 ≈ −9,932bp 라는 터무니없는 값이 나온다.
    """
    if price_usd is None or not units_per_usd or price_usd <= 0:
        return None
    return round((price_usd * units_per_usd - 1.0) * 10_000, 2)
