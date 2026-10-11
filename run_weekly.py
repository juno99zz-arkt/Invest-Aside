"""
주간 파이프라인 — Claude API 대신 Aside(무료)가 AI 분석을 맡는 2단계 구조.

  python run_weekly.py prepare   (GitHub Actions, 매주 토 09:00 KST, 비용 0)
    1. 유니버스(S&P 500 + Nasdaq-100) 스냅샷
    2. 신호 생성: M1 13F / M2 섹터 추정치 / M3 실적개선 / M4 흑자전환 / M5 Form 4 / M6 사이클
    3. 정량 점수 → 보유 + 신규 후보 선정 → 상세 데이터(dossier) → 재사용/재분석 판정
    4. RSS 뉴스 후보 수집
    → data/pending/request.json  (Aside 가 읽는 분석 요청서)

  [Aside 주간 루틴]  request.json 읽기 → 웹검색 기반 시장 브리핑 + 종목 심층분석
                     → data/aside/result.json 업로드(push)

  python run_weekly.py finalize  (push 트리거, 비용 0)
    5. 결과 검증(aside_bridge) → 분석 캐시 갱신 → 신규편입/유지/제외 결정 → 이력 갱신
    → data/weekly_signals.json, data/picks_history.json, data/reports/<주차>.json
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
PENDING_PATH = os.path.join(DATA_DIR, "pending", "request.json")
RESULT_PATH = os.path.join(DATA_DIR, "aside", "result.json")
KST = timezone(timedelta(hours=9))


def _pct(v, n=1):
    return None if v is None else round(v * 100, n)


def _b(v):
    return None if v is None else round(v / 1e9, 2)


def _dump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def _load(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_watchlist():
    """data/watchlist.json {"tickers": [...]} → 대문자 티커 목록."""
    w = _load(os.path.join(DATA_DIR, "watchlist.json"), {}) or {}
    return [str(t).strip().upper() for t in w.get("tickers", []) if str(t).strip()]


def statement_fcf(s, d):
    """현금흐름표 기준 FCF 마진·수익률(비율). Yahoo 요약(info.freeCashflow)과 다를 수 있어 이 값을 기본으로 쓴다."""
    revs = [v for _, v in d.get("q_revenue", [])][-4:]
    fcf = d.get("fcf_ttm")
    rev_ttm = sum(revs) if len(revs) == 4 and None not in revs else None
    margin = fcf / rev_ttm if fcf is not None and rev_ttm else None
    yld = fcf / s["market_cap"] if fcf is not None and s.get("market_cap") else None
    return margin, yld, rev_ttm


def pe_basis(s, today):
    """PER 기준별 값: 최근 12개월(GAAP) / 올해 회계연도 / 다음 회계연도 / 향후 12개월(추정, 두 회계연도 가중평균)."""
    price, e0, e1 = s.get("price"), s.get("eps_0y"), s.get("eps_next_fy")
    pe = lambda e: round(price / e, 1) if price and e and e > 0 else None
    ntm = None
    fy_end = s.get("next_fy_end")
    if e0 is not None and e1 is not None and fy_end:
        months_left = (fy_end - datetime.fromisoformat(today).replace(tzinfo=timezone.utc).timestamp()) / (30.44 * 86400)
        m = min(12.0, max(0.0, months_left))
        ntm = e0 * m / 12 + e1 * (12 - m) / 12
    return {"pe_trailing_gaap": s.get("trailing_pe") and round(s["trailing_pe"], 1),
            "pe_current_fy": pe(e0), "pe_next_fy": pe(e1), "pe_ntm_est": pe(ntm),
            "eps_current_fy": e0, "eps_next_fy": e1, "eps_ntm_est": ntm and round(ntm, 2),
            "forward_pe_yahoo": s.get("forward_pe") and round(s["forward_pe"], 1),
            "forward_pe_yahoo_basis": "Yahoo forwardPE = 현재가 ÷ 다음 회계연도 컨센서스 EPS (향후 12개월 아님)"}


def data_conflicts(s, d, q):
    """자동 데이터 대조 — 충돌 항목은 AI 가 공식 자료로 검증해야 한다 (미검증 시 신규 편입 보류)."""
    out = []
    m_st, y_st, _ = statement_fcf(s, d)
    m_sum = s["fcf"] / s["revenue"] if s.get("fcf") is not None and s.get("revenue") else None
    if m_st is not None and m_sum is not None and abs(m_st - m_sum) > max(0.05, abs(m_st) * 0.3):
        out.append({"metric": "FCF 마진(TTM)", "values": {"현금흐름표 4분기 합계": f"{m_st * 100:.1f}%",
                                                       "Yahoo 요약(levered FCF)": f"{m_sum * 100:.1f}%"},
                    "why": "FCF 정의·기간이 다름. 기업의 질·재무·가격 점수에 영향"})
    if s.get("forward_eps") and s.get("eps_next_fy") and abs(s["forward_eps"] / s["eps_next_fy"] - 1) > 0.1:
        out.append({"metric": "다음 회계연도 EPS", "values": {"forwardEps": s["forward_eps"], "추정치 추이(+1y)": s["eps_next_fy"]},
                    "why": "추정 PER 기준 EPS 불일치"})
    return out


def build_dossier(tk, snaps, details, scores, hits, holding, today):
    from signals import SECTOR_KR
    s, d, q = snaps[tk], details.get(tk, {}), scores.get(tk, {})
    m_st, y_st, rev_ttm = statement_fcf(s, d)
    lo, hi, e1 = s.get("eps_low_1y"), s.get("eps_high_1y"), s.get("eps_next_fy")
    dossier = {
        "ticker": tk, "name": s["name"], "sector": SECTOR_KR.get(s["sector"], s["sector"]),
        "industry": s.get("industry"), "as_of": today,
        "price": s.get("price"), "market_cap_b": _b(s["market_cap"]),
        "valuation": {
            "trailing_pe": s.get("trailing_pe"), "forward_pe": s.get("forward_pe"), "peg": s.get("peg"),
            **pe_basis(s, today),
            "fcf_yield_pct": round(y_st * 100, 2) if y_st is not None else q.get("fcf_yield_pct"),
            "fcf_yield_basis": "현금흐름표 TTM FCF ÷ 시가총액" if y_st is not None else "Yahoo 요약 FCF ÷ 시가총액",
            "analyst_target_mean": s.get("target_mean"),
            "analyst_rating_mean_1buy_5sell": s.get("rec_mean"), "analysts": s.get("analysts"),
        },
        "eps_estimate_range_next_fy": {"low": lo, "mean": e1, "high": hi,
                                       "spread_pct": round((hi - lo) / abs(e1) * 100, 1) if lo is not None and hi is not None and e1 else None},
        "growth_profitability": {
            "gross_margin_pct": _pct(s.get("gross_margin")), "op_margin_pct": _pct(s.get("op_margin")),
            "roe_pct": _pct(s.get("roe")),
            "revenue_growth_last_q_yoy_pct": _pct(s.get("revenue_growth")),
            "eps_growth_last_q_yoy_pct": _pct(s.get("earnings_growth")),
            "eps_growth_est_this_fy_pct": _pct(s.get("growth_0y")),
            "eps_growth_est_next_fy_pct": _pct(s.get("growth_1y")),
        },
        "cash_debt_dilution": {
            "fcf_ttm_b": _b(d.get("fcf_ttm")), "sbc_ttm_b": _b(d.get("sbc_ttm")), "buyback_ttm_b": _b(d.get("buyback_ttm")),
            "fcf_margin_pct": round(m_st * 100, 1) if m_st is not None else q.get("fcf_margin_pct"),
            "fcf_margin_basis": "현금흐름표 TTM FCF ÷ TTM 매출" if m_st is not None else "Yahoo 요약",
            "revenue_ttm_b": _b(rev_ttm), "net_debt_to_ebitda": q.get("net_debt_ebitda"),
            "share_count_change_pct": d.get("share_change_pct"), "share_count_span": d.get("share_change_span"),
        },
        "estimate_revisions_next_fy_eps": {
            "current": s.get("eps_next_fy"), "chg_7d_pct": s.get("rev_7d") and round(s["rev_7d"], 2),
            "chg_30d_pct": s.get("rev_30d") and round(s["rev_30d"], 2), "chg_90d_pct": s.get("rev_90d") and round(s["rev_90d"], 2),
            "up_revisions_30d": s.get("up_30d"), "down_revisions_30d": s.get("down_30d"),
        },
        "quarterly": {
            "periods": [p for p, _ in d.get("q_revenue", [])],
            "revenue_b": [_b(v) for _, v in d.get("q_revenue", [])],
            "op_margin_pct": [v for _, v in d.get("q_op_margin", [])],
            "diluted_eps": [v for _, v in d.get("q_eps", [])],
        },
        "annual_revenue_b": [[p, _b(v)] for p, v in d.get("y_revenue", [])],
        "eps_surprises": d.get("eps_surprises", []),
        "next_earnings": d.get("next_earnings"),
        "return_1y_pct": d.get("return_1y_pct"), "from_52w_high_pct": d.get("from_52w_high_pct"),
        "quant_score": {k: q.get(k) for k in ("score", "quality", "health", "value", "revisions", "score_before")},
        "data_conflicts": data_conflicts(s, d, q),
        "cycle_check": q.get("cycle_note"),
        "dashboard_signals": hits,
        "holding": None,
    }
    if holding:
        price = s.get("price")
        dossier["holding"] = {
            "added_date": holding.get("added_date"), "added_price": holding.get("added_price"),
            "return_since_add_pct": round((price / holding["added_price"] - 1) * 100, 1)
            if price and holding.get("added_price") else None,
            "original_thesis": holding.get("thesis"), "thesis_breakers": holding.get("thesis_breakers"),
            "last_thesis_status": holding.get("thesis_status"), "last_conviction": holding.get("conviction"),
        }
    return dossier


# ── 1단계: prepare (정량·신호·후보 → 분석 요청서) ─────────────────────────
def prepare():
    from data_fetcher import fetch_prices
    from market_data import fetch_annual_margins, fetch_details, fetch_snapshots
    from news_feed import fetch_candidates
    from picks import (cycle_note, cycle_pool, fresh_analysis_reason, guru_sell_note, load_cache, load_history,
                       pick_candidates, quant_scores, signal_hits)
    from sec_data import check_access, fetch_13f, fetch_insider_trades
    from signals import CAPEX_TICKERS, build_m2, build_m3, build_m4, build_m6, m3_section, m4_section
    from universe import load_universe

    now = datetime.now(KST)
    today, week = now.date().isoformat(), now.strftime("%G-W%V")

    universe = load_universe()
    snaps = fetch_snapshots(universe)
    spy = fetch_prices(["SPY"]).get("SPY")

    sec_ok = check_access()
    m1 = fetch_13f() if sec_ok else None
    m5 = fetch_insider_trades(sorted(snaps)) if sec_ok else None
    m2 = build_m2(snaps)
    m3_tk, m4_tk = build_m3(snaps), build_m4(snaps)

    # 관심종목(data/watchlist.json): 순위와 관계없이 매주 심층분석. 유니버스 밖이면 시세를 따로 수집
    watch = load_watchlist()
    missing = [(t, t) for t in watch if t not in snaps]
    if missing:
        snaps.update(fetch_snapshots(missing))
    print("관심종목:", watch, "(시세 없음: " + str([t for t in watch if t not in snaps]) + ")")
    scores = quant_scores(snaps)

    hist = load_history()
    pool = cycle_pool(scores, snaps, hist["portfolio"], m1, m3_tk, m5)
    margins = fetch_annual_margins(pool)
    cycle_notes = {tk: note for tk in pool if (note := cycle_note(snaps[tk], margins.get(tk, [])))}
    for tk, note in cycle_notes.items():
        scores[tk]["cycle_note"] = note
        print(f"  {tk}: {note}")
    held_tk, new_tk = pick_candidates(scores, snaps, hist["portfolio"], m1, m3_tk, m5, cycle_notes)
    picked_tk = new_tk
    watch_tk = [t for t in watch if t in snaps and t not in held_tk and t not in new_tk]
    new_tk = new_tk + watch_tk
    print("보유:", held_tk, "/ 신규 후보:", picked_tk, "/ 관심종목:", watch_tk)
    details = fetch_details(list(dict.fromkeys(held_tk + new_tk + m3_tk + m4_tk + CAPEX_TICKERS)))
    m6 = build_m6(snaps, details)

    # 분석 대상은 현금흐름표 FCF 로 점수 재계산 (Yahoo 요약 FCF 와 정의가 달라 점수가 왜곡될 수 있음)
    import picks as _picks
    for tk in held_tk + new_tk:
        m_st, y_st, _ = statement_fcf(snaps[tk], details.get(tk, {}))
        if m_st is not None:
            before = scores[tk]["score"]
            scores[tk] = _picks.rescore(tk, scores[tk], _picks.LAST_DIST, m_st, y_st)
            if abs(scores[tk]["score"] - before) >= 1:
                print(f"  {tk}: 현금흐름표 FCF 반영 점수 {before} → {scores[tk]['score']}")

    signals_block = {
        "built_at_kst": now.strftime("%Y-%m-%d %H:%M"), "week": week, "universe_size": len(snaps),
        "m1": m1, "m2": m2, "m3": m3_section(m3_tk, snaps, details), "m4": m4_section(m4_tk, snaps, details),
        "m5": m5, "m6": m6,
        "status": {"sec": m1 is not None and m5 is not None},
    }

    holding_by_tk = {h["ticker"]: h for h in hist["portfolio"]}
    dossiers = [build_dossier(tk, snaps, details, scores,
                              signal_hits(tk, m1, m3_tk, m5, scores) + [n for n in [guru_sell_note(tk, m1)] if n],
                              holding_by_tk.get(tk), today)
                for tk in held_tk + new_tk]

    cache = load_cache()
    fresh_reason = {d["ticker"]: fresh_analysis_reason(cache.get(d["ticker"]), snaps[d["ticker"]],
                                                       holding_by_tk.get(d["ticker"]), today) for d in dossiers}
    to_analyze = [d for d in dossiers if fresh_reason[d["ticker"]]]
    reused = {tk: cache[tk]["analyzed_date"] for tk, r in fresh_reason.items() if r is None}
    print(f"심층분석 대상 {len(dossiers)}종목 → Aside 신규 분석 {len(to_analyze)} / 재사용 {len(reused)}")

    news = fetch_candidates(list(dict.fromkeys(held_tk + watch_tk + picked_tk)))
    tickers = held_tk + new_tk
    request = {
        "week": week, "date": today, "created_at_kst": now.strftime("%Y-%m-%d %H:%M"), "spy_price": spy,
        "held": held_tk, "new_candidates": new_tk, "watchlist": watch_tk,
        "fresh_reason": fresh_reason, "reused": reused,
        "to_analyze": to_analyze,                         # Aside 가 분석할 종목 자료 (dossier)
        "dossiers": dossiers,                             # 리포트 metrics 용 (재사용 포함 전체)
        "market_context": {
            "as_of": today,
            "sector_eps_revisions_30d": [{"sector": x["sector_kr"], "chg_pct": x["eps_revision_pct_30d"],
                                          "momentum": x["momentum"]} for x in m2["sectors"]],
            "cycle_indicators": m6["overall"],
            "current_portfolio": [h["ticker"] for h in hist["portfolio"]],
        },
        "news_candidates": news,
        # finalize 가 시세를 다시 받지 않고 결정할 수 있도록 필요한 값만 보관
        "snaps": {tk: {k: snaps[tk].get(k) for k in ("name", "sector", "price", "eps_next_fy")} for tk in tickers},
        "scores": {tk: scores[tk] for tk in tickers},
        "next_earnings": {tk: details.get(tk, {}).get("next_earnings") for tk in tickers},
        "signals": signals_block,
        "dist": _picks.LAST_DIST,                        # finalize 에서 검증값으로 점수 재계산할 때 사용
        "finalized_at": None,
    }
    try:  # 후보 선정 순위표 (왜 탈락했나 답변용)
        from explain import ranking_table
        request["ranking"] = ranking_table(scores, snaps, hist["portfolio"], m1, m3_tk, m5, cycle_notes, picked_tk,
                                           extra=watch_tk)
    except Exception as e:
        print(f"순위표 생성 실패(무시): {e}")
    _dump(PENDING_PATH, request)
    print(f"분석 요청서 저장: {PENDING_PATH} (week {week})")


# ── 2단계: finalize (Aside 결과 → 결정·이력·리포트) ──────────────────────
def _profile():
    return _load(os.path.join(DATA_DIR, "risk_profile.json"), {}) or {}


def _archive(week, payload):
    """당시 추천·원자료 보존 — 덮어쓰지 않고 실행 시각별 파일로 쌓는다."""
    stamp = datetime.now(KST).strftime("%Y%m%d-%H%M%S")
    _dump(os.path.join(DATA_DIR, "archive", week.replace("+", "_"), f"{stamp}.json"), payload)


def _apply_verified(tk, a, scores, dist, dossier):
    """AI 가 공식 자료로 검증한 FCF 확정값으로 점수를 다시 계산하고 리포트 지표도 교체."""
    import picks
    vm = (a or {}).get("verified_metrics") or {}
    fm, fy = vm.get("fcf_margin_pct"), vm.get("fcf_yield_pct")
    if (fm is None and fy is None) or tk not in scores:
        return None
    before = scores[tk]["score"]
    scores[tk] = picks.rescore(tk, scores[tk], dist, None if fm is None else fm / 100, None if fy is None else fy / 100)
    if dossier is not None:
        c, v = dossier.setdefault("cash_debt_dilution", {}), dossier.setdefault("valuation", {})
        if fm is not None:
            c["fcf_margin_pct"], c["fcf_margin_basis"] = fm, "AI 검증 확정값 (공식 현금흐름표)"
        if fy is not None:
            v["fcf_yield_pct"], v["fcf_yield_basis"] = fy, "AI 검증 확정값"
        dossier["quant_score"] = {k: scores[tk].get(k) for k in
                                  ("score", "quality", "health", "value", "revisions", "score_before")}
    return {"before": before, "after": scores[tk]["score"]}


def _risk_info(a, price, weight):
    """판단표 요약: 행동·가격 조건·시나리오별 수익률·비중·전체 자산 영향."""
    dt = (a or {}).get("decision_table") or {}
    out = {"action": dt.get("action"), "buy_below": dt.get("buy_below"), "weight": weight or 0, "price": price}
    key = {"약세": "bear", "기준": "base", "강세": "bull"}
    for s in dt.get("scenarios", []):
        if price and s.get("name") in key:
            out[key[s["name"]] + "_pct"] = round((s["price"] / price - 1) * 100, 1)
    if out.get("bear_pct") is not None:
        out["portfolio_impact_pct"] = round((weight or 0) * out["bear_pct"] / 100, 2)
    return out


def _rewind_week(hist, week):
    """같은 주를 다시 finalize 할 때 그 주 결정 이전 상태로 되돌린다 (재실행해도 결과가 같도록).
    그 주에 편입된 종목은 보유에서 빼고, 그 주에 제외된 종목은 보유로 되돌린다."""
    last = next((w for w in hist.get("weeks", []) if w["week"] == week and not w.get("interim")), None)
    if not last:
        return hist
    d0 = last["date"]
    port = [h for h in hist.get("portfolio", []) if h.get("added_date") != d0]
    out_tk = {d["ticker"] for d in last.get("decisions", []) if d["action"] == "제외"}
    back = [c for c in hist.get("closed", []) if c.get("removed_date") == d0 and c["ticker"] in out_tk]
    for c in back:
        port.append({"ticker": c["ticker"], "name": c.get("name"), "added_date": c.get("added_date"),
                     "added_price": c.get("added_price"), "thesis_status": "유효"})
    hist["closed"] = [c for c in hist.get("closed", []) if c not in back]
    if len(port) != len(hist.get("portfolio", [])) or back:
        print(f"{week} 재실행 — 이번 주 결정 이전 보유로 되돌림: {[h['ticker'] for h in port]}")
    hist["portfolio"] = port
    return hist


def finalize():
    import aside_bridge
    from picks import cache_entry, decide, load_cache, load_history, save_cache, save_history, update_history

    req = _load(PENDING_PATH)
    res = _load(RESULT_PATH)
    if not req:
        sys.exit("::error::data/pending/request.json 없음 — prepare 먼저 실행")
    if not res:
        sys.exit("::error::data/aside/result.json 없음")
    if res.get("mode") == "interim":
        return finalize_interim(req, res)
    if req.get("finalized_at") and res.get("week") == req["week"] and not os.environ.get("FORCE_FINALIZE") \
            and res.get("analyzed_at") == req.get("finalized_result_at"):
        print(f"{req['week']} 이미 반영됨 ({req['finalized_at']}) — 건너뜀")
        return

    week, today, spy = req["week"], req["date"], req.get("spy_price")
    snaps, scores, next_e = req["snaps"], req["scores"], req.get("next_earnings", {})
    held_tk, new_tk = req["held"], req["new_candidates"]
    watch_tk = set(req.get("watchlist") or [])
    old_weekly = _load(os.path.join(DATA_DIR, "weekly_signals.json"), {}) or {}

    if res.get("week") != week:
        sys.exit(f"::error::결과 주차({res.get('week')})가 요청서({week})와 다름 — 반영 중단")
    brief, fresh, errors = aside_bridge.load_result(req, res)
    for e in errors:
        print("::warning::" + e)

    weekly = {**req["signals"], "market_brief": brief}
    ok = [tk for tk, a in fresh.items() if a]
    weekly["status"] = {**weekly.get("status", {}), "claude": bool(brief) or bool(ok), "engine": "aside"}

    if fresh and not ok:
        print("::error::Aside 심층분석 결과가 전부 무효 — 추천 이력 갱신 건너뜀, 신호·브리핑만 반영")
    else:
        cache = load_cache()
        hist = _rewind_week(load_history(), week)
        reused = {tk: cache[tk] for tk in req.get("reused", {}) if tk in cache and not fresh.get(tk)}
        for tk, a in fresh.items():
            if a is not None:
                cache[tk] = cache_entry(a, snaps[tk], {"next_earnings": next_e.get(tk)}, today)
        save_cache(cache, today)

        analyses = {**{tk: c["analysis"] for tk, c in reused.items()}, **{tk: a for tk, a in fresh.items() if a}}
        dossier_by_tk = {d["ticker"]: d for d in req["dossiers"]}

        # ① 검증된 확정값으로 점수 재계산 (AI 가 오류를 설명만 하고 끝나지 않도록)
        rescored = {}
        for tk, a in analyses.items():
            r = _apply_verified(tk, a, scores, req.get("dist") or {}, dossier_by_tk.get(tk))
            if r:
                rescored[tk] = r
                print(f"  {tk}: 검증값 반영 점수 {r['before']} → {r['after']}")

        kept, decisions, removed = decide(hist["portfolio"], new_tk, analyses, snaps, scores, today)
        for dcs in decisions:
            if dcs["ticker"] in watch_tk and dcs["ticker"] not in held_tk:
                dcs["reason"] = "[관심종목] " + dcs["reason"]
            if dcs["ticker"] in reused:
                dcs["reason"] = (f"[{reused[dcs['ticker']]['analyzed_date']} 분석 재사용 — 이후 실적 발표·큰 주가 변동·"
                                 f"추정치 급변 없음] " + dcs["reason"])

        # ② 종목 선택과 투자 규모 분리: 자산배분(스탠스) → 위험 기반 종목 비중 → 코어 ETF → 현금
        prev_weights = next((w.get("weights") for w in reversed(hist["weeks"]) if w["week"] != week and w.get("weights")),
                            None) or {}
        stance = (brief or {}).get("stance") or (old_weekly.get("market_brief") or {}).get("stance") or "중립"
        prices = {tk: s.get("price") for tk, s in snaps.items()}
        meta = {tk: {"sector": s.get("sector"), "industry": (dossier_by_tk.get(tk) or {}).get("industry")}
                for tk, s in snaps.items()}
        weights, alloc = aside_bridge.size_portfolio([h["ticker"] for h in kept], res.get("target_weights"), prev_weights,
                                                     analyses, prices, meta, stance, _profile(), res.get("allocation"),
                                                     new_tickers=[d["ticker"] for d in decisions if d["action"] == "신규편입"])
        alloc["agent_reason"] = res.get("weights_reason")
        for n in alloc["notes"]:
            print("  비중: " + n)

        hist = update_history(hist, week, today, kept, decisions, removed, snaps, spy)
        hist["weeks"][-1]["weights"] = weights
        hist["weeks"][-1]["allocation"] = {k: alloc[k] for k in
                                           ("stance", "equity_pct", "stock_pct", "core_etf", "core_pct", "cash_pct", "reason", "expected_return_pct")}
        for h in hist["portfolio"]:
            a = analyses.get(h["ticker"]) or {}
            h["weight"] = weights.get(h["ticker"])
            h["alert_level"] = a.get("alert_level") or h.get("alert_level")
            h["evidence"] = a.get("evidence") or h.get("evidence")
            h["action"] = (a.get("decision_table") or {}).get("action") or h.get("action")
        save_history(hist)
        print("비중:", weights, f"현금 {100 - sum(weights.values())}%")

        report = {
            "week": week, "date": today, "spy_price": spy,
            "candidates": [{"ticker": tk, "role": "보유" if tk in held_tk else ("관심종목" if tk in watch_tk else "신규후보"),
                            **{k: v for k, v in scores[tk].items() if k != "parts"},
                            "analysis": "재사용" if tk in reused else (req["fresh_reason"].get(tk) or "")}
                           for tk in held_tk + new_tk],
            "analyses": {tk: {"analysis": a, "metrics": {k: v for k, v in dossier_by_tk[tk].items() if k != "holding"},
                              "reused_from": reused[tk]["analyzed_date"] if tk in reused else None,
                              "risk": _risk_info(a, prices.get(tk), weights.get(tk)),
                              "rescored": rescored.get(tk)}
                         for tk, a in analyses.items() if a and tk in dossier_by_tk},
            "decisions": decisions,
            "weights": weights, "allocation": alloc, "weights_reason": res.get("weights_reason"),
            "ranking": req.get("ranking"),
            "usage": {"aside": {"engine": "Aside (무료)", "analyzed": len(ok), "reused": len(reused),
                                "failed": [tk for tk, a in fresh.items() if not a], "est_cost_usd": 0,
                                "analyzed_at": res.get("analyzed_at")}},
        }
        _dump(os.path.join(DATA_DIR, "reports", f"{week}.json"), report)
        _archive(week, {"kind": "weekly", "finalized_at": datetime.now(KST).isoformat(),
                        "request": {k: req.get(k) for k in ("week", "date", "created_at_kst", "spy_price", "held",
                                                            "new_candidates", "watchlist", "fresh_reason", "reused",
                                                            "dossiers", "scores", "snaps", "market_context")},
                        "result": res, "decisions": decisions, "weights": weights, "allocation": alloc})
        print("결정:", [(x["ticker"], x["action"]) for x in decisions])

    _dump(os.path.join(DATA_DIR, "weekly_signals.json"), weekly)
    req["finalized_at"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    req["finalized_result_at"] = res.get("analyzed_at")
    req["finalize_errors"] = errors
    _dump(PENDING_PATH, req)
    print("finalize 완료")


def finalize_interim(req, res):
    """중간 재평가(수요일 등): 실적 발표·급변 종목만 다시 분석해 보유 판단·비중을 갱신. 신규 편입은 하지 않음."""
    import aside_bridge
    from picks import cache_entry, load_cache, load_history, save_cache, save_history

    iid = res.get("interim_id") or res.get("analyzed_at")
    if iid in (req.get("interims") or []) and not os.environ.get("FORCE_FINALIZE"):
        print(f"중간 재평가 {iid} 이미 반영됨 — 건너뜀")
        return
    hist = load_history()
    held = [h["ticker"] for h in hist["portfolio"]]
    today = datetime.now(KST).date().isoformat()
    valid, errs = {}, []
    for tk, a in (res.get("analyses") or {}).items():
        e = aside_bridge.validate_analysis(a, tk)
        if e:
            errs += e
        else:
            valid[tk] = {k: a[k] for k in aside_bridge.ANALYSIS_SCHEMA}
    for e in errs:
        print("::warning::" + e)
    if not valid:
        sys.exit("::error::중간 재평가 결과가 모두 무효")
    prices = {k: float(v) for k, v in (res.get("prices") or {}).items() if v}
    cache = load_cache()
    for tk, a in valid.items():
        snap = {"price": prices.get(tk) or (cache.get(tk) or {}).get("price"),
                "eps_next_fy": (cache.get(tk) or {}).get("eps_next_fy")}
        cache[tk] = cache_entry(a, snap, {"next_earnings": res.get("next_earnings", {}).get(tk)}, today)
    save_cache(cache, today)

    analyses = {tk: (cache.get(tk) or {}).get("analysis") for tk in held}
    analyses.update(valid)
    decisions, kept, removed = [], [], []
    for h in hist["portfolio"]:
        tk, a = h["ticker"], analyses.get(h["ticker"]) or {}
        if tk in valid and (a.get("alert_level") == "논리 붕괴" or a.get("thesis_status") == "붕괴" or a.get("verdict") == "부적합"):
            removed.append(h)
            decisions.append({"ticker": tk, "action": "제외", "reason": "[중간 재평가] " + a.get("thesis_status_reason", "")})
        else:
            if tk in valid:
                h = {**h, "thesis_status": a.get("thesis_status") if a.get("thesis_status") != "해당없음" else h.get("thesis_status"),
                     "alert_level": a.get("alert_level"), "conviction": a.get("conviction"), "evidence": a.get("evidence"),
                     "action": (a.get("decision_table") or {}).get("action"), "last_review": today}
                decisions.append({"ticker": tk, "action": "유지",
                                  "reason": f"[중간 재평가 · {a.get('alert_level')}] " + a.get("thesis_status_reason", "")})
            kept.append(h)
    prev_weights = next((w.get("weights") for w in reversed(hist["weeks"]) if w.get("weights")), None) or {}
    weekly = _load(os.path.join(DATA_DIR, "weekly_signals.json"), {}) or {}
    stance = (weekly.get("market_brief") or {}).get("stance") or "중립"
    px = {tk: prices.get(tk) or (cache.get(tk) or {}).get("price") for tk in held}
    meta = {h["ticker"]: {"sector": h.get("sector")} for h in hist["portfolio"]}
    keep_w = {tk: v for tk, v in prev_weights.items() if tk in [h["ticker"] for h in kept]}
    weights, alloc = aside_bridge.size_portfolio([h["ticker"] for h in kept], res.get("target_weights") or keep_w,
                                                 prev_weights, analyses, px, meta, stance, _profile(),
                                                 res.get("allocation"))
    alloc["agent_reason"] = res.get("weights_reason")
    for h in removed:
        p = px.get(h["ticker"])
        hist["closed"].append({"ticker": h["ticker"], "name": h.get("name"), "added_date": h.get("added_date"),
                               "removed_date": today, "added_price": h.get("added_price"), "removed_price": p,
                               "return_pct": round((p / h["added_price"] - 1) * 100, 1) if p and h.get("added_price") else None,
                               "spy_return_pct": None, "reason": decisions[-1]["reason"] if decisions else ""})
    for h in kept:
        h["weight"] = weights.get(h["ticker"])
    hist["portfolio"] = kept
    hist["weeks"].append({"week": f"{req['week']}+중간점검", "date": today, "interim": True, "decisions": decisions,
                          "portfolio_after": [h["ticker"] for h in kept], "weights": weights,
                          "allocation": {k: alloc[k] for k in ("stance", "equity_pct", "stock_pct", "core_etf", "core_pct",
                                                               "cash_pct", "reason", "expected_return_pct")},
                          "trigger": res.get("trigger")})
    save_history(hist)

    rep_path = os.path.join(DATA_DIR, "reports", f"{req['week']}.json")
    rep = _load(rep_path, {}) or {}
    for tk, a in valid.items():
        if tk in rep.get("analyses", {}):
            rep["analyses"][tk]["analysis"] = a
            rep["analyses"][tk]["interim_update"] = today
            rep["analyses"][tk]["risk"] = _risk_info(a, px.get(tk), weights.get(tk))
    rep.setdefault("interim_notes", []).append({"date": today, "tickers": list(valid), "trigger": res.get("trigger"),
                                                "weights": weights})
    _dump(rep_path, rep)
    _archive(req["week"] + "+interim", {"kind": "interim", "result": res, "decisions": decisions, "weights": weights,
                                        "allocation": alloc})
    req.setdefault("interims", []).append(iid)
    _dump(PENDING_PATH, req)
    print("중간 재평가 반영:", decisions, weights)


if __name__ == "__main__":
    sys.path.insert(0, BASE_DIR)
    mode = sys.argv[1] if len(sys.argv) > 1 else "prepare"
    {"prepare": prepare, "finalize": finalize}[mode]()
