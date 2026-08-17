"""공통 HTTP 헬퍼. 표준 라이브러리만 사용."""

from __future__ import annotations

import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

UA = "stablecoin-watch/0.2 (https://github.com/nathan1ca/Stablecoin-watch; public supervisory dashboard)"


def get_json(url: str, retries: int = 3, timeout: int = 45, headers: dict | None = None):
    """JSON GET with exponential backoff. Raises RuntimeError on final failure."""
    last = None
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    for attempt in range(retries):
        try:
            req = Request(url, headers=hdrs)
            with urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            wait = 2 ** attempt
            print(f"  재시도 {attempt + 1}/{retries} ({e}) — {wait}s 대기", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"{url} 수집 실패: {last}")


def get_json_params(base: str, params: dict | None = None, **kwargs):
    url = base
    if params:
        url += ("&" if "?" in base else "?") + urlencode(params)
    return get_json(url, **kwargs)
