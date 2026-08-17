# 스테이블코인 감시

공개 블록체인 원장에 기록된 발행·소각과 시장가격만으로 스테이블코인의 페그 유지 상태, 상환 압력, 시장 집중도를 측정하는 정적 대시보드입니다.

의존성이 없습니다. 파이썬 표준 라이브러리로 데이터를 받아 JSON으로 떨어뜨리고, 브라우저는 그 JSON을 읽어 그립니다. 서버도 데이터베이스도 API 키도 필요 없습니다.

## 최근 개선 (위험 모니터링)

- **합성 위험점수 (0–100)**: 페그 편차 · 상환 압력 · 발행사 집중도(HHI) · 알고리즘형 비중 · 가격 품질을 가중 평균. 임계값은 `etl/thresholds.json`.
- **다중 가격 교차검증**: DefiLlama 오라클 + CoinGecko. 소스 간 편차 ≥ 30bp 이면 `price_quality=degraded`.
- **스테이블코인 김치프리미엄**: 업비트 `KRW-USDT` · `KRW-USDC`를 $1 기준으로 측정 (임계 0.5% / 1.5%). BTC·ETH·XRP는 기존과 동일.
- **임계값 외부화**: `etl/thresholds.json` — ETL과 화면 메타가 같은 값을 공유.
- **공유 라이브러리**: `etl/lib/` (http · metrics · config). 단위 테스트 `tests/test_metrics.py`.
- **다크 모드**: 시스템 선호 감지 + 수동 토글 (설정 로컬 저장).

## 처음 실행

```bash
# 1. 원본 API 스키마부터 확인한다 (필드명이 바뀌었는지 검증)
python etl/fetch.py --probe

# 2. 실제 수집
python etl/fetch.py

# 3. 로컬 확인
python -m http.server 8000 --directory site
# → http://localhost:8000
```

`site/data/`에 지금 들어 있는 값은 화면 확인용 **샘플**입니다. 위 2번을 한 번 돌리면 실제 데이터로 덮어써지고 상단 안내 띠가 사라집니다.

`--probe`를 먼저 돌리라는 건 형식적인 권고가 아닙니다. DefiLlama는 무인증 공개 API라 스키마 변경 예고를 하지 않으므로, `pegMechanism`이나 `circulatingPrevMonth` 같은 필드명이 그대로인지 눈으로 한 번 보고 넘어가는 편이 안전합니다. ETL은 필드가 없어도 죽지 않고 `None`으로 넘기게 짜여 있어서, 검증을 건너뛰면 조용히 빈 칸이 늘어납니다.

## 구조

```
etl/fetch.py            수집 + 지표 계산 → site/data/*.json
etl/fetch_freeze.py     발행사 동결·소각 조치 (Etherscan, 선택)
etl/fetch_premium.py    김치프리미엄 (키 불필요)
etl/fetch_flow.py       국경 간 온체인 코너 자금흐름 · 이더리움 (Etherscan, 선택)
etl/fetch_flow_xrp.py   국경 간 온체인 코너 자금흐름 · XRP Ledger (키 불필요)
etl/attestations.json   어테스테이션 원본 데이터 (손으로 갱신)
etl/fetch_attestation.py 어테스테이션 시차 계산
etl/live_loop.py        상시 실행 루프 (실시간 갱신용, 선택)
etl/keccak.py           keccak-256 순수 구현 (topic0 계산)
etl/make_sample.py      샘플 데이터 생성 (개발용)
site/index.html         화면
site/style.css
site/app.js             렌더링 (의존성 없음)
site/data/               산출물. CI가 커밋한다.
.github/workflows/      15분 주기 수집 + GitHub Pages 배포
```

## 실시간(1분) 갱신

"1분마다 실시간"은 모듈 성격에 따라 가능 여부가 다릅니다. 뭉뚱그려 다루면 안 되는 이유부터 정리합니다.

| 모듈 | 근거 API | 1분 갱신 |
|---|---|---|
| 김치프리미엄 | Upbit·Binance·Frankfurter (무인증) | **가능** — 무료·무제한에 가까움 |
| 발행 현황 | DefiLlama (무인증) | 가능하지만 무의미 — 그 정도로 안 바뀜, 5분이면 충분 |
| 발행사 동결·자금흐름(ETH) | Etherscan (키 필요) | **불가능** — 무료 티어 하루 10만 콜, 1분마다 돌리면 이 모듈 하나가 하루 할당량의 대부분을 씀 |
| 자금흐름(XRP) | xrpscan (무인증이나 대량 사용 시 유료 권장) | 권장 안 함 |
| 어테스테이션 | 사람이 손으로 갱신 | 해당 없음 — 몇 번을 다시 돌려도 값이 그대로 |

