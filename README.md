# Invest-Aside — AI 투자 브리핑 대시보드 (API 비용 0원)

대시보드: https://juno99zz-arkt.github.io/Invest-Aside/

원본 [Invest](https://github.com/juno99zz-arkt/Invest)와 같은 구조·로직이지만, 유료 Claude API 호출(시장 브리핑·종목 심층분석)을
**Aside 주간 루틴**이 대신 수행합니다.

## 주간 흐름

| 시점 (KST) | 주체 | 작업 |
|---|---|---|
| 토 09:00 | GitHub Actions `prepare` | S&P500+NDX 정량 점수, M1~M6 신호, 후보 선정, RSS 뉴스 후보 → `data/pending/request.json` |
| 토 오전 | Aside 루틴 | 요청서를 읽고 웹검색으로 시장 브리핑(스탠스·뉴스 10선·일정) + 종목 심층분석 → `data/aside/result.json` 업로드 |
| 업로드 직후 | GitHub Actions `finalize` (push 트리거) | 결과 검증 → 신규편입/유지/제외 결정 → 이력·리포트 → 대시보드 배포 |
| 매일 17:00 | GitHub Actions `daily` | 시세·PEG·미국/한국 상위 50 갱신 |

## 2026-10 개선 (외부 검토 제언 반영)

1. **데이터 일관성** — FCF 는 현금흐름표 4분기 합계 기본, Yahoo 요약과 충돌 시 자동 표시·AI 검증·점수 재계산(`picks.rescore`),
   미검증이면 '검증 대기'로 신규 편입 보류. PER 기준(최근 12개월/향후 12개월/올해·다음 회계연도) 분리.
   Form 4 는 거래 대상(issuer) 일치 공시만, 13F 는 같은 분기만 비교(지연 자료는 가산점 제외), 한국 PER 결측은 '미수집' 표시.
2. **종목 선택과 투자 규모 분리** — 스탠스→주식 비중, 약세 시나리오 손실 상한·테마 상한으로 개별주 비중, 나머지 코어 SPY
   (`aside_bridge.size_portfolio`, 규칙 `data/risk_profile.json`). 실제 보유자산 노출(`data/my_holdings.json`, `themes.py`).
3. **판단표** — 현재 행동·가격 조건·약세/기준/강세·비중 한도·다음 검증·판단 변경 조건, 확신도 대신 근거 상태,
   경고 4단계(정상→추가 매수 중단→비중 재검토→논리 붕괴). 업종별 PER/PEG 신호등, 뉴스는 공시 우선·사실/해석/논리 영향.
4. **변경 알림** — 매일 실적 발표·급변·검증 일정·약세 가격 감지(`generate_invest.build_alerts`), 수요일 중간 재평가(`finalize_interim`).
5. **성과 검증** — 신호 다음 거래일 시가 체결, 배당·현금 이자·환전 스프레드, 원자료 보존(`data/archive/`),
   제외 후보 사후 추적, 정량 상위 8 전략·같은 비중 SPY·전액 SPY 비교(`sim.py`).

## 파일

- `run_weekly.py` — `prepare` / `finalize` 2단계 파이프라인
- `aside_bridge.py` — Aside 결과 스키마(원본 analyst.py 의 strict 스키마와 동일) 검증·정규화
  - 로컬 검증: `python aside_bridge.py data/pending/request.json data/aside/result.json`
- `picks.py` — 정량 점수·후보 선정·편입 결정 규칙 (원본 그대로)
- `generate_invest.py` — `index.html` 의 `INJECTED` 데이터 교체
- `analyst.py`, `compare_models.py`, `report_email.py` — 원본 Claude API 버전 (현재 워크플로에서 사용 안 함)

## 필요한 Secrets

- `SEC_USER_AGENT` — SEC EDGAR 접근용 `이름 이메일` 형식 (13F·Form 4)
