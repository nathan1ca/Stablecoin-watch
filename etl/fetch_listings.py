#!/usr/bin/env python3
"""
스테이블코인 감시 - 국내 거래소 거래지원 현황

국내 원화마켓 거래소 5곳(업비트·빗썸·코인원·디지털엑스·고팍스)의 공개 마켓 목록을
받아, 대시보드에 나오는 스테이블코인이 어느 거래소의 어느 마켓(KRW·BTC·USDT)
에서 거래되는지 정리한다. API 키가 필요 없다.

거래지원 시작·종료는 직전 수집분(site/data/listings.json)과 비교해 이벤트로
남긴다. 거래소 API 가 실패한 회차에는 그 거래소의 '종료'를 기록하지 않는다.

    python etl/fetch_listings.py --probe   # 거래소별 응답 형식·스테이블코인 매칭 확인
    python etl/fetch_listings.py           # site/data/listings.json 생성

출처(전부 무인증 공개 API)
  업비트  https://api.upbit.com/v1/market/all
  빗썸    https://api.bithumb.com/v1/market/all (실패 시 public/ticker/ALL_{KRW,BTC,USDT})
  코인원  https://api.coinone.co.kr/public/v2/markets/{KRW}
  디지털엑스(옛 코빗) https://api.korbit.co.kr/v2/currencyPairs (실패 시 v1/ticker/detailed/all)
            — 2026-08 사명 변경 후에도 2026-10-07 기준 옛 API 도메인이 응답한다.
  고팍스  https://api.gopax.co.kr/trading-pairs
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.http import UA, get_json  # noqa: E402

CONFIG_PATH = Path(__file__).resolve().parent / "kr_exchanges.json"
MAX_EVENTS = 80


# ── 거래소별 마켓 목록 → [(base, quote, extra)] ─────────────────────────────
def _pairs_from_upbit_style(rows) -> list[tuple[str, str, dict]]:
    """'KRW-BTC' 형식(호가통화-기준통화). 업비트·빗썸 v1 이 같은 모양이다."""
    out = []
    for m in rows or []:
        code = str(m.get("market") or "")
        if "-" not in code:
            continue
        quote, base = code.split("-", 1)
        ev = m.get("market_event") or {}
        warning = bool(ev.get("warning")) or str(m.get("market_warning") or "").upper() == "CAUTION"
        name = m.get("korean_name") or m.get("english_name") or ""
        out.append((base.upper(), quote.upper(), {"warning": warning, "name": str(name)}))
    return out


def fetch_upbit() -> list[tuple[str, str, dict]]:
    return _pairs_from_upbit_style(get_json("https://api.upbit.com/v1/market/all?isDetails=true", timeout=20))


def fetch_bithumb() -> list[tuple[str, str, dict]]:
    try:
        rows = get_json("https://api.bithumb.com/v1/market/all?isDetails=true", timeout=20)
        pairs = _pairs_from_upbit_style(rows)
        if pairs:
            return pairs
    except RuntimeError as e:
        print(f"  빗썸 v1 실패({e}) — public/ticker 로 대체", file=sys.stderr)
    out = []
    for quote in ("KRW", "BTC", "USDT"):
        try:
            r = get_json(f"https://api.bithumb.com/public/ticker/ALL_{quote}", timeout=20)
        except RuntimeError:
            continue
        data = r.get("data") if isinstance(r, dict) else None
        if isinstance(data, dict):
            out += [(k.upper(), quote, {}) for k, v in data.items() if k != "date" and isinstance(v, dict)]
    if not out:
        raise RuntimeError("빗썸 마켓 목록을 받지 못함")
    return out


def fetch_coinone() -> list[tuple[str, str, dict]]:
    out = []
    for quote in ("KRW",):
        r = get_json(f"https://api.coinone.co.kr/public/v2/markets/{quote}", timeout=20)
        for m in (r.get("markets") if isinstance(r, dict) else None) or []:
            base = str(m.get("target_currency") or "").upper()
            q = str(m.get("quote_currency") or quote).upper()
            if base:
                # trade_status 가 0 이면 거래 중지로 본다(필드가 없으면 거래 중으로 둔다).
                if m.get("trade_status") in (0, "0"):
                    continue
                out.append((base, q, {}))
    if not out:
        raise RuntimeError("코인원 마켓 목록이 비어 있음")
    return out


def fetch_digitalx() -> list[tuple[str, str, dict]]:
    out = []
    try:
        r = get_json("https://api.korbit.co.kr/v2/currencyPairs", timeout=20)
        rows = r.get("data") if isinstance(r, dict) else r
        for m in rows or []:
            sym = str((m or {}).get("symbol") or "")
            status = str((m or {}).get("status") or "launched").lower()
            if "_" in sym and status not in ("delisted", "stopped"):
                base, quote = sym.split("_", 1)
                out.append((base.upper(), quote.upper(), {}))
    except RuntimeError as e:
        print(f"  디지털엑스 v2 실패({e}) — v1 으로 대체", file=sys.stderr)
    if out:
        return out
    r = get_json("https://api.korbit.co.kr/v1/ticker/detailed/all", timeout=20)
    if isinstance(r, dict):
        for sym in r:
            if "_" in sym:
                base, quote = sym.split("_", 1)
                out.append((base.upper(), quote.upper(), {}))
    if not out:
        raise RuntimeError("디지털엑스 마켓 목록이 비어 있음")
    return out


def fetch_gopax() -> list[tuple[str, str, dict]]:
    r = get_json("https://api.gopax.co.kr/trading-pairs", timeout=20)
    out = []
    for m in r or []:
        base = str(m.get("baseAsset") or "").upper()
        quote = str(m.get("quoteAsset") or "").upper()
        if not (base and quote) and "-" in str(m.get("name") or ""):
            base, quote = [s.upper() for s in str(m["name"]).split("-", 1)]
        if base and quote:
            out.append((base, quote, {}))
    if not out:
        raise RuntimeError("고팍스 마켓 목록이 비어 있음")
    return out


FETCHERS = {
    "upbit": fetch_upbit,
    "bithumb": fetch_bithumb,
    "coinone": fetch_coinone,
    "digitalx": fetch_digitalx,
    "gopax": fetch_gopax,
}


# ── 가공 ──────────────────────────────────────────────────────────────────
def load_config(path: Path | str | None = None) -> dict:
    p = Path(path) if path else CONFIG_PATH
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        print(f"  kr_exchanges.json 읽기 실패({e}) — 기본 5개 거래소", file=sys.stderr)
        cfg = {}
    ex = cfg.get("exchanges") or [{"id": k, "name": k} for k in FETCHERS]
    return {
        "exchanges": [e for e in ex if isinstance(e, dict) and e.get("id")],
        "deny": {str(s).upper() for s in cfg.get("deny_symbols") or []},
        "aliases": {str(k).upper(): str(v).upper() for k, v in (cfg.get("aliases") or {}).items()},
    }


def tracked_symbols(snapshot: dict | None) -> set[str]:
    """대시보드에 실제로 나오는 스테이블코인 심볼(상위 목록 + 감시목록)."""
    if not snapshot:
        return set()
    syms = {str(a.get("symbol") or "").upper() for a in snapshot.get("assets") or []}
    for r in ((snapshot.get("watchlist") or {}).get("rows") or []):
        syms.add(str(r.get("symbol") or "").upper())
    syms.discard("")
    return syms


def build_listings(raw: dict[str, list | Exception], cfg: dict, tracked: set[str],
                   prev: dict | None = None, today: str | None = None) -> dict:
    """거래소별 마켓 목록 → 스테이블코인별 거래지원 현황과 시작·종료 이벤트.

    raw: {거래소 id: [(base, quote, extra)] 또는 수집 중 난 예외}
    """
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prev = prev or {}
    prev_seen: dict[str, str] = dict(prev.get("first_seen") or {})
    baseline = not prev_seen

    ex_meta, assets, seen_now, ok_ids = [], {}, {}, set()
    for ex in cfg["exchanges"]:
        eid = ex["id"]
        got = raw.get(eid)
        if isinstance(got, Exception) or got is None:
            ex_meta.append({**ex, "status": "fail", "error": str(got)[:200] if got else "수집 안 됨"})
            # 일시 장애로 표가 흔들리지 않게 직전 상태를 '이전 값'으로 표시해 이어 간다.
            for sym, a in (prev.get("assets") or {}).items():
                slot = (a.get("exchanges") or {}).get(eid)
                if slot and sym in tracked:
                    assets.setdefault(sym, {})[eid] = {**slot, "stale": True}
            continue
        ok_ids.add(eid)
        n_match = 0
        for base, quote, extra in got:
            sym = cfg["aliases"].get(base, base)
            if sym in cfg["deny"] or sym not in tracked:
                continue
            n_match += 1
            slot = assets.setdefault(sym, {}).setdefault(eid, {"markets": [], "warning": False})
            if quote not in slot["markets"]:
                slot["markets"].append(quote)
            slot["warning"] = slot["warning"] or bool((extra or {}).get("warning"))
            key = f"{eid}:{sym}:{quote}"
            seen_now[key] = prev_seen.get(key, today)
        ex_meta.append({**ex, "status": "ok", "market_count": len(got), "stable_markets": n_match})

    # 마켓 표기 순서: KRW → BTC → USDT → 그 밖
    order = {"KRW": 0, "BTC": 1, "USDT": 2}
    for sym, by_ex in assets.items():
        for slot in by_ex.values():
            slot["markets"].sort(key=lambda q: (order.get(q, 9), q))

    # 이벤트: 새로 보인 마켓 = 거래지원 시작, 사라진 마켓 = 종료(해당 거래소 수집 성공 시에만)
    events = list(prev.get("events") or [])
    if not baseline:
        for key in seen_now.keys() - prev_seen.keys():
            eid, sym, quote = key.split(":")
            events.append({"date": today, "exchange": eid, "symbol": sym, "market": quote, "kind": "listed"})
        for key in prev_seen.keys() - seen_now.keys():
            eid, sym, quote = key.split(":")
            if eid in ok_ids:
                events.append({"date": today, "exchange": eid, "symbol": sym, "market": quote, "kind": "delisted"})
            else:
                seen_now[key] = prev_seen[key]  # 수집 실패 거래소는 직전 상태를 이어 간다
    events.sort(key=lambda e: e["date"], reverse=True)

    summary = {
        sym: {
            "exchanges": by_ex,
            "count": len(by_ex),
            "krw_count": sum(1 for s in by_ex.values() if "KRW" in s["markets"]),
        }
        for sym, by_ex in sorted(assets.items(), key=lambda x: (-len(x[1]), x[0]))
    }
    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "is_sample": False,
            "exchanges": ex_meta,
            "tracked_count": len(tracked),
            "baseline": baseline,
            "source": "업비트·빗썸·코인원·디지털엑스·고팍스 공개 마켓 API",
            "note": "거래소 티커와 DefiLlama 심볼이 같으면 같은 종목으로 본다(동명 티커 위험이 큰 심볼은 제외). "
                    "거래지원 시작·종료는 이 수집이 처음 본 날짜 기준이며, 거래소 공지일과 다를 수 있다.",
        },
        "assets": summary,
        "events": events[:MAX_EVENTS],
        "first_seen": dict(sorted(seen_now.items())),
    }


# ── 거래소 아이콘 ─────────────────────────────────────────────────────────
# 각 거래소 웹사이트가 공식으로 내놓는 아이콘(파비콘)을 한 번 받아 site/data/ex-icons/ 에
# 저장한다. 화면은 이 사본만 쓰므로 방문자 브라우저가 거래소 서버에 접속하지 않는다.
# 받지 못하면 화면이 거래소 머리글자 배지로 대신한다. SVG 는 받지 않는다(같은 출처에서
# 직접 열면 스크립트가 돌 수 있어서).
ICON_DIRNAME = "ex-icons"
ICON_MAX_BYTES = 200_000
_LINK_RE = re.compile(r"<link\b[^>]*>", re.I)
_ATTR_RE = re.compile(r"""([a-zA-Z-]+)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""")


def sniff_image(data: bytes) -> str | None:
    """바이트 머리로 형식을 판정해 확장자를 돌려준다(SVG·알 수 없는 형식은 None)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"\x00\x00\x01\x00":
        return ".ico"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return None


