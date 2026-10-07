#!/usr/bin/env python3
"""PR 이 열리거나 머지될 때 텔레그램으로 수정 내용을 알린다. 표준 라이브러리만 사용.

GitHub Actions(.github/workflows/notify-telegram.yml)에서 실행된다.

필요한 저장소 시크릿
  TELEGRAM_BOT_TOKEN  @BotFather 가 발급한 봇 토큰
  TELEGRAM_CHAT_ID    알림을 받을 대화(개인 채팅 또는 그룹)의 chat id

둘 중 하나라도 없으면 아무것도 보내지 않고 정상 종료한다(워크플로를 빨갛게
만들지 않는다). 토큰은 로그에 찍지 않는다.

사용
  python .github/scripts/notify_telegram.py            # GITHUB_EVENT_PATH 의 PR 이벤트
  python .github/scripts/notify_telegram.py --test     # 연결 확인용 시험 메시지
  python .github/scripts/notify_telegram.py --dry-run  # 보내지 않고 메시지만 출력
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

TELEGRAM_LIMIT = 4096          # 텔레그램 메시지 최대 길이(문자)
SUMMARY_MAX_LINES = 14         # 본문에서 옮겨 올 요약 줄 수
SUMMARY_MAX_CHARS = 1800
KST = timezone(timedelta(hours=9))

# 요약으로 옮겨 올 PR 본문 소제목. 앞에서부터 먼저 있는 것을 쓴다.
SUMMARY_HEADINGS = ("변경내용", "변경 내용", "Changes", "Summary")


def esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def section(body: str, names=SUMMARY_HEADINGS) -> str:
    """'## 변경내용' 같은 소제목 아래 내용을 다음 '## ' 전까지 꺼낸다."""
    if not body:
        return ""
    for name in names:
        m = re.search(rf"^##\s*{re.escape(name)}\s*$(.*?)(?=^##\s|\Z)", body, re.M | re.S)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return ""


def plain(md: str) -> str:
    """마크다운을 텔레그램에서 읽기 좋은 평문으로 단순화한다."""
    out = []
    for line in md.splitlines():
        s = line.rstrip()
        if not s.strip():
            continue
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1", s)   # 링크는 글자만
        s = re.sub(r"`([^`]*)`", r"\1", s)                           # 인라인 코드
        s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)                     # 굵게
        s = re.sub(r"^\s*[-*]\s+", "• ", s)                           # 목록 기호
        out.append(s)
    return "\n".join(out)


def clip(text: str, max_lines: int = SUMMARY_MAX_LINES, max_chars: int = SUMMARY_MAX_CHARS) -> str:
    lines = text.splitlines()
    cut = len(lines) > max_lines
    text = "\n".join(lines[:max_lines])
    if len(text) > max_chars:
        text, cut = text[: max_chars - 1].rstrip(), True
    return text + ("\n…" if cut else "")


def first_link(body: str, host: str) -> str | None:
    m = re.search(rf"https?://{re.escape(host)}[^\s)\]>]+", body or "")
    return m.group(0) if m else None


def kst(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")


def build_message(event: dict) -> str | None:
    """PR 이벤트 → 텔레그램 HTML 메시지. 알릴 필요가 없으면 None."""
    pr = event.get("pull_request") or {}
    action = event.get("action")
    if not pr:
        return None

    merged = bool(pr.get("merged"))
    if action == "closed" and not merged:
        return None  # 머지 없이 닫힌 PR 은 알리지 않는다
    if action not in ("opened", "reopened", "ready_for_review", "closed"):
        return None
    if pr.get("draft") and action != "closed":
        return None

    if action == "closed":
        head = "✅ 대시보드에 반영됨"
        when = kst(pr.get("merged_at"))
    else:
        head = "🆕 보완 제안 (검토 대기)"
        when = kst(pr.get("created_at"))

    title = pr.get("title") or "(제목 없음)"
    num = pr.get("number")
    url = pr.get("html_url") or ""
    repo = (event.get("repository") or {}).get("full_name", "")
    adds, dels, files = pr.get("additions"), pr.get("deletions"), pr.get("changed_files")

    body = pr.get("body") or ""
    summary = clip(plain(section(body)))
    report = first_link(body, "docs.google.com")

    parts = [f"<b>{esc(head)}</b>", f"<b>{esc(title)}</b>"]
    meta = [f"#{num}"] if num else []
    if files is not None:
        meta.append(f"파일 {files}개 · +{adds or 0} / −{dels or 0}")
    if when:
        meta.append(when)
    if meta:
        parts.append(esc(" · ".join(meta)))
    if summary:
        parts.append(esc(summary))
    links = [f'<a href="{esc(url)}">PR 보기</a>'] if url else []
    if merged and repo:
        owner, _, name = repo.partition("/")
        links.append(f'<a href="https://{esc(owner.lower())}.github.io/{esc(name)}/">대시보드 열기</a>')
    if report:
        links.append(f'<a href="{esc(report)}">상세 보고서</a>')
    if links:
        parts.append(" · ".join(links))

    msg = "\n\n".join(parts)
    if len(msg) > TELEGRAM_LIMIT:  # 요약을 줄여 한도 안으로
        over = len(msg) - TELEGRAM_LIMIT + 2
        msg = msg.replace(esc(summary), esc(summary[: max(0, len(summary) - over)]) + "…", 1)
    return msg


def test_message() -> str:
    return ("<b>🔔 스테이블코인 모니터링 알림 연결 확인</b>\n\n"
            "이 메시지가 보이면 설정이 끝난 것입니다. 앞으로 보완 PR 이 열리거나 "
            "대시보드에 반영될 때마다 이 대화로 알려 드립니다.\n\n"
            + esc(datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")))


def send(token: str, chat_id: str, text: str) -> None:
    data = urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST")
    try:
        with urlopen(req, timeout=20) as r:
            res = json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        # 응답 본문에는 토큰이 없다. 원인(예: chat not found)을 그대로 보여 준다.
        detail = e.read().decode("utf-8", "replace")[:300]
        raise SystemExit(f"텔레그램 전송 실패: HTTP {e.code} {detail}")
    except URLError as e:
        raise SystemExit(f"텔레그램 전송 실패: {e.reason}")
    if not res.get("ok"):
        raise SystemExit(f"텔레그램 전송 실패: {res.get('description')}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="연결 확인용 시험 메시지")
    ap.add_argument("--dry-run", action="store_true", help="보내지 않고 출력만")
    args = ap.parse_args()

    if args.test:
        msg = test_message()
    else:
        path = os.environ.get("GITHUB_EVENT_PATH")
        if not path or not os.path.exists(path):
            print("GITHUB_EVENT_PATH 없음 — 보낼 이벤트가 없다.")
            return
        with open(path, encoding="utf-8") as fh:
            msg = build_message(json.load(fh))
        if not msg:
            print("알릴 이벤트가 아님(머지 없이 닫힘·초안 등) — 건너뜀.")
            return

    if args.dry_run:
        print(msg)
        return

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("::notice::TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 시크릿이 없어 텔레그램 알림을 건너뜁니다.")
        return
    send(token, chat_id, msg)
    print("텔레그램 알림 전송 완료")


if __name__ == "__main__":
    main()
