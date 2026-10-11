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

ALERT_LEVELS = ["정상", "추가 매수 중단", "비중 재검토", "논리 붕괴", "해당없음"]
ACTIONS = ["신규 매수", "추가 매수", "보유", "추가 매수 보류", "축소 검토", "매수 보류", "관망"]
IMPACT = ["강화", "약화", "변화 없음", "해당없음"]

# ── 스키마 (analyst.py ANALYSIS_TOOL / MARKET_TOOL 기반 + 판단표·근거·데이터 검증) ─────────────
S, I, N, NO = "str", "score", "num", "num?"
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
    # ── 2026-10 개선: 데이터 검증·근거 상태·경고 단계·판단표 ──
    "alert_level": ALERT_LEVELS,
    "data_checks": [{"metric": S, "dashboard_value": S, "verified_value": S, "source": S,
                     "status": ["일치", "불일치(수정)", "확인불가"]}],
    "verified_metrics": {"fcf_margin_pct": NO, "fcf_yield_pct": NO, "pe_ntm": NO, "pe_basis_note": S},
    "evidence": {"data_verified": ["검증 완료", "검증 대기"], "bear_case_status": ["해소", "부분 해소", "미해소"],
                  "estimate_dispersion": S, "reward_risk": S, "conviction_basis": S},
    "decision_table": {
        "action": ACTIONS, "buy_below": NO, "price_logic": S,
        "scenarios": [{"name": ["약세", "기준", "강세"], "eps": N, "pe": N, "price": N, "horizon": S, "assumption": S}],
        "next_checks": [{"metric": S, "threshold": S, "date": S}],
        "change_triggers": [{"condition": S, "new_action": S}],
    },
}
MARKET_SCHEMA = {
    "stance": STANCES,
    "stance_reason": S,
    "market_env": S,
    "key_change": S,
    "next_events": [{"date": S, "event": S}],
}
NEWS_ITEM = {"summary": S, "insight": S, "stars": I, "fact": S, "interpretation": S, "thesis_impact": IMPACT}


def _check(val, spec, path, errs):
    if spec == S:
        if not isinstance(val, str) or not val.strip():
            errs.append(f"{path}: 비어 있지 않은 문자열이어야 함")
    elif spec == I:
        if not isinstance(val, int) or isinstance(val, bool) or not 1 <= val <= 5:
            errs.append(f"{path}: 1~5 정수여야 함 (현재 {val!r})")
    elif spec in (N, NO):
        if val is None and spec == NO:
            return
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            errs.append(f"{path}: 숫자여야 함 (현재 {val!r})")
    elif isinstance(spec, list) and spec and all(isinstance(x, str) and x not in (S, I, N, NO) for x in spec):
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
        if len(a["data_checks"]) < 2:
            errs.append(f"{tk}.data_checks: 핵심 수치 2개 이상 검증 필요")
        sc = a["decision_table"]["scenarios"]
        names = [x["name"] for x in sc]
        if sorted(names) != sorted(["약세", "기준", "강세"]):
            errs.append(f"{tk}.decision_table.scenarios: 약세·기준·강세 3개가 각 1번씩 필요 (현재 {names})")
        else:
            by = {x["name"]: x for x in sc}
            for x in sc:
                if x["eps"] and x["pe"] and abs(x["eps"] * x["pe"] / x["price"] - 1) > 0.15:
                    errs.append(f"{tk}.{x['name']}: price({x['price']}) ≈ eps×pe({x['eps']}×{x['pe']}) 이어야 함")
            if not by["약세"]["price"] < by["기준"]["price"] <= by["강세"]["price"]:
                errs.append(f"{tk}.scenarios: 약세 < 기준 ≤ 강세 가격 순서여야 함")
        if not a["decision_table"]["next_checks"] or not a["decision_table"]["change_triggers"]:
            errs.append(f"{tk}.decision_table: next_checks·change_triggers 각 1개 이상 필요")
        # FCF 를 '불일치(수정)' 으로 고쳤다면 확정값이 있어야 점수를 재계산할 수 있다
        fcf_fixed = any(c["status"] == "불일치(수정)" and "FCF" in c["metric"].upper() for c in a["data_checks"])
        if fcf_fixed and a["evidence"]["data_verified"] == "검증 완료" and a["verified_metrics"].get("fcf_margin_pct") is None:
            errs.append(f"{tk}: FCF 불일치를 수정했다면 verified_metrics.fcf_margin_pct 에 확정값을 넣어야 함")
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
            "kind": (c or {}).get("kind") or n.get("kind") or "뉴스",
            "summary": n.get("summary", ""), "insight": n.get("insight", ""),
            "fact": n.get("fact", ""), "interpretation": n.get("interpretation", ""),
            "thesis_impact": n.get("thesis_impact", "해당없음"),
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


