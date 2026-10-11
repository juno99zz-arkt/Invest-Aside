"""
후보 선정 순위 진단 — "왜 이 종목은 심층분석 후보에 안 들어갔나?"

ranking_table() : prepare 와 같은 규칙으로 전 종목 순위표(정량 점수·가산·감점·자격·탈락 사유)를 만든다.
                  run_weekly.prepare 가 request.json 의 ranking 으로 저장한다 (상위 40 + 조회 종목).
python explain.py AMD,AVGO : 지금 시세로 다시 계산해 data/diagnostics/ranking_<날짜>.json 저장 + 로그 출력.
                             가산점 신호(M1·M3·M5)는 최근 request.json 의 값을 그대로 쓴다.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KST = timezone(timedelta(hours=9))


def _why_ineligible(s):
    out = []
    if not (s.get("forward_eps") or 0) > 0:
        out.append("향후 12개월 추정 EPS 적자")
    if (s.get("analysts") or 0) < 5:
        out.append(f"애널리스트 {s.get('analysts') or 0}명(<5)")
    if not (s.get("op_margin") or 0) > 0 and not ((s.get("eps_1q") or 0) > 0 and (s.get("rev_30d") or 0) >= 0):
        out.append("영업이익률 적자 + 흑자전환 임박 조건 미충족")
    return out


def ranking_table(scores, snaps, holdings, m1, m3_tk, m5, cycle_notes, picked, extra=(), top=40):
    from picks import (CYCLE_PENALTY, GURU_SELL_PENALTY, MAX_PER_INDUSTRY_CANDIDATES, NEW_CANDIDATES, _signals,
                       guru_sell_note, is_cyclical)
    held = {h["ticker"] for h in holdings}
    rows = {}
    for tk, sc in scores.items():
        s = snaps[tk]
        sig = _signals(tk, m1, m3_tk, m5, scores)
        bonus = sum(p for _, p in sig)
        pens = []
        if tk in cycle_notes:
            pens.append((cycle_notes[tk], CYCLE_PENALTY))
        if guru_sell_note(tk, m1):
            pens.append((guru_sell_note(tk, m1), GURU_SELL_PENALTY))
        rows[tk] = {
            "ticker": tk, "name": s.get("name"), "sector": s.get("sector"), "industry": s.get("industry"),
            "score": sc["score"], "quality": sc["quality"], "health": sc["health"], "value": sc["value"],
            "revisions": sc["revisions"], "eligible": sc["eligible"],
            "bonus": bonus, "bonus_items": [f"{l} (+{p})" for l, p in sig],
            "penalty": sum(p for _, p in pens), "penalty_items": [l for l, _ in pens],
            "final": round(sc["score"] + bonus - sum(p for _, p in pens), 1),
            "cyclical": is_cyclical(s),
            "metrics": {k: s.get(k) for k in ("price", "forward_pe", "peg", "op_margin", "gross_margin", "revenue_growth",
                                               "growth_1y", "rev_30d", "rev_90d", "up_30d", "down_30d", "analysts")},
        }
    ranked = sorted([r for r in rows.values() if r["eligible"] and r["ticker"] not in held], key=lambda r: -r["final"])
    per_ind, n_picked = {}, 0
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
        ind = r["industry"] or r["ticker"]
        if r["ticker"] in picked:
            r["status"] = "심층분석 후보 선정"
            per_ind[ind] = per_ind.get(ind, 0) + 1
            n_picked += 1
        elif n_picked >= NEW_CANDIDATES:
            r["status"] = f"순위 밖 (상위 {NEW_CANDIDATES}개 선정 완료 후)"
        elif per_ind.get(ind, 0) >= MAX_PER_INDUSTRY_CANDIDATES:
            r["status"] = f"같은 업종({ind}) 후보 {MAX_PER_INDUSTRY_CANDIDATES}개 한도 초과"
        else:
            r["status"] = "순위 밖"
    cutoff = min((r["final"] for r in ranked if r["ticker"] in picked), default=None)
    for tk, r in rows.items():
        if tk in held:
            r["status"] = "보유 종목 (매주 자동 재점검)"
        elif not r["eligible"]:
            r["status"] = "기본 조건 미달: " + ", ".join(_why_ineligible(snaps[tk]) or ["기본 조건 미달"])
    keep = {r["ticker"] for r in ranked[:top]} | set(picked) | held | {t for t in extra if t in rows}
    return {"cutoff_final": cutoff, "eligible_count": len(ranked), "universe": len(rows),
            "rows": sorted([rows[t] for t in keep], key=lambda r: (r.get("rank") or 9999, r["ticker"]))}


def main(targets):
    sys.path.insert(0, BASE_DIR)
    from market_data import fetch_annual_margins, fetch_snapshots
    from picks import cycle_note, cycle_pool, load_history, pick_candidates, quant_scores
    from universe import load_universe

    req_path = os.path.join(BASE_DIR, "data", "pending", "request.json")
    req = json.load(open(req_path, encoding="utf-8")) if os.path.exists(req_path) else {}
    sig = req.get("signals") or {}
    m1, m5 = sig.get("m1"), sig.get("m5")
    m3_tk = [c["ticker"] for c in (sig.get("m3") or {}).get("companies", [])]

    snaps = fetch_snapshots(load_universe())
    scores = quant_scores(snaps)
    hist = load_history()
    pool = cycle_pool(scores, snaps, hist["portfolio"], m1, m3_tk, m5)
    check = list(dict.fromkeys(pool + [t for t in targets if t in snaps]))
    margins = fetch_annual_margins(check)
    notes = {tk: n for tk in check if (n := cycle_note(snaps[tk], margins.get(tk, [])))}
    for tk, n in notes.items():
        scores[tk]["cycle_note"] = n
    # 사이클 할인은 원래 '정량 상위 30 중 경기순환 업종'에만 적용 → 조회 종목이 그 밖이면 참고용으로만 표시
    applied = {tk: n for tk, n in notes.items() if tk in pool}
    _, picked = pick_candidates(scores, snaps, hist["portfolio"], m1, m3_tk, m5, applied)
    table = ranking_table(scores, snaps, hist["portfolio"], m1, m3_tk, m5, applied, picked, extra=targets)
    table["reference_cycle_notes"] = {tk: n for tk, n in notes.items() if tk not in pool}
    table["cycle_pool"] = pool
    table["picked_now"] = picked
    table["picked_in_request"] = req.get("new_candidates")
    table["signals_from"] = req.get("week")
    table["built_at_kst"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M")

    out_dir = os.path.join(BASE_DIR, "data", "diagnostics")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"ranking_{datetime.now(KST).date().isoformat()}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(table, f, ensure_ascii=False, indent=1)
    print(f"유니버스 {table['universe']} / 자격 {table['eligible_count']} / 선정 컷 {table['cutoff_final']} / 지금 선정 {picked}")
    by = {r["ticker"]: r for r in table["rows"]}
    for t in targets:
        r = by.get(t)
        if not r:
            print(f"{t}: 유니버스에 없음")
            continue
        print(json.dumps(r, ensure_ascii=False, indent=1))
        if t in table["reference_cycle_notes"]:
            print("  (참고) " + table["reference_cycle_notes"][t])
    print("저장:", path)


if __name__ == "__main__":
    tg = [t.strip().upper() for t in (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("EXPLAIN_TICKERS", "")).split(",") if t.strip()]
    main(tg)