그리고 애초에 GitHub Actions cron은 5분 미만 스케줄을 지원하지 않고, 부하 시 그마저 늦게 돕니다. 그래서 진짜 1분 단위가 필요하면 **로컬(또는 서버)에서 계속 켜놓는 스크립트**가 있어야 합니다.

### 준비물

1. Python이 실행 가능한, 계속 켜져 있는 컴퓨터 (집 PC, 라즈베리파이, 저가 VPS 등)
2. `ETHERSCAN_API_KEY` (동결·ETH 자금흐름까지 실시간에 가깝게 하고 싶다면)
3. 이미 갖고 계신 Cloudflare tunnel 설정 — 지금 만드는 대시보드도 그대로 물리면 됩니다

### 절차

```bash
# 1. 기본 설정으로 무기한 실행 (프리미엄 60초 / 발행현황 5분 / 무거운 모듈 20분)
python etl/live_loop.py

# 2. 백그라운드로 돌리고 싶다면
nohup python etl/live_loop.py > live.log 2>&1 &

# 3. Etherscan 키가 없다면 무거운 모듈(ETH 동결·자금흐름)을 아예 건너뛰기
python etl/live_loop.py --skip-heavy
```

`live_loop.py`는 반복마다 `etl/fetch_premium.py`를 실제로 실행해 `site/data/premium.json`을 새로 씁니다. 화면 쪽은 **브라우저가 Upbit·Binance를 직접 호출하지 않습니다** — 실제로 테스트해보니 두 곳 다 브라우저의 크로스오리진 호출(CORS)을 막습니다. 대신 브라우저가 60초마다 같은 출처의 `data/premium.json`을 다시 불러오는 방식입니다. 그래서:

- `live_loop.py`가 돌고 있으면 → 파일이 60초마다 갱신되고, 화면 상단에 초록 점이 깜빡이며 "실시간 갱신 중"이 뜹니다.
- `live_loop.py`가 꺼져 있으면 → 파일이 안 바뀌니 화면이 자동으로 "정적 값 · 기준시각 …"으로 표시됩니다. 코드 수정 없이 그대로 동작합니다.

`--heavy-mins`를 15분 미만으로 주지 않는 걸 권합니다. 그 밑으로 내리면 Etherscan 무료 할당량을 하루 안에 소진할 수 있습니다.

## 어테스테이션 시차 (선택, 손으로 갱신)

발행사가 공표한 준비금 보고서의 기준일과 지금 이 순간의 온체인 발행잔액 사이 간격을 봅니다. 이건 **자동 수집이 불가능한 유일한 모듈**입니다 — 어테스테이션 보고서는 PDF로만 나오고, 발행일·기준일을 구조화해서 주는 무료 API가 없습니다.

```bash
# etl/fetch.py 를 먼저 실행해 site/data/snapshot.json 이 있어야 드리프트가 계산됩니다
python etl/fetch.py
python etl/fetch_attestation.py
```

새 보고서가 나올 때마다 `etl/attestations.json`에 항목을 손으로 추가하십시오:

```json
{
  "issuer": "Circle",
  "symbol": "USDC",
  "as_of_date": "2026-04-30",
  "reported_circulating": 80000000000,
  "source_url": "https://www.circle.com/transparency",
  "verified": true
}
```

`reported_circulating`은 보고서 안의 "USDC In Circulation" 같은 문구에서 그대로 옮겨 적으면 됩니다. 지금 시드로 들어있는 Circle 2026년 3월 항목은 실제 공개 보고서 원문에서 확인한 값입니다.

## XRP 코너 자금흐름 (선택, 키 불필요)

이더리움 코너(`fetch_flow.py`)와 성격은 같지만 조건이 훨씬 유리합니다. XRP Ledger는 거래소가 이용자별 지갑을 안 만듭니다 — 거래소당 지갑이 보통 하나뿐이고 이용자 구분은 destination tag로 합니다. 그래서 코인원처럼 주소가 수만 개로 쪼개지는 문제 자체가 없습니다.

