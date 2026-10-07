#!/usr/bin/env python3
"""최근 24시간 뉴스 헤드라인 수집 테스트. 네트워크 없음(가짜 피드)."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "etl"))

import fetch_news as fn  # noqa: E402

NOW = datetime(2026, 10, 8, 0, 0, tzinfo=timezone.utc)


def rss(items):
    body = "".join(
        f"<item><title>{t}</title><link>{l}</link><pubDate>{format_datetime(d)}</pubDate>"
        + (f"<source url='https://x'>{s}</source>" if s else "")
        + (f"<description>{desc}</description>" if desc else "")
        + "</item>"
        for t, l, d, s, desc in items)
    return f"<?xml version='1.0'?><rss version='2.0'><channel><title>x</title>{body}</channel></rss>".encode()


def atom(entries):
    body = "".join(
        f"<entry><title>{t}</title><link rel='alternate' href='{l}'/><updated>{d.isoformat()}</updated></entry>"
        for t, l, d in entries)
    return f"<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'>{body}</feed>".encode()


GNEWS = rss([
    ("원화 스테이블코인 법안 국회 상정 - 한국경제", "https://news.google.com/a1", NOW - timedelta(hours=2), "한국경제", ""),
    ("원화 스테이블코인 법안 국회 상정 - 매일경제", "https://news.google.com/a2", NOW - timedelta(hours=3), "매일경제", ""),  # 같은 제목
    ("오래된 기사 - 연합뉴스", "https://news.google.com/a3", NOW - timedelta(hours=30), "연합뉴스", ""),  # 24시간 밖
])
FED = rss([
    ("Federal Reserve Board announces interest rates", "https://www.federalreserve.gov/1", NOW - timedelta(hours=1), "", ""),
    ("Agencies issue proposal on stablecoin issuers", "https://www.federalreserve.gov/2", NOW - timedelta(hours=5), "", ""),
])
ECB = atom([
    ("ECB publishes report on tokenised deposits", "https://www.ecb.europa.eu/1", NOW - timedelta(hours=4)),
    ("Monetary policy statement", "https://www.ecb.europa.eu/2", NOW - timedelta(hours=4)),
])

CFG = {
    "window_hours": 24, "max_items": 80,
    "keywords": ["stablecoin", "tokenised deposit", "스테이블코인"],
    "categories": {"reg_global": "해외 당국", "news_kr": "국내 보도", "reg_kr": "국내 당국"},
    "feeds": [
        {"id": "gnews_ko", "category": "news_kr", "url": "g", "filter": False},
        {"id": "fed", "name": "연준", "category": "reg_global", "url": "f", "filter": True},
        {"id": "ecb", "name": "ECB", "category": "reg_global", "url": "e", "filter": True},
        {"id": "broken", "name": "고장", "category": "reg_kr", "url": "b", "filter": True},
        {"id": "off", "category": "reg_kr", "url": "o", "enabled": False},
    ],
}


def fake_fetch(url):
    if url == "b":
        raise RuntimeError("HTTP Error 404")
    if url == "o":
        raise AssertionError("비활성 피드를 받으면 안 된다")
    return {"g": GNEWS, "f": FED, "e": ECB}[url]


class TestParse(unittest.TestCase):
    def test_rss_and_atom(self):
        self.assertEqual(len(fn.parse_feed(GNEWS)), 3)
        a = fn.parse_feed(ECB)
        self.assertEqual(a[0]["link"], "https://www.ecb.europa.eu/1")
        self.assertIsNotNone(a[0]["published"])

    def test_dates(self):
        self.assertEqual(fn.parse_date("Wed, 07 Oct 2026 21:00:00 GMT"),
                         datetime(2026, 10, 7, 21, 0, tzinfo=timezone.utc))
        self.assertEqual(fn.parse_date("2026-10-07T21:00:00Z"),
                         datetime(2026, 10, 7, 21, 0, tzinfo=timezone.utc))
        self.assertIsNone(fn.parse_date("어제"))

    def test_split_source(self):
        self.assertEqual(fn.split_source("제목 - 한국경제", "한국경제"), ("제목", "한국경제"))
        self.assertEqual(fn.split_source("Title only", ""), ("Title only", ""))


class TestCollect(unittest.TestCase):
    def setUp(self):
        self.d = fn.collect(CFG, now=NOW, fetch=fake_fetch)
        self.titles = [i["title"] for i in self.d["items"]]

    def test_window_and_dedup(self):
        self.assertIn("원화 스테이블코인 법안 국회 상정", self.titles)
        self.assertEqual(self.titles.count("원화 스테이블코인 법안 국회 상정"), 1)
        self.assertNotIn("오래된 기사", self.titles)

    def test_keyword_filter_on_full_feeds(self):
        self.assertIn("Agencies issue proposal on stablecoin issuers", self.titles)
        self.assertIn("ECB publishes report on tokenised deposits", self.titles)
        self.assertNotIn("Federal Reserve Board announces interest rates", self.titles)
        self.assertNotIn("Monetary policy statement", self.titles)

    def test_newest_first_and_source(self):
        pubs = [i["published"] for i in self.d["items"]]
        self.assertEqual(pubs, sorted(pubs, reverse=True))
        first = next(i for i in self.d["items"] if i["feed"] == "gnews_ko")
        self.assertEqual(first["source"], "한국경제")

    def test_broken_feed_does_not_stop(self):
        st = {f["id"]: f for f in self.d["meta"]["feeds"]}
        self.assertFalse(st["broken"]["ok"])
        self.assertIn("404", st["broken"]["error"])
        self.assertNotIn("off", st)
        self.assertEqual(self.d["counts"], {"reg_global": 2, "news_kr": 1, "reg_kr": 0})

    def test_no_body_text_stored(self):
        for i in self.d["items"]:
            self.assertEqual(set(i), {"title", "link", "source", "feed", "category", "published"})

    def test_per_feed_cap_keeps_newest(self):
        cfg = dict(CFG, feeds=[{"id": "fed", "category": "reg_global", "url": "f", "filter": False, "max": 1}])
        d = fn.collect(cfg, now=NOW, fetch=fake_fetch)
        self.assertEqual([i["title"] for i in d["items"]], ["Federal Reserve Board announces interest rates"])

    def test_repo_config_loads(self):
        cfg = fn.load_cfg()
        self.assertTrue(cfg["feeds"])
        for f in cfg["feeds"]:
            self.assertIn(f["category"], cfg["categories"])
            self.assertTrue(f["url"].startswith("https://"))


class TestDigestFile(unittest.TestCase):
    """site/digest/news_digest.json — 매일 점검이 쓰고 PR 검토로 들어오는 요약 파일 형식."""

    def setUp(self):
        import json
        p = Path(__file__).resolve().parents[1] / "site" / "digest" / "news_digest.json"
        self.d = json.loads(p.read_text(encoding="utf-8"))

    def test_required_fields(self):
        for k in ("written_at", "window", "headline", "sections", "caveats"):
            self.assertIn(k, self.d)
        self.assertIsNotNone(fn.parse_date(self.d["written_at"]))
        self.assertLess(fn.parse_date(self.d["window"]["from"]), fn.parse_date(self.d["window"]["to"]))

    def test_items_have_https_sources_and_confidence(self):
        for sct in self.d["sections"]:
            self.assertIn("label", sct)
            if not sct["items"]:
                self.assertTrue(sct.get("empty_note"))
            for it in sct["items"]:
                for k in ("title", "summary", "confidence", "sources"):
                    self.assertTrue(it.get(k), k)
                for src in it["sources"]:
                    self.assertTrue(src["url"].startswith("https://"))

    def test_disclaimer_kept(self):
        self.assertIn("공식 견해", self.d["caveats"])


if __name__ == "__main__":
    unittest.main()
