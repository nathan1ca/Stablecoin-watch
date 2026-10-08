"""이더리움 토큰 이체 건수를 Etherscan 로그로 직접 센다(Coin Metrics 무료 범위 밖 종목용).

2026-10-08 네이선 요청: USDS·USD1·USDG·RLUSD 이체 건수. 이 네 종목은 Coin Metrics
커뮤니티에 없어, 동결 수집에 쓰는 ETHERSCAN_API_KEY(무료)로 Transfer 이벤트를 하루 단위로 센다.

방식
  · getLogs(topic0=Transfer) 를 블록 구간으로 나눠 받는다. Etherscan 은 한 조회에 최대 1만 건
    (1,000건 × 10쪽)까지만 주므로, 구간 결과가 1만 건에 닿으면 구간을 반으로 나눠 다시 받는다.
  · 로그의 timeStamp 로 UTC 날짜별 건수를 쌓고, 어디까지 셌는지(last_block)를 상태 파일
    (site/data/onchain_eth_state.json)에 남겨 다음 회차가 이어 센다.
  · 처음엔 31일 전 0시(UTC) 블록부터 앞으로 센다. 회차당 시간 상한(TIME_BUDGET)이 있어
    30일치가 차기까지 몇 회차가 걸릴 수 있다 — 그동안은 '채우는 중'으로 표시.
한계(화면에 그대로 밝힘): 이더리움 몫만 센다. 같은 종목의 솔라나·BNB·XRP 원장·X Layer 등
다른 체인 이체는 빠진다.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = "https://api.etherscan.io/v2/api"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# 2026-10-08 Blockscout 로 이름·심볼 대조(작업 브랜치 검증 로그).
TOKENS = [
    {"symbol": "USDS", "address": "0xdC035D45d973E3EC169d2276DDab16f1e407384F"},
    {"symbol": "USD1", "address": "0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d"},
    {"symbol": "USDG", "address": "0xe343167631d89B6Ffc58B88d6b7fB0228795491D"},
    {"symbol": "RLUSD", "address": "0x8292Bb45bf1Ee4d140127049757C2E0fF06317eD"},
]
STATE = Path(__file__).resolve().parents[1] / "site" / "data" / "onchain_eth_state.json"
TIME_BUDGET = float(os.environ.get("ETH_COUNT_TIME_BUDGET", "120"))
PAUSE = 0.22            # 무료 키 초당 5회 아래로
CAP = 10_000            # 한 조회 최대 결과 수(1,000 × 10쪽)
START_SPAN = 2_000      # 첫 구간 블록 수(약 6.7시간). 넘치면 반으로 나눈다.
KEEP_DAYS = 70


def _call(params: dict, key: str, retries: int = 3):
    q = {"chainid": 1, **params, "apikey": key}
    last = None
    for attempt in range(retries):
        try:
            time.sleep(PAUSE)
            req = Request(f"{API}?{urlencode(q)}", headers={"User-Agent": "stablecoin-watch/0.2"})
            with urlopen(req, timeout=40) as r:
                j = json.loads(r.read().decode("utf-8"))
            res = j.get("result")
            if isinstance(res, str) and ("rate limit" in res.lower() or "max calls" in res.lower()):
                raise RuntimeError(res)
            return j
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Etherscan 호출 실패: {str(last)[:120]}")


def tip_block(key: str, call=_call) -> int:
    return int(call({"module": "proxy", "action": "eth_blockNumber"}, key)["result"], 16)


def block_at(ts: int, key: str, call=_call) -> int:
    r = call({"module": "block", "action": "getblocknobytime", "timestamp": ts, "closest": "after"}, key)
    return int(r["result"])


def logs_page(addr: str, frm: int, to: int, page: int, key: str, call=_call) -> list[dict]:
    r = call({"module": "logs", "action": "getLogs", "address": addr, "topic0": TRANSFER,
              "fromBlock": frm, "toBlock": to, "page": page, "offset": 1000}, key)
    res = r.get("result")
    if isinstance(res, list):
        return res
    msg = str(r.get("message") or "") + " " + str(res or "")
    if "no records" in msg.lower():
        return []
    raise RuntimeError(f"getLogs 응답 이상: {msg[:120]}")


def count_range(addr: str, frm: int, to: int, key: str, call=_call) -> dict[str, int] | None:
    """[frm, to] 구간의 날짜별 건수. 1만 건에 닿으면 None(구간을 나눠야 함)."""
    per_day: dict[str, int] = {}
    n = 0
    for page in range(1, CAP // 1000 + 1):
        res = logs_page(addr, frm, to, page, key, call)
        for lg in res:
            d = datetime.fromtimestamp(int(lg["timeStamp"], 16), timezone.utc).strftime("%Y-%m-%d")
            per_day[d] = per_day.get(d, 0) + 1
        n += len(res)
        if len(res) < 1000:
            return per_day
    return None if n >= CAP else per_day


def collect_token(tok: dict, st: dict, key: str, tip: int, deadline: float,
                  call=_call, clock=time.monotonic, now: datetime | None = None) -> dict:
    """한 종목을 last_block 다음부터 tip 까지(시간 상한 안에서) 센다. st 를 제자리 갱신."""
    now = now or datetime.now(timezone.utc)
    if not st.get("last_block"):
        start_day = (now - timedelta(days=31)).replace(hour=0, minute=0, second=0, microsecond=0)
        st["start_date"] = start_day.strftime("%Y-%m-%d")
        st["last_block"] = block_at(int(start_day.timestamp()), key, call) - 1
        st.setdefault("daily", {})
    daily = st.setdefault("daily", {})
    frm, span = st["last_block"] + 1, START_SPAN
    while frm <= tip and clock() < deadline:
        to = min(tip, frm + span - 1)
        got = count_range(tok["address"], frm, to, key, call)
        if got is None:                 # 1만 건 초과 — 구간을 반으로
            if span <= 1:
                raise RuntimeError(f"{tok['symbol']} 한 블록에 이체 1만 건 초과")
            span = max(1, span // 2)
            continue
        for d, c in got.items():
            daily[d] = daily.get(d, 0) + c
        st["last_block"] = to
        frm = to + 1
        if len(got) and sum(got.values()) < CAP // 4:
            span = min(span * 2, 50_000)   # 한산하면 구간을 넓힌다
    st["caught_up"] = st["last_block"] >= tip
    cut = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    st["daily"] = {d: c for d, c in sorted(daily.items()) if d >= cut}
    return st


def summarize(sym: str, st: dict, days: int = 30, now: datetime | None = None) -> dict | None:
    """최근 완결된 days 일(어제까지)의 합계. 아직 30일이 다 안 찼으면 complete=False.

    날짜 D 가 '완결'이려면 D 의 마지막 블록까지 셌어야 한다. 따라잡았으면(caught_up) 어제까지,
    아니면 기록이 있는 가장 늦은 날의 전날까지만 완결로 본다.
    """
    now = now or datetime.now(timezone.utc)
    daily = st.get("daily") or {}
    start = st.get("start_date")
    if not start:
        return None
    yesterday = (now - timedelta(days=1)).date()
    if st.get("caught_up"):
        until = yesterday
    elif daily:
        until = min(yesterday, datetime.strptime(max(daily), "%Y-%m-%d").date() - timedelta(days=1))
    else:
        return None
    first = max(datetime.strptime(start, "%Y-%m-%d").date(), yesterday - timedelta(days=days - 1))
    if until < first:
        return None
    rows, d = [], first
    while d <= until:
        k = d.strftime("%Y-%m-%d")
        rows.append([k, int(daily.get(k, 0))])
        d += timedelta(days=1)
    tot = sum(c for _, c in rows)
    return {"symbol": sym, "chain": "Ethereum", "days": len(rows), "from": rows[0][0], "to": rows[-1][0],
            "tx_30d": tot, "tx_avg": round(tot / len(rows)), "tx_prev_30d": None, "chg_pct": None,
            "active_avg": None, "daily": rows, "complete": len(rows) >= days and until == yesterday,
            "source": "Etherscan getLogs(Transfer) 직접 집계"}


def collect(key: str | None, state_path: Path = STATE, call=_call, clock=time.monotonic) -> tuple[list[dict], dict]:
    if not key:
        return [], {t["symbol"]: "건너뜀: ETHERSCAN_API_KEY 없음" for t in TOKENS}
    try:
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    status, rows = {}, []
    try:
        tip = tip_block(key, call)
    except RuntimeError as e:
        return [], {t["symbol"]: f"실패: {e}" for t in TOKENS}
    deadline = clock() + TIME_BUDGET
    for i, tok in enumerate(TOKENS):
        # 남은 시간을 남은 종목 수로 나눠 준다 — 이체가 많은 USDS 가 상한을 다 써서 나머지가
        # 한 건도 못 세는 일을 막는다(2026-10-08 시험: USDS 혼자 120초에 15일치). 남는 시간은 다음 종목으로.
        tok_deadline = clock() + max(0.0, deadline - clock()) / (len(TOKENS) - i)
        st = state.setdefault(tok["symbol"], {})
        if st.get("address") and st["address"].lower() != tok["address"].lower():
            st.clear()
        st["address"] = tok["address"]
        try:
            collect_token(tok, st, key, tip, tok_deadline, call, clock)
            status[tok["symbol"]] = "ok" if st.get("caught_up") else "채우는 중(시간 상한 — 다음 회차에 이어 셈)"
        except RuntimeError as e:
            status[tok["symbol"]] = f"실패: {str(e)[:120]}"
        r = summarize(tok["symbol"], st)
        if r:
            rows.append(r)
    Path(state_path).write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return rows, status