```bash
python etl/fetch_flow_xrp.py --probe   # xrpscan 라벨 목록·응답 형식 확인
python etl/fetch_flow_xrp.py --days 180
```

주소도 하드코딩하지 않습니다. xrpscan.com의 무인증 API(`names/well-known`)에서 실행할 때마다 라벨된 계정을 통째로 받아, 이름이 정확히 "Upbit"/"Bithumb"인 것을 한국 쪽으로, "Binance"/"OKX"/"Bybit"인 것을 해외 비교군으로 씁니다. 거래 내역 응답에 상대방 이름표가 이미 붙어서 오기 때문에 그걸로 바로 필터링합니다.

한계: 네이티브 XRP 결제만 봅니다(RLUSD 등 발행 통화 제외). "Bithumb Global"처럼 이름이 다른 계열사는 정확히 일치하지 않아 빠집니다.

## 측정하는 것

| 지표 | 산출 | 왜 보는가 |
|---|---|---|
| 페그 편차 | (시장가 − 1.0) × 10,000 bp | 스테이블코인의 유일한 약속이 지켜지는지 |
| 30일 순증감률 | 발행잔액 대비 30일 전 대비 변화율 | 상환이 발행을 앞지르는 국면인지 |
| 담보 유형별 비중 | 발행잔액 가중 | 알고리즘형 비중은 시스템 취약성의 1차 지표 |
| 발행사 HHI | Σ(점유율%)² | 단일 발행사 사고가 시장 전체로 번질 여지 |
| 체인 HHI | 동일 | 특정 체인 장애 시 결제 중단 범위 |
| 페그 통화별 비중 | 발행잔액 가중 | 비달러 페그의 실제 규모 |

## 등급 임계치

`etl/fetch.py`의 `THRESHOLDS`에 모여 있습니다. 법정 기준이 아니라 임의로 정한 관측선이며, 사이트 하단 '산출 방법과 한계'에 그대로 노출됩니다. 조정하면 화면 설명도 자동으로 따라갑니다.

| 항목 | 주의 | 경보 |
|---|---|---|
| 페그 편차 | ±25bp | ±100bp |
| 30일 순증감률 | −10% | −25% |
| 발행사 HHI | 2,500 초과 | — |
| 알고리즘형 비중 | 5% 초과 | — |

HHI 2,500은 미국 수평결합지침의 고집중 시장 판단선을 그대로 가져온 것이라 스테이블코인 시장에 그대로 적용하는 데는 논쟁의 여지가 있습니다. 근거를 정리해 조정하는 편이 좋습니다.

## 동결 조치 수집 (선택)

발행사가 특정 주소를 블랙리스트에 올리거나 잔액을 소각한 기록은 전부 온체인 이벤트 로그로 남습니다. 페그 편차가 시장이 발행사를 어떻게 보는지의 지표라면, 동결 건수는 발행사가 실제로 통제권을 얼마나 행사하는지의 지표입니다.

```bash
export ETHERSCAN_API_KEY=...        # https://etherscan.io/apis 무료
python etl/fetch_freeze.py --probe  # 시그니처와 topic0 확인
python etl/fetch_freeze.py --days 365
```

키가 없으면 아무것도 하지 않고 종료하며, 사이트는 해당 섹션을 숨깁니다. CI에서는 저장소 시크릿 `ETHERSCAN_API_KEY`를 등록하면 자동으로 돌아갑니다.

Etherscan은 2025년 8월 V1 API를 종료했습니다. 이 코드는 V2(`api.etherscan.io/v2/api` + `chainid` 파라미터)를 씁니다. 키 하나로 여러 체인을 조회할 수 있으므로 `ISSUERS`에 `chainid`만 바꿔 추가하면 다른 체인으로 확장됩니다.

### 이벤트 시그니처

`topic0`은 시그니처 문자열에서 런타임에 계산합니다(`etl/keccak.py`, 순수 파이썬 구현). 하드코딩된 해시를 두지 않으므로 이벤트를 추가할 때 이름만 적으면 됩니다.

