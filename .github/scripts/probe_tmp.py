#!/usr/bin/env python3
"""임시 확인 스크립트(PR 머지 전 삭제). 표준 라이브러리 + CI 의 pdftotext."""
import json, os, re, subprocess, sys, time, tempfile
from collections import Counter, defaultdict
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

UA = "Mozilla/5.0 (X11; Linux x86_64) stablecoin-watch-probe"

def get(url, t=30, binary=False):
    r = urlopen(Request(url, headers={"User-Agent": UA, "Accept": "*/*"}), timeout=t)
    b = r.read()
    return b if binary else b.decode("utf-8", "replace")

def section(t): print(f"\n===== {t} =====", flush=True)

# ── 1. ETH 코너 흐름 진단 ────────────────────────────
def eth():
    key = os.environ.get("ETHERSCAN_API_KEY")
    if not key:
        print("키 없음"); return
    def call(p):
        p = {**p, "chainid": 1, "apikey": key}
        for _ in range(3):
            try:
                d = json.loads(get("https://api.etherscan.io/v2/api?" + urlencode(p)))
                time.sleep(0.3)
                return d
            except Exception as e:
                print(" 재시도", e); time.sleep(2)
        return {}
    KR = {"0x390de26d772d2e2005c6d1d24afc902bae37a4bb": "Upbit 1", "0xba826fec90cefdf6706858e5fbafcb27a290fbe0": "Upbit 2",
          "0x5e032243d507c743b061ef021e2ec7fcc6d3ab89": "Upbit 3", "0xc9cf0ec93d764f5c9571fd12f764bae7fc87c84e": "Upbit Cold",
          "0x17e5545b11b468072283cee1f066a059fb0dbf24": "Bithumb Hot"}
    TOK = {"USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7", "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"}
    now = time.time()
    out_cp = defaultdict(float)
    for addr, name in KR.items():
        for sym, c in TOK.items():
            d = call({"module": "account", "action": "tokentx", "address": addr, "contractaddress": c,
                      "page": 1, "offset": 1000, "sort": "desc"})
            rows = d.get("result") if isinstance(d.get("result"), list) else []
            if not rows:
                print(f"{name} {sym}: 0건 ({str(d.get('message'))[:60]} {str(d.get('result'))[:80]})"); continue
            span = (now - int(rows[-1]["timeStamp"])) / 86400
            ins = [r for r in rows if r["to"].lower() == addr]; outs = [r for r in rows if r["from"].lower() == addr]
            vol = lambda rs: sum(int(r["value"]) / 1e6 for r in rs)
            print(f"{name} {sym}: 최근 {len(rows)}건이 {span:.1f}일치 · 입금 {len(ins)}건 ${vol(ins):,.0f} · 출금 {len(outs)}건 ${vol(outs):,.0f}")
            cp = Counter()
            for r in outs:
                cp[r["to"].lower()] += int(r["value"]) / 1e6
                if r["to"].lower() not in KR: out_cp[r["to"].lower()] += int(r["value"]) / 1e6
            for a, v in cp.most_common(4):
                print(f"    출금 상대 {a} ${v:,.0f}{' (국내 자체지갑)' if a in KR else ''}")
            cpi = Counter()
            for r in ins: cpi[r["from"].lower()] += int(r["value"]) / 1e6
            for a, v in cpi.most_common(3):
                print(f"    입금 상대 {a} ${v:,.0f}{' (국내 자체지갑)' if a in KR else ''}")
    # 2단계: 출금 상대 상위 12곳이 그 돈을 어디로 넘기는지(해외 거래소 입금주소 → 해외 핫월렛 쓸어담기 확인)
    section("2단계 추적(국내 출금 상대 → 다음 행선지)")
    for a, v in sorted(out_cp.items(), key=lambda x: -x[1])[:12]:
        d = call({"module": "account", "action": "tokentx", "address": a, "page": 1, "offset": 40, "sort": "desc"})
        rows = d.get("result") if isinstance(d.get("result"), list) else []
        nxt = Counter(r["to"].lower() for r in rows if r["from"].lower() == a)
        print(f"{a} (국내에서 ${v:,.0f} 받음) → 전송 {sum(nxt.values())}건, 주요 행선지: {nxt.most_common(3)}")

# ── 2. 국내 원화마켓 스테이블코인 일별 거래대금 ─────────────
def candles():
    for m in ("KRW-USDT", "KRW-USDC"):
        try:
            rows = json.loads(get(f"https://api.upbit.com/v1/candles/days?market={m}&count=200"))
            print(f"업비트 {m}: {len(rows)}일, 최근 {rows[0]['candle_date_time_kst'][:10]} 종가 {rows[0]['trade_price']} 거래대금 ₩{rows[0]['candle_acc_trade_price']:,.0f}, 가장 오래된 {rows[-1]['candle_date_time_kst'][:10]}")
        except Exception as e: print("업비트", m, e)
        try:
            rows = json.loads(get(f"https://api.bithumb.com/v1/candles/days?market={m}&count=200"))
            print(f"빗썸 {m}: {len(rows)}일, 최근 {rows[0].get('candle_date_time_kst','')[:10]} 종가 {rows[0].get('trade_price')} 거래대금 ₩{float(rows[0].get('candle_acc_trade_price') or 0):,.0f}")
        except Exception as e: print("빗썸", m, e)
    for base in ("USDT", "USDC"):
        try:
            r = json.loads(get(f"https://api.coinone.co.kr/public/v2/chart/KRW/{base}?interval=1d&size=5"))
            c = (r.get("chart") or [{}])[0]
            print(f"코인원 {base}: {r.get('result')} 최근 {c}")
        except Exception as e: print("코인원", base, e)

