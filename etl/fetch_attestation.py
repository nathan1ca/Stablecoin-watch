#!/usr/bin/env python3
"""
스테이블코인 감시 - 어테스테이션 시차

발행사가 공표한 준비금 보고서의 기준일(as_of_date)과 지금 이 순간의 온체인
발행잔액 사이에는 항상 간격이 있다. 그 간격이 얼마나 벌어져 있는지, 그리고
보고서 시점의 발행량과 지금 발행량이 얼마나 달라졌는지를 계산한다.

*** 자동 수집이 안 되는 유일한 모듈이다 ***
어테스테이션 보고서는 PDF로만 나오고 발행일을 구조화해서 주는 무료 API가
없다. etl/attestations.json 을 새 보고서가 나올 때마다 손으로 갱신해야
한다. 이 스크립트는 그 파일과 현재 발행잔액(site/data/snapshot.json)을
대조해서 시차만 계산한다.

    python etl/fetch_attestation.py          # site/data/snapshot.json 필요 (먼저 fetch.py 실행)
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path

# 법정 기준이 아니라 임의의 관측선. 월간 공표 주기를 기준으로 여유를 뒀다.
THRESHOLDS = {
    "watch_days": 45,   # 월간 공표 주기(약 30일) + 여유
    "breach_days": 75,  # 한 주기를 통째로 건너뛴 것으로 간주
    "drift_watch_pct": 3.0,
    "drift_breach_pct": 8.0,
}


def grade(days: int, drift_pct: float | None) -> str:
    g_days = "breach" if days >= THRESHOLDS["breach_days"] else \
             "watch" if days >= THRESHOLDS["watch_days"] else "sound"
    if drift_pct is None:
        return g_days
    g_drift = "breach" if abs(drift_pct) >= THRESHOLDS["drift_breach_pct"] else \
              "watch" if abs(drift_pct) >= THRESHOLDS["drift_watch_pct"] else "sound"
    order = {"sound": 0, "watch": 1, "breach": 2}
    return g_days if order[g_days] >= order[g_drift] else g_drift


def load_current_supply(snapshot_path: Path) -> dict[str, float]:
    if not snapshot_path.exists():
        return {}
    snap = json.loads(snapshot_path.read_text(encoding="utf-8"))
    return {a["symbol"]: a["circulating"] for a in snap.get("assets", []) if a.get("circulating")}


def build(entries: list[dict], current_supply: dict[str, float]) -> dict:
    today = datetime.now(timezone.utc).date()
    rows = []
    for e in entries:
        as_of = date.fromisoformat(e["as_of_date"])
        days = (today - as_of).days
        reported = e.get("reported_circulating")
        current = current_supply.get(e["symbol"])
        drift = None
        drift_pct = None
        if reported and current:
            drift = current - reported
            drift_pct = round(drift / reported * 100, 3)
        rows.append({
            "issuer": e["issuer"], "symbol": e["symbol"],
            "as_of_date": e["as_of_date"], "days_since": days,
            "reported_circulating": reported,
            "current_circulating": current,
            "drift": round(drift, 2) if drift is not None else None,
            "drift_pct": drift_pct,
            "source_url": e.get("source_url"),
            "verified": e.get("verified", False),
            "grade": grade(days, drift_pct),
        })
    rows.sort(key=lambda r: -r["days_since"])
    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "is_sample": False, "thresholds": THRESHOLDS,
            "maintenance_note": "이 데이터는 etl/attestations.json 을 손으로 갱신해야 최신 상태가 "
                                "유지됩니다. 자동 수집 API가 없습니다.",
        },
        "entries": rows,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--attestations", default="etl/attestations.json")
    ap.add_argument("--snapshot", default="site/data/snapshot.json")
    ap.add_argument("--out", default="site/data")
    args = ap.parse_args()

    data = json.loads(Path(args.attestations).read_text(encoding="utf-8"))
    current_supply = load_current_supply(Path(args.snapshot))
    if not current_supply:
        print("경고: snapshot.json 을 못 읽었습니다. 먼저 python etl/fetch.py 를 실행하십시오. "
              "시차는 계산되지만 발행잔액 드리프트는 비어 있습니다.")

    out_data = build(data["entries"], current_supply)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "attestation.json").write_text(
        json.dumps(out_data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    print(f"완료 — {len(out_data['entries'])}건")
    for r in out_data["entries"]:
        d = f", 드리프트 {r['drift_pct']}%" if r["drift_pct"] is not None else ""
        print(f"  {r['issuer']:8s} {r['symbol']:5s} {r['as_of_date']} 이후 {r['days_since']}일{d} [{r['grade']}]")


if __name__ == "__main__":
    main()