| 발행사 | 동결 | 해제 | 소각 | 확인 |
|---|---|---|---|---|
| Tether (USDT) | `AddedBlackList` | `RemovedBlackList` | `DestroyedBlackFunds` | 확인됨 |
| Circle (USDC) | `Blacklisted` | `UnBlacklisted` | 없음 | 확인됨 |
| Paxos (PYUSD/USDP) | `FreezeAddress` 외 | `UnfreezeAddress` 외 | `WipeFrozenAddress` 외 | **미확인** |

Paxos 계열은 시그니처를 실물 로그로 확인하지 못했습니다. 후보를 여러 개 두고 로그가 잡히는 쪽을 채택하도록 짜여 있으며, 전부 빈손이면 `meta.notes`에 경고가 남고 화면 하단에 표시됩니다. 그때는 컨트랙트 ABI를 보고 `ISSUERS`를 고치십시오.

구현체마다 대상 주소를 `indexed`로 두기도 하고 아니기도 해서(USDC는 topics, USDT는 data) 디코더가 양쪽을 모두 봅니다.

### 다루는 범위

이 집계는 **발행사의 조치**를 셉니다. 등재된 주소는 발행사가 공개적으로 블랙리스트에 올린 대상이고 이미 블록 익스플로러에서 누구나 볼 수 있는 정보이지만, 이 사이트는 주소를 개인과 연결하거나 특정인을 추적하지 않습니다. 화면에는 축약된 주소만 표시됩니다.

## 국경 간 순유출 (김치프리미엄 + 온체인 코너)

두 모듈로 나눠져 있습니다. 신뢰도가 다르기 때문입니다.

### 김치프리미엄 — 항상 켜짐, 라벨 불필요

```bash
python etl/fetch_premium.py --probe   # Upbit·Binance·Frankfurter 응답 확인
python etl/fetch_premium.py --days 180
```

키가 필요 없습니다. BTC·ETH·XRP 세 자산의 국내외 가격 차이를 그대로 계산합니다. XRP를 포함시킨 이유는 국내 재정거래에서 역사적으로 가장 많이 쓰인 자산이기 때문입니다. 이 지표는 라벨링 문제에서 완전히 자유롭기 때문에, 이 프로젝트에서 가장 신뢰도가 높은 축에 속합니다.

### 온체인 코너 자금흐름 — Etherscan 키 필요, 좁은 대리지표

```bash
export ETHERSCAN_API_KEY=...  # 동결 모듈과 같은 키 재사용 가능
python etl/fetch_flow.py --probe
python etl/fetch_flow.py --days 180
```

업비트·빗썸의 Etherscan 공개 태그 지갑과 Binance·OKX·Bybit 각각의 최대 단일 핫월렛 사이를 오간 **USDT·USDC** 이체를 집계합니다. 사용한 주소는 전부 Etherscan에서 직접 확인한 것입니다.

