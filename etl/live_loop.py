#!/usr/bin/env python3
"""
스테이블코인 감시 - 상시 실행 루프 (실시간 갱신용)

GitHub Actions cron은 5분 미만 주기를 지원하지 않고, 그마저도 부하 시
지연된다. 진짜 1분 단위 갱신이 필요하면 이 스크립트를 켜놓은 PC나 서버에서
계속 돌려야 한다. 김치프리미엄은 매 루프(기본 60초)마다 갱신하고, 나머지는
API 할당량을 지키기 위해 훨씬 뜸하게 돌린다 — 아래 표 그대로다.

    python etl/live_loop.py                    # 기본 설정으로 무기한 실행
    python etl/live_loop.py --premium-secs 60 --heavy-mins 20

Ctrl+C 로 종료. 화면 출력 없이 백그라운드로 두려면:
    nohup python etl/live_loop.py > live.log 2>&1 &
Cloudflare tunnel 등으로 site/ 를 이미 서빙 중이면 이 스크립트가 site/data/
를 갱신하는 동안 브라우저에서 새로고침만 하면 최신값이 보인다.

┌──────────────────┬──────────┬────────────────────────────────────────┐
│ 모듈              │ 권장 주기 │ 이유                                     │
├──────────────────┼──────────┼────────────────────────────────────────┤
│ 김치프리미엄        │ 1분      │ 키 불필요, 사실상 무제한 공개 API           │
│ 스테이블코인 발행현황 │ 5분      │ 키 불필요, DefiLlama도 그 정도로 안 바뀜   │
│ 발행사 동결(ETH)    │ 20분     │ Etherscan 무료 티어 하루 10만 콜 보호      │
│ 코너 자금흐름(ETH)   │ 20분     │ 위와 동일 + 지갑 수만큼 콜이 곱해짐        │
│ 코너 자금흐름(XRP)   │ 20분     │ xrpscan 무료 사용 예의                   │
└──────────────────┴──────────┴────────────────────────────────────────┘

어테스테이션은 이 루프에 넣지 않는다. attestations.json 을 손으로 고치기
전까지는 몇 번을 다시 돌려도 값이 똑같기 때문이다.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def run(script: str, *args: str) -> bool:
    cmd = [PY, os.path.join(HERE, script), *args]
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {script} {' '.join(args)}")
    r = subprocess.run(cmd, cwd=os.path.dirname(HERE))
    if r.returncode != 0:
        print(f"  경고: {script} 종료코드 {r.returncode}", file=sys.stderr)
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--premium-secs", type=int, default=60, help="김치프리미엄 갱신 주기(초)")
    ap.add_argument("--stablecoin-mins", type=int, default=5, help="발행 현황 갱신 주기(분)")
    ap.add_argument("--heavy-mins", type=int, default=20,
                     help="동결/자금흐름(ETH·XRP) 갱신 주기(분). Etherscan 무료 티어 보호용 하한 권장 15분 이상")
    ap.add_argument("--skip-heavy", action="store_true",
                     help="ETHERSCAN_API_KEY 없을 때 등, 무거운 모듈을 아예 건너뛴다")
    args = ap.parse_args()

    if args.heavy_mins < 15 and not args.skip_heavy:
        print("경고: --heavy-mins 를 15분 미만으로 주면 Etherscan 무료 할당량을 "
              "하루 안에 다 쓸 수 있습니다. 계속 진행합니다만 권장하지 않습니다.", file=sys.stderr)

    has_key = bool(os.environ.get("ETHERSCAN_API_KEY"))
    if not has_key and not args.skip_heavy:
        print("ETHERSCAN_API_KEY 가 없어 동결·자금흐름(ETH) 모듈은 이번 실행에서 건너뜁니다.",
              file=sys.stderr)

    tick = 0
    stable_every = max(1, args.stablecoin_mins * 60 // args.premium_secs)
    heavy_every = max(1, args.heavy_mins * 60 // args.premium_secs)

    print(f"루프 시작 — 프리미엄 {args.premium_secs}초 / 발행현황 {args.stablecoin_mins}분 / "
          f"무거운 모듈 {args.heavy_mins}분 주기. Ctrl+C 로 종료.")

    try:
        while True:
            run("fetch_premium.py", "--days", "180")

            if tick % stable_every == 0:
                run("fetch.py")

            if tick % heavy_every == 0:
                if has_key:
                    run("fetch_freeze.py", "--days", "365")
                    run("fetch_flow.py", "--days", "180")
                if not args.skip_heavy:
                    run("fetch_flow_xrp.py", "--days", "180")

            tick += 1
            time.sleep(args.premium_secs)
    except KeyboardInterrupt:
        print("\n종료합니다.")


if __name__ == "__main__":
    main()
