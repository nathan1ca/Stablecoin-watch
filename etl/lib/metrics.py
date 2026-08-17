"""위험 지표 계산. 단위 테스트 대상."""

from __future__ import annotations

from typing import Any


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


def grade_peg(dev_bp: float | None, thr: dict) -> str:
    if dev_bp is None:
        return "unknown"
    a = abs(dev_bp)
    if a >= thr["peg_breach_bp"]:
        return "breach"
    if a >= thr["peg_watch_bp"]:
        return "watch"
    return "sound"


def grade_redemption(chg_30d: float | None, thr: dict) -> str:
    if chg_30d is None:
        return "unknown"
    if chg_30d <= thr["redemption_breach"]:
        return "breach"
    if chg_30d <= thr["redemption_watch"]:
        return "watch"
    return "sound"


def grade_premium(pct: float | None, thr: dict, *, stable: bool = False) -> str:
    """김치프리미엄 등급. 스테이블코인 페어는 더 낮은 임계값."""
    if pct is None:
        return "unknown"
    inv = thr["premium_inverted_pct"]
    watch = thr["stable_premium_watch_pct"] if stable else thr["premium_watch_pct"]
    breach = thr["stable_premium_breach_pct"] if stable else thr["premium_breach_pct"]
    if pct <= inv:
        return "watch"
    if pct >= breach:
        return "breach"
    if pct >= watch:
        return "watch"
    return "sound"


WORST = {"unknown": 0, "sound": 1, "watch": 2, "breach": 3}


def worse_grade(*grades: str) -> str:
    return max(grades, key=lambda g: WORST.get(g, 0))


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def component_peg_score(dev_bp: float | None, thr: dict) -> float:
    """0~100. 편차 절댓값 기준 선형 스케일 (breach 에서 100)."""
    if dev_bp is None:
        return 0.0
    a = abs(dev_bp)
    breach = float(thr["peg_breach_bp"]) or 100.0
    return _clamp(a / breach * 100.0)


def component_redemption_score(chg_30d: float | None, thr: dict) -> float:
    """0~100. 순소각이 깊을수록 높음. 증가(양수)는 0."""
    if chg_30d is None:
        return 0.0
    if chg_30d >= 0:
        return 0.0
    breach = abs(float(thr["redemption_breach"])) or 25.0
    return _clamp(abs(chg_30d) / breach * 100.0)


def component_concentration_score(hhi_val: float, thr: dict) -> float:
    """0~100. HHI 0→0, 10000→100. 고집중 임계 근처에서 가속."""
    concentrated = float(thr["hhi_concentrated"]) or 2500.0
    if hhi_val <= 0:
        return 0.0
    # 2500에서 약 50, 5000에서 75, 10000에서 100
    if hhi_val < concentrated:
        return _clamp(hhi_val / concentrated * 50.0)
    span = 10000.0 - concentrated
    return _clamp(50.0 + (hhi_val - concentrated) / span * 50.0)


def component_algo_score(algo_share: float, thr: dict) -> float:
    watch = float(thr["algo_share_watch"]) or 5.0
    if algo_share <= 0:
        return 0.0
    # watch% 에서 50, 그 이상 선형
    return _clamp(algo_share / watch * 50.0)


def component_price_quality_score(degraded_share: float) -> float:
    """가격 품질 저하 종목 비중(0~1) → 0~100."""
    return _clamp(degraded_share * 100.0)


def composite_risk_score(
    *,
    max_abs_dev_bp: float | None,
    worst_redemption_pct: float | None,
    hhi_issuer: float,
    algo_share: float,
    price_degraded_share: float,
    thr: dict,
) -> dict[str, Any]:
    """시스템 합성 위험점수.

    Returns:
        {
          score: 0~100,
          grade: sound|watch|breach,
          components: {peg, redemption, concentration, algorithmic, price_quality}
        }
    """
    comps = {
        "peg": round(component_peg_score(max_abs_dev_bp, thr), 1),
        "redemption": round(component_redemption_score(worst_redemption_pct, thr), 1),
        "concentration": round(component_concentration_score(hhi_issuer, thr), 1),
        "algorithmic": round(component_algo_score(algo_share, thr), 1),
        "price_quality": round(component_price_quality_score(price_degraded_share), 1),
    }
    score = (
        comps["peg"] * thr["risk_weight_peg"]
        + comps["redemption"] * thr["risk_weight_redemption"]
        + comps["concentration"] * thr["risk_weight_concentration"]
        + comps["algorithmic"] * thr["risk_weight_algorithmic"]
        + comps["price_quality"] * thr["risk_weight_price_quality"]
    )
    score = round(_clamp(score), 1)
    if score >= thr["risk_breach"]:
        grade = "breach"
    elif score >= thr["risk_watch"]:
        grade = "watch"
    else:
        grade = "sound"
    return {"score": score, "grade": grade, "components": comps}


def median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0
