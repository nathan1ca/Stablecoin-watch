"""시험용(머지하지 않음): USDS·USD1·USDG·RLUSD 이체 건수를 무료로 셀 수 있는지 확인."""
import json, os, sys, time
from urllib.request import Request, urlopen
from urllib.parse import urlencode

TOKENS = {  # 이더리움 컨트랙트
    "USDS": "0xdC035D45d973E3EC169d2276DDab16f1e407384F",
    "USD1": "0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d",
    "USDG": "0xe343167631d89B6Ffc58B88d6b7fB0228795491D",
    "RLUSD": "0x8292Bb45bf1Ee4d140127049757C2E0fF06317eD",
}
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
UA = {"User-Agent": "stablecoin-watch-probe/0.1", "Accept": "application/json"}

def get(url):
    with urlopen(Request(url, headers=UA), timeout=40) as r:
        return json.loads(r.read().decode())

print("== 1) Blockscout 이더리움 토큰 카운터(누적 이체 수)")
for s, a in TOKENS.items():
    for path in (f"/api/v2/tokens/{a}", f"/api/v2/tokens/{a}/counters"):
        try:
            d = get("https://eth.blockscout.com" + path)
            keep = {k: d.get(k) for k in ("name", "symbol", "holders_count", "holders", "transfers_count", "token_holders_count", "total_supply") if k in d}
            print(f"  {s} {path.split('/')[-1]}: {keep}")
        except Exception as e:
            print(f"  {s} {path}: 실패 {e}")
        time.sleep(0.5)

print("== 2) Blockscout 다른 체인(카운터 엔드포인트 존재 여부)")
for host, s, a in [("base.blockscout.com", "USDS(Base)", "0x820C137fa70C8691f0e44Dc420a5e53c168921Dc"),
                   ("arbitrum.blockscout.com", "USDS(Arb)", "0x6491c05A82219b8D1479057361ff1654749b876b")]:
    try:
        d = get(f"https://{host}/api/v2/tokens/{a}/counters"); print(f"  {s}: {d}")
    except Exception as e:
        print(f"  {s}: 실패 {e}")

key = os.environ.get("ETHERSCAN_API_KEY")
print("== 3) Etherscan getLogs 로 최근 24시간(약 7,200블록) 이체 수 직접 세기", "키 있음" if key else "키 없음")
if key:
    tip = int(get("https://api.etherscan.io/v2/api?" + urlencode({"chainid": 1, "module": "proxy", "action": "eth_blockNumber", "apikey": key}))["result"], 16)
    for s, a in TOKENS.items():
        n, page, t0 = 0, 1, time.time()
        while page <= 30:
            q = {"chainid": 1, "module": "logs", "action": "getLogs", "address": a, "topic0": TRANSFER,
                 "fromBlock": tip - 7200, "toBlock": tip, "page": page, "offset": 1000, "apikey": key}
            r = get("https://api.etherscan.io/v2/api?" + urlencode(q)); time.sleep(0.25)
            res = r.get("result") if isinstance(r.get("result"), list) else []
            n += len(res)
            if len(res) < 1000:
                if not res and page == 1: print(f"    {s} 응답: {str(r)[:160]}")
                break
            page += 1
        print(f"  {s}: 24시간 이체 {n}건 (페이지 {page}, {time.time()-t0:.1f}초){' — 상한 도달' if page > 30 else ''}")

print("== 4) XRPL(RLUSD) 공개 API 후보")
for url in ["https://api.xrpscan.com/api/v1/account/rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De",
            "https://data.ripple.com/v2/accounts/rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"]:
    try:
        d = get(url); print(f"  {url.split('/')[2]}: {str(d)[:300]}")
    except Exception as e:
        print(f"  {url.split('/')[2]}: 실패 {e}")
