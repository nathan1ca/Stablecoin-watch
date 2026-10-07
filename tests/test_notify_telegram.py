#!/usr/bin/env python3
"""텔레그램 알림 메시지 구성 테스트. 네트워크 없음."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github" / "scripts"))

import notify_telegram as nt  # noqa: E402

BODY = """## 발견사항
- 무언가 발견

## 변경내용
1. **감시목록** 추가 — `etl/watchlist.json`
2. 차트 <축> 수정 & 정리
- [링크](https://example.com) 포함

## 상세 보고서
[보고서](https://docs.google.com/document/d/abc/edit)
"""


def event(action="opened", merged=False, draft=False, body=BODY):
    return {
        "action": action,
        "repository": {"full_name": "nathan1ca/Stablecoin-watch"},
        "pull_request": {
            "number": 12, "title": "[자동보완] 테스트 <PR>", "html_url": "https://github.com/x/y/pull/12",
            "body": body, "merged": merged, "draft": draft,
            "created_at": "2026-10-07T04:00:00Z", "merged_at": "2026-10-07T05:00:00Z" if merged else None,
            "additions": 10, "deletions": 2, "changed_files": 3,
        },
    }


class TestBuildMessage(unittest.TestCase):
    def test_opened(self):
        m = nt.build_message(event())
        self.assertIn("보완 제안", m)
        self.assertIn("&lt;PR&gt;", m)               # 제목의 꺾쇠는 이스케이프
        self.assertIn("감시목록 추가", m)              # 굵게·코드 기호 제거
        self.assertIn("차트 &lt;축&gt; 수정 &amp; 정리", m)
        self.assertNotIn("무언가 발견", m)            # 변경내용 절만 옮긴다
        self.assertIn("2026-10-07 13:00 KST", m)
        self.assertIn('href="https://docs.google.com/document/d/abc/edit"', m)
        self.assertNotIn("대시보드 열기", m)

    def test_merged(self):
        m = nt.build_message(event("closed", merged=True))
        self.assertIn("반영됨", m)
        self.assertIn("https://nathan1ca.github.io/Stablecoin-watch/", m)
        self.assertIn("2026-10-07 14:00 KST", m)

    def test_skips(self):
        self.assertIsNone(nt.build_message(event("closed", merged=False)))
        self.assertIsNone(nt.build_message(event(draft=True)))
        self.assertIsNone(nt.build_message(event("synchronize")))
        self.assertIsNone(nt.build_message({"action": "opened"}))

    def test_no_summary_section(self):
        m = nt.build_message(event(body="그냥 본문"))
        self.assertIn("PR 보기", m)

    def test_length_limit(self):
        long = "## 변경내용\n" + "\n".join(f"- 항목 {i} " + "가" * 300 for i in range(40))
        m = nt.build_message(event(body=long))
        self.assertLessEqual(len(m), nt.TELEGRAM_LIMIT)
        self.assertIn("…", m)


if __name__ == "__main__":
    unittest.main()