# ── 3. 어테스테이션 PDF 확인 ──────────────────────────
KW = re.compile(r"(circulat|outstanding|in circulation|issued|redeemable|fair value|total assets|total reserve|as of|report date|accountant|LLP|Limited)", re.I)
def pdf_lines(url, label, maxl=40):
    try:
        b = get(url, t=60, binary=True)
    except Exception as e:
        print(f"[{label}] 받기 실패 {e}"); return
    if not b.startswith(b"%PDF"):
        print(f"[{label}] PDF 아님 ({len(b)}바이트) {b[:80]!r}"); return
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(b); f.flush()
        txt = subprocess.run(["pdftotext", "-layout", f.name, "-"], capture_output=True, text=True).stdout
    hits = [ln.strip() for ln in txt.splitlines() if KW.search(ln) and re.search(r"\d", ln)]
    print(f"[{label}] {len(b)}바이트, 핵심 줄 {len(hits)}개")
    for ln in hits[:maxl]: print("   ", ln[:200])

def paxos(slug, label):
    try:
        html = get(f"https://www.paxos.com/{slug}")
    except Exception as e:
        print(label, "페이지 실패", e); return
    links = sorted(set(re.findall(r"https?://[^\s\"'<>]+?\.pdf", html.replace("\\/", "/"))))
    l26 = [u for u in links if "2026" in u]
    print(f"{label}: pdf 링크 {len(links)}개, 2026 {len(l26)}개")
    for u in l26[-6:]: print("   ", u)
    if l26:
        aug = [u for u in l26 if re.search(r"aug", u, re.I)] or l26[-1:]
        pdf_lines(aug[-1], f"{label} 최신")

def attest():
    paxos("pyusd-transparency", "PYUSD")
    paxos("usdg-transparency", "USDG")
    try:
        html = get("https://ripple.com/products/stablecoin/transparency/")
        links = sorted(set(re.findall(r"https://cdn\.sanity\.io/files/[^\s\"'<>]+?\.pdf", html.replace("\\/", "/"))))
        print(f"RLUSD: pdf 링크 {len(links)}개")
        for u in links[-4:]: print("   ", u)
    except Exception as e:
        print("RLUSD 페이지 실패", e); links = []
    pdf_lines("https://cdn.sanity.io/files/ior4a5y3/production/4981331c98a2bb203c0c9ab2584e8b2a0da80938.pdf", "RLUSD Aug")
    for label, u in [
        ("USDC Aug", "https://6778953.fs1.hubspotusercontent-na1.net/hubfs/6778953/USDCAttestationReports/2026/2026%20USDC_Examination%20Report%20August%2026.pdf"),
        ("EURC Aug", "https://6778953.fs1.hubspotusercontent-na1.net/hubfs/6778953/EURC%20Attestations/2026%20EURC_Examination%20Report%20August%2026.pdf"),
        ("USDT Q2", "https://assets.ctfassets.net/vyse88cgwfbl/2kYf7r64h3tzwiu6F0CbUB/2997abd2f11ecea74a21528048b50707/Opinion___Report_-_Tether_International_Financial_Figure_30-06-2026.pdf"),
        ("USD1 Aug", "https://landing.bitgo.com/rs/552-OGK-141/images/USD1_Reserve_Attestation_Report_August_2026.pdf"),
        ("FDUSD Aug", "https://cdn.prod.website-files.com/675ab99bf1f7ea944d49a55b/6aa8fb9c867332b394838288_Attestation%20Report%20on%20Reserves%20Account%20August%202026.pdf"),
        ("USDtb Aug", "https://learn.anchorage.com/08.31.26_USDtb_Stablecoin_Attestation_Report%20(FINAL)%20signed_9.28.26.pdf"),
        ("AUSD Aug", "https://docs.agora.finance/_fern-files/agora.docs.buildwithfern.com/444c4f3a9642d6a9149e8fa9736469cd03192f55cc7ff3d2326117b0397875b5/docs/assets/2026%20Aug%20-%20Agora%20Dollar%20Reserve%20Report.pdf"),
    ]:
        pdf_lines(u, label, 25)

if __name__ == "__main__":
    for name, fn in (("ETH 코너 진단", eth), ("국내 스테이블코인 거래대금", candles), ("어테스테이션", attest)):
        section(name)
        try: fn()
        except Exception as e: print("실패:", e)