def find_icon_links(html: str, base: str) -> list[str]:
    """홈페이지 HTML 의 <link rel=icon|apple-touch-icon> 주소를 큰 것부터 돌려준다."""
    found = []
    for tag in _LINK_RE.findall(html or ""):
        attrs = {k.lower(): v.strip("\"'") for k, v in _ATTR_RE.findall(tag)}
        rel = attrs.get("rel", "").lower()
        href = attrs.get("href")
        if not href or "icon" not in rel or "mask-icon" in rel or href.lower().endswith(".svg"):
            continue
        sizes = [int(n) for n in re.findall(r"(\d+)x\d+", attrs.get("sizes", ""))]
        score = (1 if "apple-touch-icon" in rel else 0, max(sizes) if sizes else 0)
        found.append((score, urljoin(base, href)))
    found.sort(key=lambda x: x[0], reverse=True)
    out = []
    for _, u in found:
        if u not in out:
            out.append(u)
    return out


def fetch_bytes(url: str, timeout: int = 15, limit: int = ICON_MAX_BYTES) -> bytes:
    req = Request(url, headers={"User-Agent": UA, "Accept": "image/*,text/html;q=0.9,*/*;q=0.5"})
    with urlopen(req, timeout=timeout) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError(f"{len(data)}바이트 초과")
    return data


