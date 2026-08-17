#!/usr/bin/env python3
"""
스테이블코인 감시 - 발행사 동결·소각 조치 수집

발행사가 특정 주소를 블랙리스트에 올리거나 잔액을 소각한 기록은 전부 온체인
이벤트 로그로 남는다. 페그 편차가 시장이 발행사를 어떻게 보는지의 지표라면,
동결 건수는 발행사가 실제로 통제권을 얼마나 행사하는지의 지표다.

Etherscan V2 API 키가 필요하다(무료). https://etherscan.io/apis
    export ETHERSCAN_API_KEY=...
    python etl/fetch_freeze.py --days 365

키가 없으면 아무것도 하지 않고 종료하며, 사이트는 해당 섹션을 숨긴다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from keccak import topic0  # noqa: E402

API = "https://api.etherscan.io/v2/api"  # V1은 2025-08-15 종료. chainid 파라미터로 체인 지정.
UA = "stablecoin-watch/0.2"
PAUSE = 0.25  # 무료 티어 초당 5회 제한 대응

# 발행사별 컨트랙트와 이벤트 시그니처.
# 같은 동작이라도 구현체마다 이름이 다르므로 후보를 여러 개 두고, 로그가 잡히는
# 쪽을 채택한다. verified=False 는 시그니처를 실물 로그로 확인하지 못했다는 뜻이다.
ISSUERS = [
    {
        "issuer": "Tether", "symbol": "USDT", "chainid": 1, "decimals": 6, "verified": True,
        "address": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        "events": {
            "freeze": ["AddedBlackList(address)"],
            "unfreeze": ["RemovedBlackList(address)"],
            "seize": ["DestroyedBlackFunds(address,uint256)"],
        },
    },
    {
        "issuer": "Circle", "symbol": "USDC", "chainid": 1, "decimals": 6, "verified": True,
        "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "events": {
            "freeze": ["Blacklisted(address)"],
            "unfreeze": ["UnBlacklisted(address)"],
            "seize": [],
        },
    },
    {
        "issuer": "Paxos", "symbol": "PYUSD", "chainid": 1, "decimals": 6, "verified": False,
        "address": "0x6c3ea9036406852006290770BEdFcAbA0e23A0e8",
        "events": {
            "freeze": ["FreezeAddress(address)", "AddressFrozen(address)"],
            "unfreeze": ["UnfreezeAddress(address)", "AddressUnfrozen(address)"],
            "seize": ["WipeFrozenAddress(address)", "FrozenAddressWiped(address)"],
        },
    },
    {
        "issuer": "Paxos", "symbol": "USDP", "chainid": 1, "decimals": 18, "verified": False,
        "address": "0x8E870D67F660D95d5be530380D0eC0bd388289E1",
        "events": {
            "freeze": ["FreezeAddress(address)", "AddressFrozen(address)"],
            "unfreeze": ["UnfreezeAddress(address)", "AddressUnfrozen(address)"],
            "seize": ["WipeFrozenAddress(address)", "FrozenAddressWiped(address)"],
        },
    },
]

KIND_KO = {"freeze": "동결", "unfreeze": "해제", "seize": "소각"}


def api_key() -> str | None:
    return os.environ.get("ETHERSCAN_API_KEY") or None


def call(params: dict, key: str, retries: int = 3):
    params = {**params, "apikey": key}
    url = API + "?" + urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            time.sleep(PAUSE)
            req = Request(url, headers={"User-Agent": UA})
            with urlopen(req, timeout=45) as r:
                body = json.loads(r.read().decode("utf-8"))
            # status "0" 는 오류이거나 단순히 결과 없음이다. 둘을 구분한다.
            if body.get("status") == "1":
                return body.get("result") or []
            msg = str(body.get("message", "")) + " " + str(body.get("result", ""))
            if "No records found" in msg or "No logs found" in msg:
                return []
            if "rate limit" in msg.lower():
                time.sleep(1.5 * (attempt + 1))
                last = RuntimeError(msg)
                continue
            raise RuntimeError(msg.strip())
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Etherscan 호출 실패: {last}")


def block_at(ts: int, chainid: int, key: str) -> int:
    r = call({"chainid": chainid, "module": "block", "action": "getblocknobytime",
              "timestamp": ts, "closest": "before"}, key)
    return int(r)


def get_logs(address: str, t0: str, from_b: int, chainid: int, key: str) -> list[dict]:
    out, page = [], 1
    while True:
        r = call({"chainid": chainid, "module": "logs", "action": "getLogs",
                  "address": address, "topic0": t0, "fromBlock": from_b,
                  "toBlock": "latest", "page": page, "offset": 1000}, key)
        if not r:
            break
        out.extend(r)
        if len(r) < 1000:
            break
        page += 1
        if page > 12:  # 안전장치
            break
    return out


def word(data_hex: str, i: int) -> int:
    """data 의 i번째 32바이트 워드를 정수로."""
    d = data_hex[2:] if data_hex.startswith("0x") else data_hex
    chunk = d[i * 64:(i + 1) * 64]
    return int(chunk, 16) if len(chunk) == 64 else 0


def decode(log: dict) -> tuple[str | None, int | None]:
    """대상 주소와 금액(있으면)을 꺼낸다.

    구현체마다 파라미터를 indexed 로 두기도 하고 아니기도 해서 둘 다 본다.
    (USDC 는 indexed → topics[1], USDT 는 non-indexed → data)
    """
    topics = log.get("topics") or []
    addr = None
    if len(topics) > 1 and isinstance(topics[1], str) and len(topics[1]) >= 42:
        addr = "0x" + topics[1][-40:]
    data = log.get("data") or "0x"
    if addr is None and len(data) >= 66:
        addr = "0x" + data[2:66][-40:]
    amount = None
    body = data[2:] if data.startswith("0x") else data
    if addr and len(topics) > 1 and len(body) >= 64:
        amount = word(data, 0)          # indexed 주소 + data 첫 워드가 금액
    elif len(body) >= 128:
        amount = word(data, 1)          # non-indexed 주소 다음 워드가 금액
    return addr, amount


def collect(days: int, key: str) -> dict:
    now = int(datetime.now(timezone.utc).timestamp())
    since = now - days * 86400
    events, notes = [], []
    per_issuer: dict[str, dict] = {}
    block_cache: dict[int, int] = {}

    for spec in ISSUERS:
        cid = spec["chainid"]
        if cid not in block_cache:
            print(f"  체인 {cid} 시작 블록 조회…")
            block_cache[cid] = block_at(since, cid, key)
        from_b = block_cache[cid]

        row = per_issuer.setdefault(spec["symbol"], {
            "issuer": spec["issuer"], "symbol": spec["symbol"],
            "address": spec["address"], "verified": spec["verified"],
            "freeze": 0, "unfreeze": 0, "seize": 0, "seized_units": 0.0,
            "matched_signatures": [],
        })

        for kind, sigs in spec["events"].items():
            for sig in sigs:
                t0 = topic0(sig)
                try:
                    logs = get_logs(spec["address"], t0, from_b, cid, key)
                except RuntimeError as e:
                    notes.append(f"{spec['symbol']} {sig}: {e}")
                    continue
                if not logs:
                    continue
                row["matched_signatures"].append(sig)
                for lg in logs:
                    addr, amt = decode(lg)
                    ts = int(lg.get("timeStamp", "0x0"), 16) if str(
                        lg.get("timeStamp", "")).startswith("0x") else int(lg.get("timeStamp") or 0)
                    units = (amt / 10 ** spec["decimals"]) if (amt and kind == "seize") else None
                    events.append({
                        "t": ts, "issuer": spec["issuer"], "symbol": spec["symbol"],
                        "kind": kind, "addr": addr, "units": round(units, 2) if units else None,
                        "tx": lg.get("transactionHash"),
                    })
                    row[kind] += 1
                    if units:
                        row["seized_units"] += units
                print(f"  {spec['symbol']:6s} {KIND_KO[kind]} {sig:34s} {len(logs):5d}건")
                break  # 시그니처 후보 중 잡힌 것 하나면 충분

        if not row["matched_signatures"] and not spec["verified"]:
            notes.append(f"{spec['symbol']}: 이벤트 시그니처를 확인하지 못했습니다. "
                         f"컨트랙트 ABI를 보고 ISSUERS 설정을 고치십시오.")

    events.sort(key=lambda e: -e["t"])

    # 월별 집계 — 조치의 빈도 추이가 개별 건보다 의미 있다
    monthly: dict[str, dict[str, int]] = {}
    for e in events:
        m = datetime.fromtimestamp(e["t"], timezone.utc).strftime("%Y-%m")
        monthly.setdefault(m, {})
        monthly[m][e["issuer"]] = monthly[m].get(e["issuer"], 0) + (1 if e["kind"] != "unfreeze" else 0)
    monthly_rows = [{"m": m, **v} for m, v in sorted(monthly.items())]

    rows = sorted(per_issuer.values(), key=lambda r: -(r["freeze"] + r["seize"]))
    for r in rows:
        r["seized_units"] = round(r["seized_units"], 2)

    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "lookback_days": days, "chain": "Ethereum", "is_sample": False,
            "source": "Etherscan V2", "notes": notes,
        },
        "totals": {
            "freeze": sum(r["freeze"] for r in rows),
            "unfreeze": sum(r["unfreeze"] for r in rows),
            "seize": sum(r["seize"] for r in rows),
            "seized_units": round(sum(r["seized_units"] for r in rows), 2),
            "issuers": len([r for r in rows if r["freeze"] or r["seize"]]),
        },
        "issuers": rows,
        "monthly": monthly_rows,
        "events": events[:200],
    }


def probe(key: str):
    print("이벤트 시그니처 → topic0")
    for spec in ISSUERS:
        print(f"\n{spec['symbol']} {spec['address']} (verified={spec['verified']})")
        for kind, sigs in spec["events"].items():
            for sig in sigs:
                print(f"  {KIND_KO[kind]:4s} {sig:36s} {topic0(sig)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365, help="조회 기간(일)")
    ap.add_argument("--out", default="site/data")
    ap.add_argument("--probe", action="store_true", help="시그니처와 topic0만 출력")
    args = ap.parse_args()

    key = api_key()
    if args.probe:
        probe(key or "")
        return
    if not key:
        print("ETHERSCAN_API_KEY 가 없어 동결 수집을 건너뜁니다.", file=sys.stderr)
        print("무료 키 발급: https://etherscan.io/apis", file=sys.stderr)
        return

    print(f"최근 {args.days}일 동결·소각 조치 수집…")
    data = collect(args.days, key)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "freeze.json").write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    t = data["totals"]
    print(f"\n완료 — 동결 {t['freeze']}건 / 해제 {t['unfreeze']}건 / 소각 {t['seize']}건")
    for n in data["meta"]["notes"]:
        print(f"  참고: {n}", file=sys.stderr)


if __name__ == "__main__":
    main()
