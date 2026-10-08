#!/usr/bin/env python3
"""솔라나 동결 조치를 무료 공개 RPC 로 수집할 수 있는지 시험(임시, 쓰기 없음).

방법: 민트(토큰) 계정에서 freezeAuthority(동결 권한 주소)를 체인에서 직접 읽고,
그 주소가 서명에 참여한 거래 목록(getSignaturesForAddress)을 받아 SPL 토큰
FreezeAccount/ThawAccount 명령만 센다. 민트 주소 전체를 훑는 것보다 거래 수가 훨씬 적다.
"""
import json, sys, time
from collections import Counter
from datetime import datetime, timezone
from urllib.request import Request, urlopen

RPCS = ["https://api.mainnet-beta.solana.com"]
MINTS = {
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "PYUSD": "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",
}

def rpc(url, method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for a in range(4):
        try:
            req = Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "stablecoin-watch-probe"})
            with urlopen(req, timeout=40) as r:
                j = json.loads(r.read())
            if "error" in j:
                raise RuntimeError(j["error"])
            return j["result"]
        except Exception as e:
            last = e
            time.sleep(2 * (a + 1))
    raise RuntimeError(f"{method} 실패: {last}")

def main():
    url = RPCS[0]
    calls = 0
    for sym, mint in MINTS.items():
        print(f"\n== {sym} {mint}")
        try:
            info = rpc(url, "getAccountInfo", [mint, {"encoding": "jsonParsed"}]); calls += 1
            parsed = info["value"]["data"]["parsed"]["info"]
            fa = parsed.get("freezeAuthority"); prog = info["value"]["owner"]
            print(f"  program={prog} freezeAuthority={fa} supply={parsed.get('supply')} decimals={parsed.get('decimals')}")
        except Exception as e:
            print(f"  민트 조회 실패: {e}"); continue
        if not fa:
            print("  동결 권한 없음"); continue
        sigs, before, pages = [], None, 0
        while pages < 10:
            p = {"limit": 1000}
            if before: p["before"] = before
            try:
                page = rpc(url, "getSignaturesForAddress", [fa, p]); calls += 1
            except Exception as e:
                print(f"  서명 목록 실패(페이지 {pages+1}): {e}"); break
            pages += 1
            if not page: break
            sigs += page; before = page[-1]["signature"]
            if len(page) < 1000: break
            time.sleep(0.5)
        if not sigs:
            print("  서명 없음"); continue
        ts = [s.get("blockTime") for s in sigs if s.get("blockTime")]
        f = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")
        print(f"  서명 {len(sigs)}건 ({pages}쪽) · 기간 {f(min(ts))} ~ {f(max(ts))} · 실패거래 {sum(1 for s in sigs if s.get('err'))}")
        # 최근 40건 거래 내용을 열어 명령 종류를 센다
        kinds = Counter(); sample = []
        for s in sigs[:40]:
            try:
                tx = rpc(url, "getTransaction", [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]); calls += 1
            except Exception as e:
                kinds["조회실패"] += 1; continue
            ins = (tx or {}).get("transaction", {}).get("message", {}).get("instructions", [])
            inner = [i for g in ((tx or {}).get("meta") or {}).get("innerInstructions") or [] for i in g.get("instructions", [])]
            for i in ins + inner:
                t = (i.get("parsed") or {}).get("type") if isinstance(i.get("parsed"), dict) else None
                if t: kinds[f"{i.get('program')}:{t}"] += 1
                if t in ("freezeAccount", "thawAccount") and len(sample) < 3:
                    sample.append((f(s['blockTime']) if s.get('blockTime') else '?', t, i["parsed"]["info"].get("account")))
            time.sleep(0.3)
        print(f"  최근 40건 명령 종류: {dict(kinds.most_common(8))}")
        for x in sample: print(f"    예: {x}")
    print(f"\n총 RPC 호출 {calls}회")

if __name__ == "__main__":
    main()