def _floor5(x):
    return int(max(0, x) // 5 * 5)


def scenario_of(a, name):
    for s in ((a or {}).get("decision_table") or {}).get("scenarios", []):
        if s.get("name") == name:
            return s
    return None


SCENARIO_PROBS = {"약세": 0.25, "기준": 0.5, "강세": 0.25}


def expected_return(a, price, probs=None):
    """약세·기준·강세 시나리오 가격의 확률 가중 12개월 기대수익률(%). 시나리오가 없으면 None."""
    if not price:
        return None
    tot = 0.0
    for name, p in (probs or SCENARIO_PROBS).items():
        s = scenario_of(a, name)
        if not s or not s.get("price"):
            return None
        tot += p * (s["price"] / price - 1)
    return round(tot * 100, 1)


def size_portfolio(kept, proposed, prev, analyses, prices, meta, stance, profile, allocation=None, new_tickers=None):
    """최대 5종목 개별주 + 원화 현금 계좌의 비중 결정 (ETF·코어 없음, 종목 선택과 분리).
    1) 종목별 출발점: 보유 종목은 Agent 제안(없으면 지난 비중), 신규는 확신도별 기본(5→30, 4→20, 3→10 탐색 비중)
       — Agent 는 신규 종목을 확신도 기본보다 높일 수 없음
    2) 기대수익률(약세25·기준50·강세25) < 최소 기대수익이면 신규 0%, 보유는 비중 확대 금지
    3) 상한: 한 종목 최대 · 약세 시나리오에서 전체 자산 손실 한도 · 경고 단계 · 검증 대기 · 테마 합계
    4) 주식 합계 ≤ 스탠스별 상한(Agent ±조정) — 넘으면 기대수익이 가장 낮은 종목부터 5%p 축소
    5) 나머지는 원화 현금
    반환: (weights {ticker: %}, allocation 설명 dict)"""
    from themes import themes_of
    notes, per = [], {}
    prev = prev or {}
    new = set(new_tickers) if new_tickers is not None else {tk for tk in kept if tk not in prev}
    conv_w = {int(k): v for k, v in (profile.get("conviction_weight") or {"5": 30, "4": 20, "3": 10}).items()}
    max_single = profile.get("max_single_weight", 30)
    max_loss = profile.get("max_position_loss_pct", 8)
    hurdle = profile.get("min_expected_return_pct", 10)
    probs = {k: v / 100 for k, v in (profile.get("scenario_probs") or {}).items()} or None
    w, er = {}, {}
    for tk in kept:
        a = analyses.get(tk) or {}
        price = prices.get(tk)
        v = (proposed or {}).get(tk)
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) and v % 5 == 0 and 0 <= v <= max_single
        if v is not None and not ok:
            notes.append(f"{tk} 제안 비중 {v!r} 무효(5% 단위, 0~{max_single}%) — 규칙값 사용")
            v = None
        pw = prev.get(tk)
        info = {"new": tk in new}
        if tk in new:
            base = conv_w.get(int(a.get("conviction") or 0), 0)
            x = base if v is None else min(int(v), base)
            info["conviction_base"] = base
        else:
            x = int(v) if v is not None else int(pw if pw is not None else DEFAULT_WEIGHT)
        info["proposed"] = x
        e = expected_return(a, price, probs)
        er[tk] = e
        info["expected_return_pct"] = e
        if e is None:
            notes.append(f"{tk}: 시나리오 없음 — 기대수익 미계산")
        elif e < hurdle:
            if tk in new:
                notes.append(f"{tk}: 기대수익 {e:+.1f}% < 최소 {hurdle}% → 신규 0%")
                x = 0
            elif pw is not None and x > pw:
                notes.append(f"{tk}: 기대수익 {e:+.1f}% < 최소 {hurdle}% → 비중 확대 금지({pw}%)")
                x = pw
        x = min(x, max_single)
        bear = scenario_of(a, "약세")
        if bear and price:
            down = bear["price"] / price - 1
            info["downside_pct"] = round(down * 100, 1)
            if down < 0:
                cap = max(5, _floor5(max_loss / abs(down)))
                info["risk_cap"] = cap
                if x > cap:
                    notes.append(f"{tk}: 약세 시나리오 {down * 100:.0f}% → 전체 손실 {max_loss}% 이내로 비중 {x}%→{cap}%")
                    x = cap
        lvl = a.get("alert_level")
        if lvl == "비중 재검토":
            nx = max(5, _floor5((pw or x) / 2))
            notes.append(f"{tk}: 경고 '비중 재검토' → {x}%→{min(x, nx)}%")
            x = min(x, nx)
        elif lvl == "추가 매수 중단" and pw is not None and x > pw:
            notes.append(f"{tk}: 경고 '추가 매수 중단' → 지난 비중 {pw}% 유지")
            x = pw
        if (a.get("evidence") or {}).get("data_verified") == "검증 대기" and x > (pw or 0):
            notes.append(f"{tk}: 핵심 수치 검증 대기 → 비중 확대 보류({pw or 0}%)")
            x = pw or 0
        w[tk] = x
        per[tk] = info
    ekey = lambda t: er.get(t) if er.get(t) is not None else -999
    # 테마 상한 (달러 자산 제외)
    max_theme = profile.get("max_theme_pct", 50)
    for _ in range(40):
        exp = {}
        for tk, x in w.items():
            for th in themes_of(tk, (meta.get(tk) or {}).get("sector"), (meta.get(tk) or {}).get("industry")):
                if not th.startswith("달러"):
                    exp.setdefault(th, []).append(tk)
        over = [(th, tks) for th, tks in exp.items() if sum(w[t] for t in tks) > max_theme]
        if not over:
            break
        th, tks = over[0]
        low = min([t for t in tks if w[t] > 0], key=ekey)
        w[low] -= 5
        notes.append(f"테마 '{th}' 합계 {max_theme}% 초과 → 기대수익이 낮은 {low} 5%p 축소")
    # 주식 합계 상한 (스탠스)
    caps = profile.get("stock_cap_by_stance") or {}
    base_cap = caps.get(stance, 80)
    cap, reason = base_cap, f"스탠스 '{stance}' 주식 합계 상한 {base_cap}%"
    adj = profile.get("agent_adjust_max", 10)
    if allocation and isinstance(allocation.get("equity_pct"), (int, float)):
        want = min(100, _floor5(allocation["equity_pct"] + 2.5))
        if abs(want - base_cap) <= adj:
            cap = want
            if cap != base_cap:
                reason += f" → Agent 조정 {cap}%: {allocation.get('reason', '')}"
        else:
            notes.append(f"Agent 제안 주식 상한 {allocation['equity_pct']}% 는 허용 범위(±{adj}%p) 밖 — 기본값 사용")
    while sum(w.values()) > cap:
        low = min([t for t in w if w[t] > 0], key=ekey)
        w[low] -= 5
        notes.append(f"주식 합계가 상한 {cap}% 초과 → 기대수익이 가장 낮은 {low} 5%p 축소")
    weights = {t: x for t, x in w.items() if x > 0}
    stock = sum(weights.values())
    for tk, info in per.items():
        info["final"] = weights.get(tk, 0)
        if info.get("downside_pct") is not None:
            info["portfolio_impact_pct"] = round(info["final"] * info["downside_pct"] / 100, 1)
    port_e = round(sum(weights[t] * (er.get(t) or 0) for t in weights) / 100
                   + (100 - stock) * profile.get("cash_rate_pct", 2.5) / 100, 1)
    alloc = {"stance": stance, "equity_pct": cap, "stock_pct": stock, "core_etf": None, "core_pct": 0,
             "cash_pct": 100 - stock, "reason": reason, "notes": notes, "per_stock": per,
             "expected_return_pct": port_e,
             "rules": {k: profile.get(k) for k in ("max_holdings", "max_position_loss_pct", "max_single_weight",
                                                    "max_theme_pct", "min_expected_return_pct", "conviction_weight")}}
    return weights, alloc


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
    got = result.get("analyses") or {}
    # 요청된 종목 + (재사용 대상이었지만 새 스키마로 다시 분석해 온 종목)
    wanted = [d["ticker"] for d in request.get("to_analyze", [])]
    wanted += [tk for tk in got if tk not in wanted and tk in {d["ticker"] for d in request.get("dossiers", [])}]
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
