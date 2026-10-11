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

## 파일

- `run_weekly.py` — `prepare` / `finalize` 2단계 파이프라인
- `aside_bridge.py` — Aside 결과 스키마(원본 analyst.py 의 strict 스키마와 동일) 검증·정규화
  - 로컬 검증: `python aside_bridge.py data/pending/request.json data/aside/result.json`
- `picks.py` — 정량 점수·후보 선정·편입 결정 규칙 (원본 그대로)
- `generate_invest.py` — `index.html` 의 `INJECTED` 데이터 교체
- `analyst.py`, `compare_models.py`, `report_email.py` — 원본 Claude API 버전 (현재 워크플로에서 사용 안 함)

## 필요한 Secrets

- `SEC_USER_AGENT` — SEC EDGAR 접근용 `이름 이메일` 형식 (13F·Form 4)
