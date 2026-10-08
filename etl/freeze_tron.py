"""트론(Tron) USDT 동결·해제·소각 이벤트 수집. 표준 라이브러리만 쓴다.

트론 USDT(TRC-20) 컨트랙트도 이더리움 USDT 와 같은 이벤트를 남긴다:
AddedBlackList(동결) · RemovedBlackList(해제) · DestroyedBlackFunds(잔액 소각).
TronGrid 공개 API(https://api.trongrid.io)의 컨트랙트 이벤트 조회를 쓴다.
키 없이도 되지만 호출 한도가 낮아, 시크릿 TRONGRID_API_KEY 가 있으면 헤더로 붙인다.
"""

from __future__ import annotations

import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = "https://api.trongrid.io"
USDT_TRON = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"  # 테더 공식 TRC-20 USDT
EVENTS = {
    "freeze": "AddedBlackList",
    "unfreeze": "RemovedBlackList",
    "seize": "DestroyedBlackFunds",
}
PAUSE = 0.4
MAX_PAGES = 60  # 이벤트당 200건 × 60쪽 = 12,000건 안전장치


def _get(url: str, retries: int = 4) -> dict:
    headers = {"Accept": "application/json", "User-Agent": "stablecoin-watch/0.2"}
    key = os.environ.get("TRONGRID_API_KEY")
    if key:
        headers["TRON-PRO-API-KEY"] = key
    last = None
    for attempt in range(retries):
        try:
            time.sleep(PAUSE)
            with urlopen(Request(url, headers=headers), timeout=40) as r:
                return json.loads(r.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(2 * (attempt + 1))  # 429(한도 초과) 포함 — 조금 쉬었다 다시
    raise RuntimeError(f"TronGrid 호출 실패: {last}")


def _first(result: dict, *names):
    for n in names:
        if n in result and result[n] not in (None, ""):
            return result[n]
    # 이름이 다르게 오면 순서상 첫 값
    vals = [v for v in result.values() if v not in (None, "")]
    return vals[0] if vals else None


def parse_event(ev: dict, kind: str) -> dict:
    """TronGrid 이벤트 한 건 → 공통 이벤트 형식."""
    res = ev.get("result") or {}
    addr = _first(res, "_user", "_blackListedUser", "0")
    units = None
    if kind == "seize":
        bal = res.get("_balance", res.get("1"))
        try:
            units = round(int(bal) / 1e6, 2)
        except (TypeError, ValueError):
            units = None
    return {
        "t": int(ev.get("block_timestamp", 0)) // 1000,
        "chain": "Tron", "issuer": "Tether", "symbol": "USDT", "kind": kind,
        "addr": addr, "units": units, "tx": ev.get("transaction_id"),
    }


def collect(since_ts: int, fetch=_get) -> tuple[list[dict], dict]:
    """since_ts(초) 이후 이벤트와 상태(성공 여부·건수·메모)."""
    events, status = [], {"chain": "Tron", "symbol": "USDT", "ok": True, "note": ""}
    for kind, name in EVENTS.items():
        params = {"event_name": name, "min_block_timestamp": since_ts * 1000,
                  "limit": 200, "only_confirmed": "true", "order_by": "block_timestamp,desc"}
        fp, pages = None, 0
        while pages < MAX_PAGES:
            q = dict(params)
            if fp:
                q["fingerprint"] = fp
            try:
                body = fetch(f"{API}/v1/contracts/{USDT_TRON}/events?{urlencode(q)}")
            except RuntimeError as e:
                status["ok"] = False
                status["note"] = str(e)[:160]
                break
            pages += 1
            data = body.get("data") or []
            events += [parse_event(ev, kind) for ev in data if ev.get("event_name", name) == name]
            fp = (body.get("meta") or {}).get("fingerprint")
            if not fp or not data:
                break
        if pages >= MAX_PAGES:
            status["note"] = f"{name} 페이지 한도({MAX_PAGES}) 도달 — 일부 누락 가능"
    events = [e for e in events if e["t"] >= since_ts]
    return events, status