def appstore_icon_urls(ex: dict, log: list | None = None) -> list[str]:
    """애플 앱스토어 검색 API(무인증)에서 거래소 공식 앱 아이콘 주소를 찾는다.
    판매자(sellerName)가 appstore_seller 중 하나를 포함할 때만 쓴다(동명 앱 오인 방지)."""
    term, sellers = ex.get("appstore_term"), [x.lower() for x in ex.get("appstore_seller") or []]
    if not term or not sellers:
        return []
    from urllib.parse import quote
    try:
        r = get_json(f"https://itunes.apple.com/search?term={quote(term)}&country=kr&entity=software&limit=10",
                     retries=2, timeout=15)
    except RuntimeError as e:
        if log is not None:
            log.append(f"앱스토어 검색 실패({e})")
        return []
    out = []
    for app in (r or {}).get("results") or []:
        seller = str(app.get("sellerName") or app.get("artistName") or "")
        hit = any(sv in seller.lower() for sv in sellers)
        if log is not None:
            log.append(f"앱스토어 후보: {app.get('trackName')} / {seller}{' ← 채택' if hit else ''}")
        if hit:
            u = app.get("artworkUrl100") or app.get("artworkUrl60")
            if u:
                out.append(u)
            break
    return out


def icon_candidates(ex: dict, log: list | None = None) -> list[str]:
    base = str(ex.get("url") or "").rstrip("/") + "/"
    cands = list(ex.get("icon_urls") or [])
    if ex.get("icon_source") == "appstore":
        return list(dict.fromkeys(cands + appstore_icon_urls(ex, log)))
    if ex.get("url"):
        try:
            html = fetch_bytes(base, limit=1_500_000).decode("utf-8", "replace")
            cands += find_icon_links(html, base)
        except (HTTPError, URLError, TimeoutError, RuntimeError, OSError) as e:
            if log is not None:
                log.append(f"홈페이지 읽기 실패({e})")
        cands += [urljoin(base, "apple-touch-icon.png"), urljoin(base, "favicon.ico")]
    # 홈페이지 후보가 모두 실패할 때를 위해 앱스토어 공식 앱 아이콘을 맨 뒤에 둔다(설정이 있을 때만).
    if ex.get("appstore_term"):
        cands += appstore_icon_urls(ex, log)
    seen, out = set(), []
    for u in cands:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def download_icon(ex: dict, log: list | None = None) -> tuple[bytes, str, str] | None:
    """(바이트, 확장자, 원본 주소) 또는 None."""
    for url in icon_candidates(ex, log):
        try:
            data = fetch_bytes(url)
        except (HTTPError, URLError, TimeoutError, RuntimeError, OSError) as e:
            if log is not None:
                log.append(f"{url} 실패({e})")
            continue
        ext = sniff_image(data)
        if ext and len(data) >= 100:
            if log is not None:
                log.append(f"{url} → {ext} {len(data)}바이트")
            return data, ext, url
        if log is not None:
            log.append(f"{url} 이미지 아님({len(data)}바이트)")
    return None


