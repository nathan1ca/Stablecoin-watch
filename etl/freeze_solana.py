"""솔라나 스테이블코인 동결·해제 수집. 표준 라이브러리만 쓴다.

솔라나 SPL 토큰은 블랙리스트 이벤트가 없다. 대신 민트(토큰)마다 '동결 권한 주소'
(freezeAuthority)가 있고, 그 주소가 서명한 FreezeAccount/ThawAccount 명령으로
개별 토큰 계정을 얼리고 푼다. 그래서
  1) 민트 계정에서 동결 권한 주소를 체인에서 직접 읽고(하드코딩하지 않음),
  2) 그 주소가 서명한 거래 목록(getSignaturesForAddress)을 받아,
  3) 거래를 열어(getTransaction) 해당 민트의 freezeAccount/thawAccount 만 센다.

USDC·PYUSD 는 동결 권한 주소의 거래가 수십~수백 건뿐이라 한 번에 끝난다.
USDT 는 같은 주소가 일반 송금에도 쓰여 1년치가 1만 건을 넘는다(2026-10-08 시험).
그래서 상태 파일(site/data/freeze_state_solana.json)에 어디까지 봤는지 남기고,
회차마다 새 거래 전부 + 과거분 일부(BACKFILL_PER_RUN)씩만 연다(증분 수집).

솔라나 재단 공개 RPC 는 무료지만 호출 한도가 있다. 시크릿 SOLANA_RPC_URL 이 있으면
그 주소(무료 키를 붙인 다른 RPC 등)를 쓴다.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
MINTS = [
    {"issuer": "Circle", "symbol": "USDC", "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"},
    {"issuer": "Tether", "symbol": "USDT", "mint": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"},
    {"issuer": "Paxos", "symbol": "PYUSD", "mint": "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo"},
]
PAUSE = 0.3
NEW_MAX = 1500            # 한 회차에 여는 새 거래 상한(넘으면 메모로 남김)
BACKFILL_PER_RUN = 500    # 한 회차에 여는 과거 거래 수
KIND = {"freezeAccount": "freeze", "thawAccount": "unfreeze"}


def rpc_url() -> str:
    return os.environ.get("SOLANA_RPC_URL") or DEFAULT_RPC


def _rpc(method: str, params: list, retries: int = 4):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last = None
    for attempt in range(retries):
        try:
            time.sleep(PAUSE)
            req = Request(rpc_url(), data=body, headers={"Content-Type": "application/json",
                                                          "User-Agent": "stablecoin-watch/0.2"})
            with urlopen(req, timeout=40) as r:
                j = json.loads(r.read().decode("utf-8"))
            if "error" in j:
                raise RuntimeError(j["error"])
            return j["result"]
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Solana RPC {method} 실패: {str(last)[:120]}")


def freeze_events_in_tx(tx: dict | None, mint: str) -> list[tuple[str, str]]:
    """거래 안의 (조치, 토큰계정) 목록 — 해당 민트의 freeze/thaw 만."""
    if not tx or (tx.get("meta") or {}).get("err"):
        return []
    msg = (tx.get("transaction") or {}).get("message") or {}
    ins = list(msg.get("instructions") or [])
    for grp in (tx.get("meta") or {}).get("innerInstructions") or []:
        ins += grp.get("instructions") or []
    out = []
    for i in ins:
        p = i.get("parsed")
        if not isinstance(p, dict):
            continue
        k = KIND.get(p.get("type"))
        info = p.get("info") or {}
        if k and info.get("mint") == mint:
            out.append((k, info.get("account")))
    return out


def _sigs(addr: str, rpc, before=None, until=None, limit=1000):
    p = {"limit": limit}
    if before:
        p["before"] = before
    if until:
        p["until"] = until
    return rpc("getSignaturesForAddress", [addr, p]) or []


def collect_mint(spec: dict, st: dict, since_ts: int, rpc=_rpc) -> tuple[list[dict], dict]:
    """민트 하나. st 는 이 민트의 상태(제자리 갱신). 반환: (전체 이벤트, 상태 메모)."""
    note, opened = "", 0
    info = rpc("getAccountInfo", [spec["mint"], {"encoding": "jsonParsed"}])
    fa = (((info or {}).get("value") or {}).get("data") or {}).get("parsed", {}).get("info", {}).get("freezeAuthority")
    if not fa:
        return st.get("events", []), {"note": "동결 권한 주소 없음", "done": True, "opened": 0}
    if st.get("authority") and st["authority"] != fa:
        st.clear()  # 동결 권한 주소가 바뀌면 처음부터 다시
    st["authority"] = fa
    events = st.setdefault("events", [])
    have = {(e["tx"], e["addr"], e["kind"]) for e in events}

    def open_sig(s):
        nonlocal opened
        if s.get("err"):
            return
        tx = rpc("getTransaction", [s["signature"], {"encoding": "jsonParsed",
                                                     "maxSupportedTransactionVersion": 0}])
        opened += 1
        for kind, acct in freeze_events_in_tx(tx, spec["mint"]):
            key = (s["signature"], acct, kind)
            if key in have:
                continue
            have.add(key)
            events.append({"t": int(s.get("blockTime") or 0), "chain": "Solana",
                           "issuer": spec["issuer"], "symbol": spec["symbol"], "kind": kind,
                           "addr": acct, "units": None, "tx": s["signature"]})

    # 1) 새 거래 — 지난번에 본 가장 최근 서명(newest) 이후 전부.
    #    첫 회차에는 기준점만 잡고, 실제 조회는 아래 과거분 채우기가 최신부터 한다.
    if not st.get("newest"):
        top = _sigs(fa, rpc, limit=1)
        if top:
            st["newest"] = top[0]["signature"]
            st["backfill_from_top"] = True   # 과거분 채우기를 가장 최신 거래부터 시작
    else:
        new, before = [], None
        while True:
            page = _sigs(fa, rpc, before=before, until=st["newest"])
            if not page:
                break
            new += page
            before = page[-1]["signature"]
            if len(page) < 1000 or len(new) >= NEW_MAX:
                break
        if len(new) >= NEW_MAX:
            note = f"새 거래가 {NEW_MAX}건을 넘어 일부만 확인"
        if new:
            st["newest"] = new[0]["signature"]
        for s in new:
            if (s.get("blockTime") or 0) >= since_ts:
                open_sig(s)

    # 2) 과거분 — 조회 기간 시작점에 닿을 때까지 회차마다 BACKFILL_PER_RUN 건씩
    budget = BACKFILL_PER_RUN
    while budget > 0 and not st.get("done") and st.get("newest"):
        cursor = None if st.get("backfill_from_top") else st.get("oldest")
        want = min(1000, budget)
        page = _sigs(fa, rpc, before=cursor, limit=want)
        st["backfill_from_top"] = False
        if not page:
            st["done"] = True
            break
        for s in page:
            if (s.get("blockTime") or 0) < since_ts:
                st["done"] = True
                break
            open_sig(s)
            budget -= 1
            st["oldest"] = s["signature"]
            st["oldest_t"] = s.get("blockTime")
        else:
            if len(page) < want:   # 더 오래된 거래가 없다
                st["done"] = True

    st["events"] = [e for e in events if e["t"] >= since_ts]
    return st["events"], {"note": note, "done": bool(st.get("done")), "opened": opened,
                          "covered_from": st.get("oldest_t")}


def collect(since_ts: int, state_path: Path, rpc=_rpc) -> tuple[list[dict], list[dict]]:
    try:
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    all_ev, statuses = [], []
    for spec in MINTS:
        st = state.setdefault(spec["mint"], {})
        try:
            ev, info = collect_mint(spec, st, since_ts, rpc)
            all_ev += ev
            statuses.append({"chain": "Solana", "symbol": spec["symbol"], "ok": True,
                             "complete": info["done"], "covered_from": info.get("covered_from"),
                             "note": info["note"] or ("" if info["done"] else "과거 기록 채우는 중"),
                             "opened": info["opened"]})
        except RuntimeError as e:
            all_ev += st.get("events", [])
            statuses.append({"chain": "Solana", "symbol": spec["symbol"], "ok": False,
                             "complete": bool(st.get("done")), "note": str(e)[:160]})
    Path(state_path).write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                                encoding="utf-8")
    return all_ev, statuses
