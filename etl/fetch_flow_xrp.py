#!/usr/bin/env python3
"""
스테이블코인 감시 - 국경 간 온체인 자금흐름 (XRP Ledger 코너)

이더리움 코너(fetch_flow.py)와 성격은 같지만 훨씬 유리한 조건에서 시작한다.
XRP Ledger는 거래소가 이용자별 지갑을 따로 만들지 않는다 — 거래소당
지갑 주소가 보통 하나뿐이고, 이용자 구분은 destination tag라는 부가
숫자로 한다. 그래서 코인원처럼 주소가 5만 개로 쪼개지는 문제 자체가
구조적으로 없다.

주소도 하드코딩하지 않는다. xrpscan.com이 공개하는 무인증 API
(`/api/v1/names/well-known`)에서 실행할 때마다 라벨된 계정 목록을 통째로
받아, 이름이 "Upbit"/"Bithumb"인 것을 한국 쪽으로, "Binance"/"OKX"/"Bybit"인
것을 해외 비교군으로 취급한다. 거래 내역 조회(`/api/v1/account/{addr}
/transactions`) 응답에 상대방 계정의 이름표(AccountName/DestinationName)가
이미 붙어서 나오기 때문에, 그걸로 바로 필터링한다.

*** 그래도 한계는 있다 ***
  - 네이티브 XRP 결제만 본다. RLUSD 같은 발행 통화(issued currency)는
    빠져 있다.
  - "Bithumb Global"처럼 이름이 다른 계열사는 정확히 "Bithumb"으로
    일치하는 것만 잡는다. 느슨하게 잡으면 무관한 계정이 섞일 수 있어
    엄격하게 갔다.
  - xrpscan은 무료 공개 API이지만 대량 사용 시 유료 티어를 권장하는
    안내가 있다. 그래서 1분 단위 폴링에는 쓰지 않는다 — README의
    "실시간 갱신" 절 참고.

    python etl/fetch_flow_xrp.py --probe
    python etl/fetch_flow_xrp.py --days 180
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

BASE = "https://api.xrpscan.com/api/v1"
UA = "stablecoin-watch/0.2"
PAUSE = 0.35  # 무료 공개 API 예의상 텀

KR_NAMES = {"Upbit", "Bithumb"}
GLOBAL_NAMES = {"Binance", "OKX", "Bybit"}
MAX_PAGES_PER_ACCOUNT = 15  # 안전장치


def get_json(url: str, retries: int = 3):
    last = None
    for attempt in range(retries):
        try:
            time.sleep(PAUSE)
            req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"{url} 수집 실패: {last}")


def well_known() -> list[dict]:
    return get_json(f"{BASE}/names/well-known")


def account_transactions(account: str, marker: str | None = None) -> dict:
    url = f"{BASE}/account/{account}/transactions"
    if marker:
        url += "?" + urlencode({"marker": marker})
    return get_json(url)


def amount_xrp(tx: dict) -> float | None:
    """네이티브 XRP 결제 금액을 XRP 단위로. 발행 통화면 None."""
    box = (tx.get("meta") or {}).get("delivered_amount") or tx.get("Amount")
    if not isinstance(box, dict) or box.get("currency") != "XRP":
        return None
    try:
        return float(box["value"]) / 1_000_000  # drops → XRP
    except (KeyError, TypeError, ValueError):
        return None


def collect_account(account: str, kr_name: str, cutoff: datetime,
                    info: dict | None = None) -> list[dict]:
    """한 계정의 해외 라벨 계정 상대 XRP 결제.

    info 를 넘기면 실제로 훑은 구간을 채워 준다: oldest_scanned(가장 오래된 거래
    시각), capped(페이지 한도에 걸려 cutoff 까지 못 내려갔는지). 거래소 핫월렛은
    거래가 매우 많아 한도(MAX_PAGES_PER_ACCOUNT)로는 몇 시간치밖에 못 훑는 경우가
    있다(2026-10-07 실측: 180일 요청에 실제 약 4시간). 이를 모르고 '최근 180일'로
    표시하면 수치가 크게 오독된다.
    """
    events, marker, pages = [], None, 0
    oldest: datetime | None = None
    reached_cutoff = False
    while pages < MAX_PAGES_PER_ACCOUNT:
        pages += 1
        try:
            page = account_transactions(account, marker)
        except RuntimeError as e:
            print(f"    {kr_name} 페이지 {pages} 조회 실패: {e}", file=sys.stderr)
            break
        txs = page.get("transactions") or []
        if not txs:
            break

        stop = False
        for tx in txs:
            if tx.get("TransactionType") != "Payment":
                continue
            if (tx.get("meta") or {}).get("TransactionResult") != "tesSUCCESS":
                continue
            ts = tx.get("date")
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except (AttributeError, ValueError):
                continue
            if oldest is None or dt < oldest:
                oldest = dt
            if dt < cutoff:
                stop = True
                continue

            acc, dest = tx.get("Account"), tx.get("Destination")
            acc_name = ((tx.get("AccountName") or {}).get("name") or "")
            dest_name = ((tx.get("DestinationName") or {}).get("name") or "")

            if acc == account:
                direction, counterparty = "outflow", dest_name
            elif dest == account:
                direction, counterparty = "inflow", acc_name
            else:
                continue

            if counterparty not in GLOBAL_NAMES:
                continue
            amt = amount_xrp(tx)
            if not amt:
                continue

            events.append({
                "t": int(dt.timestamp()), "kr_wallet": f"{kr_name} ({account[:8]}…)",
                "global_wallet": counterparty, "direction": direction,
                "amount": round(amt, 2), "tx": tx.get("hash"),
            })

        marker = page.get("marker")
        if not marker or stop:
            reached_cutoff = True  # 기록 끝 또는 cutoff 도달 — 요청 구간 전체를 훑었다
            break
    if info is not None:
        info["oldest_scanned"] = oldest
        info["capped"] = not reached_cutoff
    return events


def observed_window(infos: list[dict], cutoff: datetime) -> tuple[datetime, bool]:
    """모든 계정이 공통으로 훑은 구간의 시작 시각과 '한도에 걸렸는지'.

    한 계정이라도 한도에 걸렸다면 그 계정이 훑은 가장 오래된 시각 중 가장 늦은
    값부터가 모든 계정을 같은 조건으로 볼 수 있는 구간이다. 계정마다 구간이 다르면
    거래가 적은 계정만 길게 잡혀 합계가 왜곡되므로 이 공통 구간으로 자른다.
    """
    capped = [i["oldest_scanned"] for i in infos if i.get("capped") and i.get("oldest_scanned")]
    if not capped:
        return cutoff, False
    return max(max(capped), cutoff), True


def collect(days: int) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    print("라벨 계정 목록 조회…")
    names = well_known()
    kr_accounts = [(e["account"], e["name"]) for e in names if e.get("name") in KR_NAMES]
    global_present = sorted({e["name"] for e in names if e.get("name") in GLOBAL_NAMES})
    print(f"  한국 계정 {len(kr_accounts)}개, 해외 라벨 확인됨: {', '.join(global_present) or '없음'}")

    events, infos = [], []
    for account, name in kr_accounts:
        print(f"  {name} ({account[:10]}…) 거래 내역 조회…")
        info: dict = {}
        events.extend(collect_account(account, name, cutoff, info))
        infos.append(info)

    since, truncated = observed_window(infos, cutoff)
    if truncated:
        before = len(events)
        events = [e for e in events if e["t"] >= int(since.timestamp())]
        print(f"  페이지 한도로 요청 구간({days}일)을 다 못 훑음 — 공통 관측 구간 "
              f"{since.isoformat(timespec='minutes')} 이후로 자름 ({before}→{len(events)}건)")
    now = datetime.now(timezone.utc)
    observed_hours = round((now - since).total_seconds() / 3600, 1)

    events.sort(key=lambda e: -e["t"])

    def sum_side(direction: str) -> float:
        return sum(e["amount"] for e in events if e["direction"] == direction)

    inflow, outflow = sum_side("inflow"), sum_side("outflow")

    daily: dict[str, float] = {}
    for e in events:
        d = datetime.fromtimestamp(e["t"], timezone.utc).strftime("%Y-%m-%d")
        sign = 1 if e["direction"] == "outflow" else -1
        daily[d] = daily.get(d, 0.0) + sign * e["amount"]
    daily_rows = [{"date": d, "net_outflow_xrp": round(v, 2)} for d, v in sorted(daily.items())]

    by_exchange: dict[str, dict] = {}
    for e in events:
        row = by_exchange.setdefault(e["global_wallet"], {"inflow": 0.0, "outflow": 0.0})
        row[e["direction"]] += e["amount"]
    by_exchange_rows = [
        {"exchange": n, "inflow": round(v["inflow"], 2), "outflow": round(v["outflow"], 2),
         "net_outflow": round(v["outflow"] - v["inflow"], 2)}
        for n, v in sorted(by_exchange.items(), key=lambda x: -(x[1]["inflow"] + x[1]["outflow"]))
    ]

    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "lookback_days": days, "chain": "XRPL", "asset": "XRP", "is_sample": False,
            "kr_account_count": len(kr_accounts), "global_labels_seen": global_present,
            # 실제로 모든 계정을 훑은 공통 구간. truncated 면 lookback_days 가 아니라 이 구간이 집계 기간이다.
            "observed_since": since.isoformat(timespec="seconds"),
            "observed_hours": observed_hours,
            "truncated": truncated,
            "truncated_accounts": sum(1 for i in infos if i.get("capped")),
            "coverage_note": "이름이 정확히 'Upbit'/'Bithumb'인 xrpscan 라벨 계정과 "
                             "'Binance'/'OKX'/'Bybit'인 계정 사이의 네이티브 XRP 결제만 집계. "
                             "발행 통화(RLUSD 등), 계열사 라벨(Bithumb Global 등)은 제외.",
        },
        "totals": {
            "inflow_xrp": round(inflow, 2), "outflow_xrp": round(outflow, 2),
            "net_outflow_xrp": round(outflow - inflow, 2), "event_count": len(events),
        },
        "by_exchange": by_exchange_rows,
        "daily": daily_rows,
        "events": events[:150],
    }


def probe():
    print("well-known 목록 조회…")
    names = well_known()
    print(f"  총 {len(names)}건")
    for target in ("Upbit", "Bithumb", "Binance", "OKX", "Bybit"):
        matches = [e["account"] for e in names if e.get("name") == target]
        print(f"  {target:10s} {len(matches)}개 " +
              (f"(예: {matches[0][:12]}…)" if matches else "— 못 찾음"))
    if names:
        sample_kr = next((e for e in names if e.get("name") in KR_NAMES), None)
        if sample_kr:
            print(f"\n{sample_kr['name']} ({sample_kr['account'][:12]}…) 최근 거래 3건 조회…")
            page = account_transactions(sample_kr["account"])
            for tx in (page.get("transactions") or [])[:3]:
                an = (tx.get("AccountName") or {}).get("name", "?")
                dn = (tx.get("DestinationName") or {}).get("name", "?")
                print(f"  {tx.get('date')} {tx.get('TransactionType')} {an} → {dn}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--out", default="site/data")
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args()

    if args.probe:
        probe()
        return

    print(f"최근 {args.days}일 KR↔해외 XRP 코너 수집…")
    data = collect(args.days)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "flow_xrp.json").write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    t = data["totals"]
    sign = "유출" if t["net_outflow_xrp"] >= 0 else "유입"
    m = data["meta"]
    print(f"\n완료 — 순{sign} {abs(t['net_outflow_xrp']):,.0f} XRP / 이벤트 {t['event_count']}건")
    print(f"       실제 관측 구간: {m['observed_since']} 이후 약 {m['observed_hours']}시간 "
          f"(요청 {m['lookback_days']}일, 한도 걸린 계정 {m['truncated_accounts']}/{m['kr_account_count']}개)")


if __name__ == "__main__":
    main()
