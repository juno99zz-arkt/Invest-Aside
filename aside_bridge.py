"""
Aside 연동부 (Claude API 대체, 비용 0원).

흐름
  1) GitHub Actions  : run_weekly.py prepare  → data/pending/request.json  (정량 신호·후보·분석 자료)
  2) Aside 주간 루틴 : request.json 을 읽고 웹검색으로 시장 브리핑 + 종목 심층분석
                       → data/aside/result.json 업로드 (push)
  3) GitHub Actions  : push 트리거 → run_weekly.py finalize → 편입/유지/제외 결정 → 대시보드 배포

이 파일은 결과 스키마 정의(기존 analyst.py 의 strict 스키마와 동일)와 검증·정규화만 담당한다.
로컬 검증:  python aside_bridge.py data/pending/request.json data/aside/result.json
"""
import json
import sys

STANCES = ["공격적 매수", "선별적 매수", "중립", "방어적"]
IMPACTS = ["긍정", "부정", "불확실"]
THESIS_STATUS = ["유효", "약화", "붕괴", "해당없음"]
VERDICTS = ["편입 적합", "관찰", "부적합"]

# ── 스키마 (analyst.py ANALYSIS_TOOL / MARKET_TOOL 과 동일 구조) ─────────────
S, I = "str", "score"
ANALYSIS_SCHEMA = {
    "one_line_thesis": S,
    "quality": {"score": I, "moat": S, "growth_durability": S, "summary": S},
    "financial_health": {"score": I, "cash_flow": S, "debt": S, "dilution": S, "summary": S},
    "valuation": {"score": I, "current_view": S, "upside_scenario": S, "summary": S},
    "catalysts": [{"event": S, "timing": S, "impact": IMPACTS, "detail": S}],
    "bear_case": {"summary": S, "key_risks": [S], "thesis_breakers": [S]},
    "thesis_status": THESIS_STATUS,
    "thesis_status_reason": S,
    "verdict": VERDICTS,
    "verdict_reason": S,
    "conviction": I,
    "sources": [{"title": S, "url": S}],
}
MARKET_SCHEMA = {
    "stance": STANCES,
    "stance_reason": S,
    "market_env": S,
    "key_change": S,
    "next_events": [{"date": S, "event": S}],
}
NEWS_ITEM = {"summary": S, "insight": S, "stars": I}


def _check(val, spec, path, errs):
    if spec == S:
        if not isinstance(val, str) or not val.strip():
            errs.append(f"{path}: 비어 있지 않은 문자열이어야 함")
    elif spec == I:
        if not isinstance(val, int) or isinstance(val, bool) or not 1 <= val <= 5:
            errs.append(f"{path}: 1~5 정수여야 함 (현재 {val!r})")
    elif isinstance(spec, list) and spec and all(isinstance(x, str) and x not in (S, I) for x in spec):
        if val not in spec:
            errs.append(f"{path}: {spec} 중 하나여야 함 (현재 {val!r})")
    elif isinstance(spec, list):
        if not isinstance(val, list):
            errs.append(f"{path}: 배열이어야 함")
            return
        for i, x in enumerate(val):
            _check(x, spec[0], f"{path}[{i}]", errs)
    elif isinstance(spec, dict):
        if not isinstance(val, dict):
            errs.append(f"{path}: 객체여야 함")
            return
        for k, sub in spec.items():
            if k not in val:
                errs.append(f"{path}.{k}: 누락")
            else:
                _check(val[k], sub, f"{path}.{k}", errs)


def validate_analysis(a, tk="?"):
    errs = []
    _check(a, ANALYSIS_SCHEMA, tk, errs)
    if not errs:
        if len(a["catalysts"]) < 2:
            errs.append(f"{tk}.catalysts: 2개 이상 필요")
        if len(a["bear_case"]["thesis_breakers"]) < 2:
            errs.append(f"{tk}.bear_case.thesis_breakers: 2개 이상 필요")
        if not any(str(s.get("url", "")).startswith("http") for s in a["sources"]):
            errs.append(f"{tk}.sources: 실제 URL 1개 이상 필요")
    return errs


def validate_market(m):
    errs = []
    _check(m, MARKET_SCHEMA, "market_brief", errs)
    news = m.get("news") if isinstance(m, dict) else None
    if not isinstance(news, list) or len(news) < 5:
        errs.append("market_brief.news: 5건 이상 필요")
    else:
        for i, n in enumerate(news):
            _check(n, NEWS_ITEM, f"market_brief.news[{i}]", errs)
            if "id" not in n and not str(n.get("url", "")).startswith("http"):
                errs.append(f"market_brief.news[{i}]: 후보 id 또는 url 필요")
    return errs