| 구분 | 주소 | 확인 |
|---|---|---|
| Upbit 1 | `0x390de26d772d2e2005c6d1d24afc902bae37a4bb` | [Etherscan](https://etherscan.io/address/0x390de26d772d2e2005c6d1d24afc902bae37a4bb) |
| Upbit 2 | `0xba826fec90cefdf6706858e5fbafcb27a290fbe0` | [Etherscan](https://etherscan.io/address/0xba826fec90cefdf6706858e5fbafcb27a290fbe0) |
| Upbit 3 | `0x5e032243d507c743b061ef021e2ec7fcc6d3ab89` | [Etherscan](https://etherscan.io/address/0x5e032243d507c743b061ef021e2ec7fcc6d3ab89) |
| Upbit Cold Wallet | `0xc9cf0ec93d764f5c9571fd12f764bae7fc87c84e` | [Etherscan](https://etherscan.io/address/0xc9cf0ec93d764f5c9571fd12f764bae7fc87c84e) |
| Bithumb Hot Wallet | `0x17e5545b11b468072283cee1f066a059fb0dbf24` | [Etherscan](https://etherscan.io/address/0x17e5545b11b468072283cee1f066a059fb0dbf24) |
| Binance 14 (비교군) | `0x28c6c06298d514db089934071355e5743bf21d60` | [Etherscan](https://etherscan.io/address/0x28c6c06298d514db089934071355e5743bf21d60) |
| OKX Hot Wallet 3 (비교군) | `0xa9ac43f5b5e38155a288d1a01d2cbc4478e14573` | [Etherscan](https://etherscan.io/address/0xa9ac43f5b5e38155a288d1a01d2cbc4478e14573) |
| Bybit Hot Wallet (비교군) | `0xf89d7b9c864f589bbf53a82105107622b35eaa40` | [Etherscan](https://etherscan.io/address/0xf89d7b9c864f589bbf53a82105107622b35eaa40) |

**이 숫자는 실제 순유출의 하한선이지 전체가 아닙니다.** 화면과 코드 양쪽에 이유를 명시해뒀습니다.

- 이더리움 메인넷만 봅니다. 국내 재정거래는 트론(USDT-TRC20)·XRP 통로 비중이 크다고 알려져 있는데, 둘 다 여기 없습니다. 확장하려면 Tronscan API와 XRPL 공개 노드가 필요합니다.
- 코인원은 Etherscan에 태그된 주소가 5만 2천 개가 넘습니다 — 이용자별 입금주소가 개별 태그된 것이라 '거래소 지갑'으로 묶을 수 없습니다. 코빗·고팍스는 신뢰할 만한 공개 태그를 확인하지 못해 뺐습니다.
- 해외 비교군은 세 거래소 각각의 최대 단일 핫월렛 하나씩입니다. 같은 거래소가 굴리는 다른 지갑들, 그리고 Coinbase·Kraken 등은 빠져 있습니다.
- 추적 자산은 USDT·USDC 둘입니다. DAI 등 다른 스테이블코인은 포함되지 않습니다.

새 통로를 추가하려면 `ISSUERS` 방식과 같은 패턴으로 `KR_WALLETS`/`GLOBAL_WALLETS`에 주소를 추가하면 되는데, **반드시 Etherscan에서 그 주소 페이지를 직접 열어 Name Tag를 눈으로 확인한 뒤에 추가하십시오.** 검증 안 된 주소를 넣는 순간 이 사이트의 신뢰도 전체가 흔들립니다.

## 배포

GitHub Pages 기준입니다. 저장소 Settings → Pages → Source를 **GitHub Actions**로 바꾸면 워크플로가 `site/`를 그대로 올립니다. Cloudflare Pages를 쓸 경우 빌드 명령 없이 출력 디렉터리만 `site`로 지정하면 됩니다.

## 이 데이터가 말하지 않는 것

- **준비자산**: 발행잔액은 원장상 발행량입니다. 뒷받침하는 자산이 실제로 있는지, 충분한지는 온체인에서 보이지 않습니다. 준비금 검증은 감사보고서와 어테스테이션의 영역입니다.
- **가격의 신뢰도**: 유동성이 얕은 종목은 소량 체결로도 편차가 크게 튑니다. 발행잔액 하한(기본 5천만 달러)을 둔 이유입니다.
- **이중 계상**: 브릿지된 잔액이 원본과 함께 잡힐 수 있어 소형 체인 수치는 보수적으로 보아야 합니다.
- **보유자**: 주소별 보유 분포는 다루지 않습니다.

## 다음으로 붙일 만한 것

1. **디페깅 선행지표** — Curve 등 주요 풀의 자산 구성비. 페그가 깨지기 전에 풀이 먼저 기웁니다.
2. **트론 코너 추가** — USDT-TRC20 비중이 크다고 알려져 있지만, 업비트·빗썸의 신뢰할 만한 트론 주소를 아직 확인하지 못했습니다.
3. **유통량 검증** — 재단·팀 지갑의 실제 이동을 공시된 유통량 계획과 대조. 라벨 확보가 선행돼야 합니다.
4. **어테스테이션 자동 파싱** — 지금은 손으로 `attestations.json`을 채워야 합니다. 발행사별 PDF 레이아웃이 어느 정도 고정적이라면 pdfplumber로 기준일·유통량을 자동 추출하는 것도 가능할 수 있습니다. 다만 레이아웃이 바뀌면 조용히 실패할 위험이 있어 신중하게 접근해야 합니다.

## 출처와 성격

데이터는 [DefiLlama](https://defillama.com/stablecoins) 공개 엔드포인트에서 가져옵니다. 무료 이용 시 출처 표기가 요구되며, 화면과 이 문서에 표기되어 있습니다.

공개 데이터만으로 만든 개인 참고 자료입니다. 어떠한 기관의 공식 견해도 아니며, 투자 판단의 근거로 쓰기에 적합하지 않습니다.
