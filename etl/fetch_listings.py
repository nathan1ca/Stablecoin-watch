#!/usr/bin/env python3
"""
스테이블코인 감시 - 국내 거래소 거래지원 현황

국내 원화마켓 거래소 5곳(업비트·빗썸·코인원·코빗·고팍스)의 공개 마켓 목록을
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
  코빗    https://api.korbit.co.kr/v2/currencyPairs (실패 시 v1/ticker/detailed/all)
  고팍스  https://api.gopax.co.kr/trading-pairs
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.http import get_json  # noqa: E402

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


def fetch_korbit() -> list[tuple[str, str, dict]]:
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
        print(f"  코빗 v2 실패({e}) — v1 으로 대체", file=sys.stderr)
    if out:
        return out
    r = get_json("https://api.korbit.co.kr/v1/ticker/detailed/all", timeout=20)
    if isinstance(r, dict):
        for sym in r:
            if "_" in sym:
                base, quote = sym.split("_", 1)
                out.append((base.upper(), quote.upper(), {}))
    if not out:
        raise RuntimeError("코빗 마켓 목록이 비어 있음")
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
    "korbit": fetch_korbit,
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
            "source": "업비트·빗썸·코인원·코빗·고팍스 공개 마켓 API",
            "note": "거래소 티커와 DefiLlama 심볼이 같으면 같은 종목으로 본다(동명 티커 위험이 큰 심볼은 제외). "
                    "거래지원 시작·종료는 이 수집이 처음 본 날짜 기준이며, 거래소 공지일과 다를 수 있다.",
        },
        "assets": summary,
        "events": events[:MAX_EVENTS],
        "first_seen": dict(sorted(seen_now.items())),
    }


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
        return

    out.mkdir(parents=True, exist_ok=True)
    (out / "listings.json").write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    ok = sum(1 for e in res["meta"]["exchanges"] if e["status"] == "ok")
    print(f"완료 — 거래소 {ok}/{len(res['meta']['exchanges'])}곳 수집, 국내 거래 스테이블코인 {len(res['assets'])}종, "
          f"이벤트 {len(res['events'])}건")


if __name__ == "__main__":
    main()
