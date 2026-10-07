#!/usr/bin/env python3
"""
스테이블코인 감시 - 최근 24시간 뉴스·당국 발표 헤드라인

공개 RSS/Atom 피드(etl/news_feeds.json)에서 최근 24시간 항목의 제목·출처·발행 시각·
링크만 모은다. 기사 본문이나 요약문은 가져오지 않는다(저작권). 검색형 피드(Google
뉴스 검색)는 검색어로 이미 걸러져 있고, 당국·매체 전체 피드는 키워드로 거른다.

    python etl/fetch_news.py --probe   # 피드별 응답·건수만 확인 (파일 쓰기 없음)
    python etl/fetch_news.py           # site/data/news.json

표준 라이브러리만 쓴다. 한 피드가 실패해도 나머지는 계속 모은다.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.http import UA  # noqa: E402

FEEDS_PATH = Path(__file__).resolve().parent / "news_feeds.json"
ATOM = "{http://www.w3.org/2005/Atom}"
DC = "{http://purl.org/dc/elements/1.1/}"


# ── 피드 받기 ─────────────────────────────────────────────────
def fetch_bytes(url: str, retries: int = 2, timeout: int = 30) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
            })
            with urlopen(req, timeout=timeout) as r:
                return r.read(5_000_000)  # 피드 하나 최대 5MB
        except (HTTPError, URLError, TimeoutError) as e:
            last = e
            time.sleep(1 + attempt)
    raise RuntimeError(f"{last}")


# ── 파싱 ──────────────────────────────────────────────────────
def parse_date(s: str | None) -> datetime | None:
    """RSS(RFC 822)·Atom(ISO 8601) 날짜를 UTC aware datetime 으로. 실패하면 None."""
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
        if d is not None:
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _text(el) -> str:
    return (el.text or "").strip() if el is not None else ""


def clean(s: str) -> str:
    """태그·엔티티·연속 공백 제거."""
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def parse_feed(raw: bytes) -> list[dict]:
    """RSS 2.0 · RSS 1.0(RDF) · Atom 항목을 {title, link, published, source, desc} 목록으로."""
    root = ET.fromstring(raw)
    out = []
    # RSS 2.0 / RDF: 어디에 있든 item 을 찾는다(RDF 는 네임스페이스가 붙는다).
    items = [el for el in root.iter() if el.tag == "item" or el.tag.endswith("}item")]
    for it in items:
        def child(name):
            for c in it:
                if c.tag == name or c.tag.endswith("}" + name):
                    return c
            return None
        src = child("source")
        out.append({
            "title": clean(_text(child("title"))),
            "link": _text(child("link")),
            "published": parse_date(_text(child("pubDate")) or _text(child("date"))),
            "source": clean(_text(src)),
            "desc": clean(_text(child("description")))[:400],
        })
    if out:
        return out
    # Atom
    for e in root.iter(ATOM + "entry"):
        link = ""
        for l in e.findall(ATOM + "link"):
            if l.get("rel", "alternate") == "alternate":
                link = l.get("href", "")
                break
        out.append({
            "title": clean(_text(e.find(ATOM + "title"))),
            "link": link,
            "published": parse_date(_text(e.find(ATOM + "published")) or _text(e.find(ATOM + "updated"))),
            "source": "",
            "desc": clean(_text(e.find(ATOM + "summary")))[:400],
        })
    return out


# ── 거르기·정리 ───────────────────────────────────────────────
def matches(item: dict, keywords: list[str]) -> bool:
    hay = (item.get("title", "") + " " + item.get("desc", "")).lower()
    return any(k.lower() in hay for k in keywords)


def split_source(title: str, source: str) -> tuple[str, str]:
    """Google 뉴스 제목 끝의 ' - 언론사' 를 떼어 출처로 쓴다."""
    if source and title.endswith(" - " + source):
        return title[: -len(" - " + source)].strip(), source
    m = re.match(r"^(.*\S)\s+-\s+([^-]{2,40})$", title)
    if not source and m:
        return m.group(1), m.group(2).strip()
    return title, source


def norm_key(title: str) -> str:
    """중복 판정용 — 영숫자·한글만 남긴 소문자 제목."""
    return re.sub(r"[^0-9a-z가-힣]+", "", title.lower())


def collect(cfg: dict, now: datetime | None = None, fetch=fetch_bytes) -> dict:
    now = now or datetime.now(timezone.utc)
    window = int(cfg.get("window_hours", 24))
    since = now - timedelta(hours=window)
    keywords = cfg.get("keywords") or []
    cats = cfg.get("categories") or {}
    feeds_status, items, seen = [], [], set()

    for f in cfg.get("feeds", []):
        if not f.get("enabled", True):
            continue
        st = {"id": f["id"], "name": f.get("name", f["id"]), "category": f.get("category"),
              "ok": False, "count": 0, "error": ""}
        try:
            raw_items = parse_feed(fetch(f["url"]))
            st["ok"] = True
        except Exception as e:  # noqa: BLE001 — 한 피드 실패가 전체를 멈추지 않게
            st["error"] = str(e)[:160]
            feeds_status.append(st)
            print(f"  {f['id']}: 실패 — {st['error']}", file=sys.stderr)
            continue
        kept = 0
        cap = int(f.get("max", 10**6))
        for it in sorted(raw_items, key=lambda x: x["published"] or since, reverse=True):
            if kept >= cap:
                break
            pub = it["published"]
            if pub is None or pub < since or pub > now + timedelta(hours=1):
                continue
            if f.get("filter") and not matches(it, keywords):
                continue
            title, source = split_source(it["title"], it["source"])
            if not title or not it["link"].startswith(("http://", "https://")):
                continue
            key = norm_key(title)
            if key in seen:
                continue
            seen.add(key)
            items.append({
                "title": title,
                "link": it["link"],
                "source": source or f.get("name", f["id"]),
                "feed": f["id"],
                "category": f.get("category"),
                "published": pub.astimezone(timezone.utc).isoformat(timespec="seconds"),
            })
            st["count"] += 1
            kept += 1
        feeds_status.append(st)

    items.sort(key=lambda x: x["published"], reverse=True)
    items = items[: int(cfg.get("max_items", 80))]
    by_cat = {c: sum(1 for i in items if i["category"] == c) for c in cats}
    return {
        "meta": {
            "generated_at": now.isoformat(timespec="seconds"),
            "window_hours": window,
            "since": since.isoformat(timespec="seconds"),
            "is_sample": False,
            "categories": cats,
            "feeds": feeds_status,
            "note": "제목·출처·시각·링크만 모은다(본문·요약 미수집). 검색형 피드는 검색어로, "
                    "당국·매체 전체 피드는 키워드로 거른다. 같은 제목은 하나만 남긴다.",
        },
        "counts": by_cat,
        "items": items,
    }


def load_cfg(path: Path | str | None = None) -> dict:
    return json.loads(Path(path or FEEDS_PATH).read_text(encoding="utf-8"))


def probe(cfg: dict) -> None:
    now = datetime.now(timezone.utc)
    for f in cfg.get("feeds", []) + cfg.get("probe_candidates", []):
        try:
            raw = fetch_bytes(f["url"])
            items = parse_feed(raw)
            recent = [i for i in items if i["published"] and i["published"] >= now - timedelta(hours=24)]
            hit = [i for i in recent if (not f.get("filter")) or matches(i, cfg.get("keywords") or [])]
            newest = max((i["published"] for i in items if i["published"]), default=None)
            print(f"  [OK] {f['id']:14s} 전체 {len(items):3d} · 24시간 {len(recent):3d} · 반영 {len(hit):3d} "
                  f"· 최신 {newest.isoformat(timespec='minutes') if newest else '—'} · {f['url'][:90]}")
            for i in hit[:2]:
                print(f"         - {i['title'][:90]}")
        except Exception as e:  # noqa: BLE001
            print(f"  [실패] {f['id']:14s} {str(e)[:120]} · {f['url'][:90]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/data")
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args()
    cfg = load_cfg()
    if args.probe:
        probe(cfg)
        return
    data = collect(cfg)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "news.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    ok = sum(1 for f in data["meta"]["feeds"] if f["ok"])
    print(f"완료 — 최근 {data['meta']['window_hours']}시간 {len(data['items'])}건 "
          f"(피드 {ok}/{len(data['meta']['feeds'])}개 응답) · " +
          ", ".join(f"{data['meta']['categories'].get(k, k)} {v}" for k, v in data["counts"].items()))


if __name__ == "__main__":
    main()
