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


def build_dossier(tk, snaps, details, scores, hits, holding, today):
    from signals import SECTOR_KR
    s, d, q = snaps[tk], details.get(tk, {}), scores.get(tk, {})
    dossier = {
        "ticker": tk, "name": s["name"], "sector": SECTOR_KR.get(s["sector"], s["sector"]),
        "industry": s.get("industry"), "as_of": today,
        "price": s.get("price"), "market_cap_b": _b(s["market_cap"]),
        "valuation": {
            "trailing_pe": s.get("trailing_pe"), "forward_pe": s.get("forward_pe"), "peg": s.get("peg"),
            "fcf_yield_pct": q.get("fcf_yield_pct"), "analyst_target_mean": s.get("target_mean"),
            "analyst_rating_mean_1buy_5sell": s.get("rec_mean"), "analysts": s.get("analysts"),
        },
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
            "fcf_margin_pct": q.get("fcf_margin_pct"), "net_debt_to_ebitda": q.get("net_debt_ebitda"),
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
        "quant_score": {k: q.get(k) for k in ("score", "quality", "health", "value", "revisions")},
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
    scores = quant_scores(snaps)

    hist = load_history()
    pool = cycle_pool(scores, snaps, hist["portfolio"], m1, m3_tk, m5)
    margins = fetch_annual_margins(pool)
    cycle_notes = {tk: note for tk in pool if (note := cycle_note(snaps[tk], margins.get(tk, [])))}
    for tk, note in cycle_notes.items():
        scores[tk]["cycle_note"] = note
        print(f"  {tk}: {note}")
    held_tk, new_tk = pick_candidates(scores, snaps, hist["portfolio"], m1, m3_tk, m5, cycle_notes)
    print("보유:", held_tk, "/ 신규 후보:", new_tk)
    details = fetch_details(held_tk + new_tk + m3_tk + m4_tk + CAPEX_TICKERS)
    m6 = build_m6(snaps, details)

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

    news = fetch_candidates([h["ticker"] for h in hist["portfolio"]])
    tickers = held_tk + new_tk
    request = {
        "week": week, "date": today, "created_at_kst": now.strftime("%Y-%m-%d %H:%M"), "spy_price": spy,
        "held": held_tk, "new_candidates": new_tk,
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
        "finalized_at": None,
    }
    _dump(PENDING_PATH, request)
    print(f"분석 요청서 저장: {PENDING_PATH} (week {week})")


# ── 2단계: finalize (Aside 결과 → 결정·이력·리포트) ──────────────────────
def finalize():
    import aside_bridge
    from picks import cache_entry, decide, load_cache, load_history, save_cache, save_history, update_history

    req = _load(PENDING_PATH)
    res = _load(RESULT_PATH)
    if not req:
        sys.exit("::error::data/pending/request.json 없음 — prepare 먼저 실행")
    if not res:
        sys.exit("::error::data/aside/result.json 없음")
    if req.get("finalized_at") and res.get("week") == req["week"] and not os.environ.get("FORCE_FINALIZE"):
        print(f"{req['week']} 이미 반영됨 ({req['finalized_at']}) — 건너뜀")
        return

    week, today, spy = req["week"], req["date"], req.get("spy_price")
    snaps, scores, next_e = req["snaps"], req["scores"], req.get("next_earnings", {})
    held_tk, new_tk = req["held"], req["new_candidates"]

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
        hist = load_history()
        reused = {tk: cache[tk] for tk in req.get("reused", {}) if tk in cache}
        for tk, a in fresh.items():
            if a is not None:
                cache[tk] = cache_entry(a, snaps[tk], {"next_earnings": next_e.get(tk)}, today)
        save_cache(cache, today)

        analyses = {**{tk: c["analysis"] for tk, c in reused.items()}, **fresh}
        kept, decisions, removed = decide(hist["portfolio"], new_tk, analyses, snaps, scores, today)
        for dcs in decisions:
            if dcs["ticker"] in reused:
                dcs["reason"] = (f"[{reused[dcs['ticker']]['analyzed_date']} 분석 재사용 — 이후 실적 발표·큰 주가 변동·"
                                 f"추정치 급변 없음] " + dcs["reason"])
        prev_weights = next((w.get("weights") for w in reversed(hist["weeks"]) if w["week"] != week and w.get("weights")),
                            None) or {}
        weights, w_notes = aside_bridge.resolve_weights(res.get("target_weights"), [h["ticker"] for h in kept],
                                                        prev_weights)
        for n in w_notes:
            print("::warning::" + n)
        hist = update_history(hist, week, today, kept, decisions, removed, snaps, spy)
        hist["weeks"][-1]["weights"] = weights
        for h in hist["portfolio"]:
            h["weight"] = weights.get(h["ticker"])
        save_history(hist)
        print("비중:", weights, f"현금 {100 - sum(weights.values())}%")

        dossier_by_tk = {d["ticker"]: d for d in req["dossiers"]}
        report = {
            "week": week, "date": today, "spy_price": spy,
            "candidates": [{"ticker": tk, "role": "보유" if tk in held_tk else "신규후보", **scores[tk],
                            "analysis": "재사용" if tk in reused else (req["fresh_reason"].get(tk) or "")}
                           for tk in held_tk + new_tk],
            "analyses": {tk: {"analysis": a, "metrics": {k: v for k, v in dossier_by_tk[tk].items() if k != "holding"},
                              "reused_from": reused[tk]["analyzed_date"] if tk in reused else None}
                         for tk, a in analyses.items() if a and tk in dossier_by_tk},
            "decisions": decisions,
            "weights": weights, "weights_reason": res.get("weights_reason"),
            "usage": {"aside": {"engine": "Aside (무료)", "analyzed": len(ok), "reused": len(reused),
                                "failed": [tk for tk, a in fresh.items() if not a], "est_cost_usd": 0,
                                "analyzed_at": res.get("analyzed_at")}},
        }
        _dump(os.path.join(DATA_DIR, "reports", f"{week}.json"), report)
        print("결정:", [(x["ticker"], x["action"]) for x in decisions])

    _dump(os.path.join(DATA_DIR, "weekly_signals.json"), weekly)
    req["finalized_at"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    req["finalize_errors"] = errors
    _dump(PENDING_PATH, req)
    print("finalize 완료")


if __name__ == "__main__":
    sys.path.insert(0, BASE_DIR)
    mode = sys.argv[1] if len(sys.argv) > 1 else "prepare"
    {"prepare": prepare, "finalize": finalize}[mode]()
