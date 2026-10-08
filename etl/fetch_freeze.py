#!/usr/bin/env python3
"""
스테이블코인 감시 - 발행사 동결·소각 조치 수집

발행사가 특정 주소를 블랙리스트에 올리거나 잔액을 소각한 기록은 전부 온체인
이벤트 로그로 남는다. 페그 편차가 시장이 발행사를 어떻게 보는지의 지표라면,
동결 건수는 발행사가 실제로 통제권을 얼마나 행사하는지의 지표다.

체인별 수집 경로(2026-10-08 다중 체인 확장)
  - 이더리움·Arbitrum·Polygon·Base·Optimism·Avalanche: Etherscan V2 API(무료 키 하나로
    chainid 만 바꿔 조회). 키(ETHERSCAN_API_KEY)가 없으면 이 체인들만 건너뛴다.
    무료 키로 조회가 막힌 체인은 상태에 사유를 남기고 계속한다.
  - 트론: TronGrid 공개 API(etl/freeze_tron.py), 키 불필요(TRONGRID_API_KEY 선택).
  - 솔라나: 공개 RPC(etl/freeze_solana.py), 동결 권한 주소의 거래를 증분 수집.
    상태 파일 site/data/freeze_state_solana.json(봇이 커밋).
  - BNB 체인의 USDT·USDC 는 바이낸스가 발행한 브리지 토큰(Binance-Peg)이라 발행사
    조치가 아니어서 넣지 않는다.

    export ETHERSCAN_API_KEY=...
    python etl/fetch_freeze.py --days 90
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from keccak import topic0  # noqa: E402
import freeze_solana  # noqa: E402
import freeze_tron  # noqa: E402

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

CHAIN_NAME = {1: "Ethereum", 42161: "Arbitrum", 137: "Polygon", 8453: "Base",
              10: "Optimism", 43114: "Avalanche"}

# Circle 의 다른 체인 USDC(네이티브 발행분). 이벤트는 이더리움과 같은 FiatToken 구현.
_USDC = {"freeze": ["Blacklisted(address)"], "unfreeze": ["UnBlacklisted(address)"], "seize": []}
# 테더의 다른 체인 USDT. 구현체가 둘이라(구형 TetherToken / 신형 TetherTokenV2·USDT0)
# 시그니처 후보를 둘 다 둔다. 로그가 잡히는 쪽을 채택한다.
_USDT = {
    "freeze": ["AddedBlackList(address)", "BlockPlaced(address)"],
    "unfreeze": ["RemovedBlackList(address)", "BlockReleased(address)"],
    "seize": ["DestroyedBlackFunds(address,uint256)", "DestroyedBlockedFunds(address,uint256)"],
}
# Base·Optimism·Avalanche 는 Etherscan 무료 키로 조회가 막혀 있다(2026-10-08 CI:
# "Free API access is not supported for this chain"). 유료 플랜이 생기면 PAID_CHAINS 를
# 비우면 바로 수집된다.
PAID_CHAINS = {8453, 10, 43114}
for _cid, _addr in [(42161, "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"),
                    (137, "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"),
                    (8453, "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
                    (10, "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85"),
                    (43114, "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E")]:
    ISSUERS.append({"issuer": "Circle", "symbol": "USDC", "chainid": _cid, "decimals": 6,
                    "verified": _cid in (42161, 137), "address": _addr, "events": _USDC})
for _cid, _addr in [(42161, "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"),
                    (137, "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"),
                    (10, "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58"),
                    (43114, "0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7")]:
    ISSUERS.append({"issuer": "Tether", "symbol": "USDT", "chainid": _cid, "decimals": 6,
                    "verified": False, "address": _addr, "events": _USDT})

SOLANA_STATE = Path(__file__).resolve().parent.parent / "site" / "data" / "freeze_state_solana.json"

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


def collect_evm(since: int, key: str) -> tuple[list[dict], list[dict], list[str]]:
    """Etherscan V2 체인들. 반환: (이벤트, 체인·종목별 상태, 메모)."""
    events, statuses, notes = [], [], []
    block_cache: dict[int, int | None] = {}
    for spec in ISSUERS:
        cid = spec["chainid"]
        chain = CHAIN_NAME.get(cid, str(cid))
        st = {"chain": chain, "symbol": spec["symbol"], "ok": True, "complete": True,
              "note": "", "matched": []}
        if cid in PAID_CHAINS:
            st.update(ok=False, note="Etherscan 무료 키 미지원 체인 — 제외")
            statuses.append(st)
            continue
        if cid not in block_cache:
            try:
                print(f"  {chain} 시작 블록 조회…")
                block_cache[cid] = block_at(since, cid, key)
            except RuntimeError as e:
                block_cache[cid] = None
                notes.append(f"{chain}: {e}")
        if block_cache[cid] is None:
            st.update(ok=False, note="조회 불가(무료 키 미지원 체인이거나 일시 장애)")
            statuses.append(st)
            continue
        for kind, sigs in spec["events"].items():
            for sig in sigs:
                try:
                    logs = get_logs(spec["address"], topic0(sig), block_cache[cid], cid, key)
                except RuntimeError as e:
                    st.update(ok=False, note=str(e)[:160])
                    continue
                if not logs:
                    continue
                st["matched"].append(sig)
                for lg in logs:
                    addr, amt = decode(lg)
                    ts = int(lg.get("timeStamp", "0x0"), 16) if str(
                        lg.get("timeStamp", "")).startswith("0x") else int(lg.get("timeStamp") or 0)
                    units = (amt / 10 ** spec["decimals"]) if (amt and kind == "seize") else None
                    events.append({
                        "t": ts, "chain": chain, "issuer": spec["issuer"], "symbol": spec["symbol"],
                        "kind": kind, "addr": addr, "units": round(units, 2) if units else None,
                        "tx": lg.get("transactionHash"),
                    })
                print(f"  {chain:9s} {spec['symbol']:6s} {KIND_KO[kind]} {sig:38s} {len(logs):5d}건")
                break  # 시그니처 후보 중 잡힌 것 하나면 충분
        if st["ok"] and not st["matched"] and not spec["verified"]:
            st["note"] = "조회 기간 0건 — 이 체인 컨트랙트의 동결 이벤트 이름은 아직 실물로 확인 못 함"
        statuses.append(st)
    return events, statuses, notes


# 집중 조치일: 한 종목이 하루에 평소보다 훨씬 많이 동결·소각한 날.
# 보통 수사 한 건·제재 지정 한 번으로 여러 주소를 한꺼번에 묶을 때 생긴다.
# 기준 = max(SPIKE_MIN, SPIKE_MULT × 조회 기간 일별 중앙값(조치 없는 날 0 포함)).
# 2026-10-08 1년치로 보면 USDT 중앙값 10건/일 → 50건 이상인 날(6일),
# USDC 중앙값 0 → 20건 이상인 날이 걸린다. 화면 표시용이며 경보 등급에는 쓰지 않는다.
SPIKE_MIN = 20
SPIKE_MULT = 5
SPIKE_TOP = 12


def spikes(events: list[dict], days: int) -> list[dict]:
    """종목별 집중 조치일 목록(건수 큰 순, 최대 SPIKE_TOP). 같은 날 다른 종목도 몰렸으면 joint 표시."""
    per: dict[str, dict[str, dict]] = {}
    for e in events:
        if e["kind"] == "unfreeze":
            continue
        d = datetime.fromtimestamp(e["t"], timezone.utc).strftime("%Y-%m-%d")
        c = per.setdefault(e["symbol"], {}).setdefault(
            d, {"date": d, "symbol": e["symbol"], "issuer": e["issuer"], "freeze": 0, "seize": 0,
                "units": 0.0, "chains": {}})
        c["freeze" if e["kind"] == "freeze" else "seize"] += 1
        c["chains"][e["chain"]] = c["chains"].get(e["chain"], 0) + 1
        if e.get("units"):
            c["units"] += e["units"]
    out = []
    for sym, by_day in per.items():
        counts = [c["freeze"] + c["seize"] for c in by_day.values()]
        counts += [0] * max(0, days - len(counts))
        med = statistics.median(counts) if counts else 0
        cut = max(SPIKE_MIN, SPIKE_MULT * med)
        for c in by_day.values():
            n = c["freeze"] + c["seize"]
            if n >= cut:
                out.append(dict(c, n=n, median=med, units=round(c["units"], 2)))
    by_date: dict[str, set] = {}
    for c in out:
        by_date.setdefault(c["date"], set()).add(c["symbol"])
    for c in out:
        c["joint"] = sorted(by_date[c["date"]] - {c["symbol"]})
    out.sort(key=lambda c: (-c["n"], c["date"]))
    return out[:SPIKE_TOP]


def summarize(events: list[dict], statuses: list[dict], days: int, notes: list[str]) -> dict:
    events = sorted(events, key=lambda e: -e["t"])
    # 발행사(종목)별 — 체인을 합친 값
    per: dict[str, dict] = {}
    for e in events:
        r = per.setdefault(e["symbol"], {"issuer": e["issuer"], "symbol": e["symbol"],
                                         "freeze": 0, "unfreeze": 0, "seize": 0,
                                         "seized_units": 0.0, "chains": {}})
        r[e["kind"]] += 1
        r["chains"][e["chain"]] = r["chains"].get(e["chain"], 0) + (0 if e["kind"] == "unfreeze" else 1)
        if e.get("units"):
            r["seized_units"] += e["units"]
    issuer_of = {sp["symbol"]: sp["issuer"] for sp in ISSUERS + freeze_solana.MINTS}
    for st in statuses:  # 이벤트가 0건인 종목도 행은 남긴다
        per.setdefault(st["symbol"], {"issuer": issuer_of.get(st["symbol"], st["symbol"]),
                                      "symbol": st["symbol"], "freeze": 0, "unfreeze": 0, "seize": 0,
                                      "seized_units": 0.0, "chains": {}})
    rows = sorted(per.values(), key=lambda r: -(r["freeze"] + r["seize"]))
    for r in rows:
        r["seized_units"] = round(r["seized_units"], 2)

    # 체인별 — 상태와 건수를 함께
    by_chain: dict[tuple, dict] = {}
    for st in statuses:
        k = (st["chain"], st["symbol"])
        row = by_chain.setdefault(k, {"chain": st["chain"], "symbol": st["symbol"], "freeze": 0,
                                      "unfreeze": 0, "seize": 0, "ok": True, "complete": True, "note": ""})
        row["ok"] = row["ok"] and st.get("ok", True)
        row["complete"] = row["complete"] and st.get("complete", True)
        if st.get("note"):
            row["note"] = st["note"]
        if st.get("covered_from"):
            row["covered_from"] = st["covered_from"]
    for e in events:
        row = by_chain.setdefault((e["chain"], e["symbol"]), {"chain": e["chain"], "symbol": e["symbol"],
                                                              "freeze": 0, "unfreeze": 0, "seize": 0,
                                                              "ok": True, "complete": True, "note": ""})
        row[e["kind"]] += 1
    chain_rows = sorted(by_chain.values(), key=lambda r: -(r["freeze"] + r["seize"]))
    chains_ok = sorted({r["chain"] for r in chain_rows if r["ok"]})

    # 종목별 일별 건수(해제 제외) — 화면의 시간축 표식은 잘린 events 목록이 아니라 이것으로 그린다
    daily: dict[str, dict[str, list[int]]] = {}
    for e in events:
        if e["kind"] == "unfreeze":
            continue
        d = datetime.fromtimestamp(e["t"], timezone.utc).strftime("%Y-%m-%d")
        cell = daily.setdefault(e["symbol"], {}).setdefault(d, [0, 0])
        cell[1 if e["kind"] == "seize" else 0] += 1

    monthly: dict[str, dict[str, int]] = {}
    for e in events:
        if e["kind"] == "unfreeze":
            continue
        m = datetime.fromtimestamp(e["t"], timezone.utc).strftime("%Y-%m")
        monthly.setdefault(m, {})
        monthly[m][e["issuer"]] = monthly[m].get(e["issuer"], 0) + 1
    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "lookback_days": days,
            "chain": ("Ethereum" if chains_ok == ["Ethereum"] else f"{len(chains_ok)}개 체인"),
            "chains": chains_ok,
            "is_sample": False,
            "source": "Etherscan V2 · TronGrid · Solana RPC",
            "notes": notes,
            "solana_unit_note": "솔라나는 주소가 아니라 토큰 계정 단위로 동결한다(한 사람이 여러 계정을 가질 수 있음).",
        },
        "totals": {
            "freeze": sum(r["freeze"] for r in rows),
            "unfreeze": sum(r["unfreeze"] for r in rows),
            "seize": sum(r["seize"] for r in rows),
            "seized_units": round(sum(r["seized_units"] for r in rows), 2),
            "issuers": len([r for r in rows if r["freeze"] or r["seize"]]),
        },
        "issuers": rows,
        "by_chain": chain_rows,
        "monthly": [{"m": m, **v} for m, v in sorted(monthly.items())],
        "daily": daily,
        "spikes": spikes(events, days),
        "spike_rule": {"min": SPIKE_MIN, "mult": SPIKE_MULT},
        "events": events[:500],
    }


def collect(days: int, key: str | None, solana_state: Path = SOLANA_STATE) -> dict:
    now = int(datetime.now(timezone.utc).timestamp())
    since = now - days * 86400
    events, statuses, notes = [], [], []
    if key:
        ev, st, nt = collect_evm(since, key)
        events += ev; statuses += st; notes += nt
    else:
        notes.append("ETHERSCAN_API_KEY 없음 — 이더리움 계열 체인 제외")
    print("  트론 USDT 조회…")
    ev, st = freeze_tron.collect(since)
    events += ev; statuses.append(st)
    print(f"    {len(ev)}건 · {'성공' if st['ok'] else '실패: ' + st['note']}")
    print("  솔라나 조회…")
    ev, st = freeze_solana.collect(since, solana_state)
    events += ev; statuses += st
    for s_ in st:
        print(f"    {s_['symbol']:6s} {'성공' if s_['ok'] else '실패'} · 완료 {s_.get('complete')} · "
              f"연 거래 {s_.get('opened', 0)} · {s_.get('note', '')}")
    return summarize(events, statuses, days, notes)


def probe(key: str):
    print("이벤트 시그니처 → topic0")
    for spec in ISSUERS:
        print(f"\n{spec['symbol']} {spec['address']} (verified={spec['verified']})")
        for kind, sigs in spec["events"].items():
            for sig in sigs:
                print(f"  {KIND_KO[kind]:4s} {sig:36s} {topic0(sig)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90, help="조회 기간(일) — 화면 시간축이 촘촘해지지 않게 3개월(2026-10-08)")
    ap.add_argument("--out", default="site/data")
    ap.add_argument("--probe", action="store_true", help="시그니처와 topic0만 출력")
    ap.add_argument("--solana-state", default=None, help="솔라나 증분 수집 상태 파일(기본: <out>/freeze_state_solana.json)")
    args = ap.parse_args()

    key = api_key()
    if args.probe:
        probe(key or "")
        return
    if not key:
        print("ETHERSCAN_API_KEY 가 없어 이더리움 계열 체인은 건너뜁니다(트론·솔라나만).", file=sys.stderr)

    print(f"최근 {args.days}일 동결·소각 조치 수집…")
    state = Path(args.solana_state) if args.solana_state else Path(args.out) / "freeze_state_solana.json"
    data = collect(args.days, key, state)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "freeze.json").write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    t = data["totals"]
    print(f"\n완료 — 동결 {t['freeze']}건 / 해제 {t['unfreeze']}건 / 소각 {t['seize']}건")
    for r in data["by_chain"]:
        print(f"  {r['chain']:9s} {r['symbol']:6s} 동결 {r['freeze']:4d} 해제 {r['unfreeze']:4d} 소각 {r['seize']:4d}"
              f" · {'정상' if r['ok'] else '실패'}{'' if r['complete'] else ' · 채우는 중'} {r.get('note', '')}")
    for n in data["meta"]["notes"]:
        print(f"  참고: {n}", file=sys.stderr)


if __name__ == "__main__":
    main()
