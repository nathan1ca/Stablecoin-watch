"""임계값·설정 로더. thresholds.json 이 없거나 깨져도 안전한 기본값으로 동작한다."""

from __future__ import annotations

import json
import sys
from pathlib import Path

THRESHOLDS_PATH = Path(__file__).resolve().parent.parent / "thresholds.json"

# thresholds.json 이 없을 때의 폴백. fetch.py 가 예전에 하드코딩하던 값과 동일.
_DEFAULTS = {
    "peg_watch_bp": 25,
    "peg_breach_bp": 100,
    "redemption_watch": -10.0,
    "redemption_breach": -25.0,
    "hhi_concentrated": 2500,
    "algo_share_watch": 5.0,
    "min_mcap_usd": 50_000_000,
    "source_disagreement_bp": 30,
    "premium_watch_pct": 3.0,
    "premium_breach_pct": 7.0,
    "premium_inverted_pct": -1.0,
    "stable_premium_watch_pct": 0.5,
    "stable_premium_breach_pct": 1.5,
    "risk_weight_peg": 0.35,
    "risk_weight_redemption": 0.25,
    "risk_weight_concentration": 0.20,
    "risk_weight_algorithmic": 0.10,
    "risk_weight_price_quality": 0.10,
    "risk_watch": 35,
    "risk_breach": 60,
}


def load_thresholds(path: Path | str | None = None) -> dict:
    """평탄화된 임계값 dict 를 반환한다. 화면 메타·등급 함수가 같은 키를 쓴다."""
    p = Path(path) if path else THRESHOLDS_PATH
    out = dict(_DEFAULTS)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"  thresholds.json 없음 ({p}) — 내장 기본값 사용", file=sys.stderr)
        return out
    except (json.JSONDecodeError, OSError) as e:
        print(f"  thresholds.json 읽기 실패 ({e}) — 내장 기본값 사용", file=sys.stderr)
        return out

    peg = raw.get("peg") or {}
    red = raw.get("redemption") or {}
    conc = raw.get("concentration") or {}
    algo = raw.get("algorithmic") or {}
    disp = raw.get("display") or {}
    pq = raw.get("price_quality") or {}
    prem = raw.get("premium") or {}
    risk = raw.get("risk_score") or {}
    weights = risk.get("weights") or {}

    def f(section: dict, key: str, fallback):
        v = section.get(key)
        return type(fallback)(v) if isinstance(v, (int, float)) else fallback

    out["peg_watch_bp"] = f(peg, "watch_bp", out["peg_watch_bp"])
    out["peg_breach_bp"] = f(peg, "breach_bp", out["peg_breach_bp"])
    out["redemption_watch"] = f(red, "watch_pct", out["redemption_watch"])
    out["redemption_breach"] = f(red, "breach_pct", out["redemption_breach"])
    out["hhi_concentrated"] = f(conc, "hhi_concentrated", out["hhi_concentrated"])
    out["algo_share_watch"] = f(algo, "share_watch_pct", out["algo_share_watch"])
    out["min_mcap_usd"] = f(disp, "min_mcap_usd", out["min_mcap_usd"])
    out["source_disagreement_bp"] = f(pq, "source_disagreement_bp", out["source_disagreement_bp"])
    out["premium_watch_pct"] = f(prem, "watch_pct", out["premium_watch_pct"])
    out["premium_breach_pct"] = f(prem, "breach_pct", out["premium_breach_pct"])
    out["premium_inverted_pct"] = f(prem, "inverted_pct", out["premium_inverted_pct"])
    out["stable_premium_watch_pct"] = f(prem, "stable_watch_pct", out["stable_premium_watch_pct"])
    out["stable_premium_breach_pct"] = f(prem, "stable_breach_pct", out["stable_premium_breach_pct"])
    out["risk_weight_peg"] = f(weights, "peg", out["risk_weight_peg"])
    out["risk_weight_redemption"] = f(weights, "redemption", out["risk_weight_redemption"])
    out["risk_weight_concentration"] = f(weights, "concentration", out["risk_weight_concentration"])
    out["risk_weight_algorithmic"] = f(weights, "algorithmic", out["risk_weight_algorithmic"])
    out["risk_weight_price_quality"] = f(weights, "price_quality", out["risk_weight_price_quality"])
    out["risk_watch"] = f(risk, "watch", out["risk_watch"])
    out["risk_breach"] = f(risk, "breach", out["risk_breach"])
    return out


def thresholds_for_meta(thr: dict) -> dict:
    """snapshot.meta.thresholds 에 넣을 공개용 키 집합(기존 프론트 호환)."""
    return {
        "peg_watch_bp": thr["peg_watch_bp"],
        "peg_breach_bp": thr["peg_breach_bp"],
        "redemption_watch": thr["redemption_watch"],
        "redemption_breach": thr["redemption_breach"],
        "hhi_concentrated": thr["hhi_concentrated"],
        "algo_share_watch": thr["algo_share_watch"],
        "min_mcap_usd": thr["min_mcap_usd"],
        "source_disagreement_bp": thr["source_disagreement_bp"],
        "risk_watch": thr["risk_watch"],
        "risk_breach": thr["risk_breach"],
    }