def ensure_icons(cfg: dict, out: Path, refresh: bool = False) -> dict[str, str]:
    """거래소별 아이콘 사본 경로(site 기준 상대 경로). 이미 있으면 다시 받지 않는다."""
    d = out / ICON_DIRNAME
    paths = {}
    for ex in cfg["exchanges"]:
        have = sorted(d.glob(f"{ex['id']}.*")) if d.exists() else []
        if have and not refresh:
            paths[ex["id"]] = f"data/{ICON_DIRNAME}/{have[0].name}"
            continue
        got = download_icon(ex)
        if not got:
            print(f"  {ex.get('name', ex['id'])}: 아이콘 없음 — 머리글자 배지로 표시", file=sys.stderr)
            continue
        data, ext, src = got
        d.mkdir(parents=True, exist_ok=True)
        for old in have:
            old.unlink()
        (d / f"{ex['id']}{ext}").write_bytes(data)
        paths[ex["id"]] = f"data/{ICON_DIRNAME}/{ex['id']}{ext}"
        print(f"  {ex.get('name', ex['id'])}: 아이콘 저장 ({src})")
    return paths


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def collect(cfg: dict) -> dict:
    raw = {}
    for ex in cfg["exchanges"]:
        fn = FETCHERS.get(ex["id"])
        if not fn:
            raw[ex["id"]] = RuntimeError("수집기 없음")
            continue
        try:
            raw[ex["id"]] = fn()
            print(f"  {ex.get('name', ex['id'])}: 마켓 {len(raw[ex['id']])}개")
        except Exception as e:  # 한 거래소 장애가 나머지를 막지 않게
            raw[ex["id"]] = e
            print(f"  {ex.get('name', ex['id'])}: 실패 — {e}", file=sys.stderr)
    return raw


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/data")
    ap.add_argument("--probe", action="store_true", help="쓰지 않고 결과만 출력")
    ap.add_argument("--refresh-icons", action="store_true", help="거래소 아이콘을 다시 받음")
    args = ap.parse_args()
    out = Path(args.out)

    cfg = load_config()
    snap = read_json(out / "snapshot.json")
    tracked = tracked_symbols(snap)
    print(f"대상 스테이블코인 {len(tracked)}종")
    raw = collect(cfg)
    res = build_listings(raw, cfg, tracked, read_json(out / "listings.json"))

    if args.probe:
        # 스테이블코인으로 보이는 티커 전체(대상 밖 포함)도 보여 줘서 매칭 누락을 눈으로 잡는다.
        hint = ("USD", "EUR", "KRW", "JPY", "DAI")
        for eid, got in raw.items():
            if isinstance(got, Exception):
                continue
            cands = sorted({f"{b}({x.get('name')})" if x.get("name") else b
                            for b, q, x in got if any(h in b for h in hint)})
            print(f"  [{eid}] 스테이블코인 후보 티커: {', '.join(cands) or '없음'}")
        for e in res["meta"]["exchanges"]:
            print(f"  {e['name']}: {e['status']} 마켓 {e.get('market_count', '-')} · 대상 매칭 {e.get('stable_markets', '-')}"
                  + (f" · {e.get('error')}" if e.get("error") else ""))
        for sym, a in res["assets"].items():
            cells = "  ".join(f"{k}={'/'.join(v['markets'])}{'!' if v.get('warning') else ''}" for k, v in a["exchanges"].items())
            print(f"  {sym:<8} {a['count']}곳  {cells}")
        print("  -- 아이콘 후보 --")
        for ex in cfg["exchanges"]:
            log: list[str] = []
            got = download_icon(ex, log)
            print(f"  {ex.get('name')}: {'성공' if got else '실패'}")
            for line in log:
                print(f"     {line}")
        return

    out.mkdir(parents=True, exist_ok=True)
    try:
        icons = ensure_icons(cfg, out, refresh=args.refresh_icons)
    except Exception as e:  # 아이콘은 꾸밈 — 실패해도 거래지원 데이터는 쓴다
        print(f"  아이콘 단계 실패({e})", file=sys.stderr)
        icons = {}
    for e in res["meta"]["exchanges"]:
        if e["id"] in icons:
            e["icon"] = icons[e["id"]]
    (out / "listings.json").write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    ok = sum(1 for e in res["meta"]["exchanges"] if e["status"] == "ok")
    print(f"완료 — 거래소 {ok}/{len(res['meta']['exchanges'])}곳 수집, 국내 거래 스테이블코인 {len(res['assets'])}종, "
          f"이벤트 {len(res['events'])}건")


if __name__ == "__main__":
    main()
