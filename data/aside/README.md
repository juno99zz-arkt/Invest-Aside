# data/aside

Aside 주간 루틴이 `result.json`(시장 브리핑 + 종목 심층분석)을 이 폴더에 업로드하면
GitHub Actions `finalize` 가 자동 실행됩니다.

`result.json` 형식:

```json
{
  "week": "2026-W42",
  "analyzed_at": "2026-10-17 10:40",
  "market_brief": {
    "stance": "공격적 매수 | 선별적 매수 | 중립 | 방어적",
    "stance_reason": "...", "market_env": "...", "key_change": "...",
    "next_events": [{"date": "...", "event": "..."}],
    "news": [{"id": 3, "summary": "...", "insight": "...", "stars": 5, "tickers": ["NVDA"]}]
  },
  "analyses": { "TICKER": { "...": "aside_bridge.ANALYSIS_SCHEMA 참고" } }
}
```