def merge_news(news, candidates):
    """후보 id 가 있으면 제목·출처·URL·날짜를 후보 원문으로 채운다 (링크 오류 방지). 중복 URL 제거."""
    by_id = {c["id"]: c for c in candidates or []}
    out, seen = [], set()
    for n in news or []:
        c = by_id.get(n.get("id")) if n.get("id") is not None else None
        item = {
            "title": (c or n).get("title", ""), "source": (c or n).get("source", ""),
            "url": (c or n).get("url", ""), "published": (c or n).get("published", ""),
            "summary": n.get("summary", ""), "insight": n.get("insight", ""),
            "stars": n.get("stars", 3), "tickers": [t for t in (n.get("tickers") or []) if isinstance(t, str)],
        }
        if not item["url"].startswith("http") or item["url"] in seen:
            continue
        seen.add(item["url"])
        out.append(item)
    return out


MAX_WEIGHT = 30     # 종목당 최대 비중(%)
DEFAULT_WEIGHT = 20  # 비중 미지정 시 기본 (5종목 × 20%)


def resolve_weights(proposed, kept, prev):
    """Agent 제안 비중(result.target_weights) → 최종 비중 {ticker: %}.
    - 보유 확정 종목(kept)만 대상. 5% 단위 정수, 5~MAX_WEIGHT.
    - 제안이 없거나 형식이 틀리면 이전 비중, 신규는 DEFAULT_WEIGHT.
    - 합계 100 초과 시 큰 비중부터 5%씩 축소. 나머지는 현금."""
    out, notes = {}, []
    for tk in kept:
        v = (proposed or {}).get(tk)
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) and v % 5 == 0 and 5 <= v <= MAX_WEIGHT
        if v is not None and not ok:
            notes.append(f"{tk} 비중 {v!r} 무효(5% 단위, 5~{MAX_WEIGHT}%) — 기본값 사용")
        out[tk] = int(v) if ok else int((prev or {}).get(tk, DEFAULT_WEIGHT))
    while sum(out.values()) > 100:
        big = max(out, key=lambda k: out[k])
        out[big] -= 5
        notes.append(f"합계 100% 초과 — {big} 5%p 축소")
    return out, notes


def load_result(request, result):
    """결과 파일 검증 → (market_brief | None, {ticker: analysis | None}, 오류 목록)."""
    errors = []
    if result.get("week") != request.get("week"):
        errors.append(f"주차 불일치: request {request.get('week')} vs result {result.get('week')}")

    mb = result.get("market_brief")
    m_errs = validate_market(mb) if mb else ["market_brief 없음"]
    if m_errs:
        errors += m_errs
        mb = None
    else:
        news = merge_news(mb["news"], request.get("news_candidates"))
        mb = {**{k: mb[k] for k in MARKET_SCHEMA}, "news": news, "news_source": "aside"}

    analyses = {}
    wanted = [d["ticker"] for d in request.get("to_analyze", [])]
    got = result.get("analyses") or {}
    for tk in wanted:
        a = got.get(tk)
        if a is None:
            errors.append(f"{tk}: 분석 결과 없음")
            analyses[tk] = None
            continue
        e = validate_analysis(a, tk)
        if e:
            errors += e
            analyses[tk] = None
        else:
            analyses[tk] = {k: a[k] for k in ANALYSIS_SCHEMA}
    return mb, analyses, errors


if __name__ == "__main__":
    req_path = sys.argv[1] if len(sys.argv) > 1 else "data/pending/request.json"
    res_path = sys.argv[2] if len(sys.argv) > 2 else "data/aside/result.json"
    with open(req_path, encoding="utf-8") as f:
        req = json.load(f)
    with open(res_path, encoding="utf-8") as f:
        res = json.load(f)
    mb, an, errs = load_result(req, res)
    ok = [tk for tk, a in an.items() if a]
    print(f"시장 브리핑: {'OK (뉴스 ' + str(len(mb['news'])) + '건)' if mb else '실패'}")
    print(f"종목 분석: {len(ok)}/{len(an)} 통과 {ok}")
    for e in errs:
        print("  ✗", e)
    tw = res.get("target_weights")
    if tw is None:
        print("  ✗ target_weights 없음 (이전 비중·기본 20% 적용됨)")
    else:
        _, notes = resolve_weights(tw, list(tw), {})
        print(f"비중 제안: {tw} (합계 {sum(v for v in tw.values() if isinstance(v, (int, float)))}%, 나머지 현금)")
        for n in notes:
            print("  ✗", n)
            errs.append(n)
    sys.exit(1 if errs else 0)
