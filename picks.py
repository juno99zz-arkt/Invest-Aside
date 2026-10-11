"""
6개월+ 보유 목적 주간 추천 엔진.
1) quant_scores   : 유니버스 정량 점수 (품질·재무건전성·가격매력·추정치 흐름, 퍼센타일 기반)
2) pick_candidates: 기존 보유 + 신규 후보 상위 N (같은 업종 2개 제한, 경기순환 사이클 정점 할인)
3) decide         : Claude 심층분석 결과 → 신규편입/유지/제외 (규칙 기반, 억지 편입 없음)
4) 이력 파일(data/picks_history.json) 갱신
"""
import json
import os
from datetime import date, timedelta

import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_PATH = os.path.join(BASE_DIR, "data", "picks_history.json")
CACHE_PATH = os.path.join(BASE_DIR, "data", "analysis_cache.json")

REUSE_MAX_DAYS = 28         # 이 기간 안의 분석은 변화가 없으면 재사용
REUSE_PRICE_MOVE = 0.15     # 분석 시점 대비 주가 ±15% 이상 → 재분석
REUSE_EPS_MOVE = 0.05       # 내년 EPS 컨센서스 ±5% 이상 변화 → 재분석

MAX_HOLDINGS = 5
MAX_PER_SECTOR = 2
NEW_CANDIDATES = 8
MIN_CONVICTION_NEW = 4      # 신규 편입 최소 확신도 (1~5)
MIN_EXPECTED_RETURN = 10    # 확신도 3 탐색 편입에 필요한 최소 기대수익률(%, 약세25·기준50·강세25)
SWAP_CONVICTION = 5         # 만석일 때 교체하려면 후보 확신도 5 + 기존 종목 '약화'·확신도 3 이하

# 정량 점수 영역 비중 (합계 1.0)
WEIGHTS = {"quality": 0.25, "health": 0.20, "value": 0.25, "revisions": 0.30}

SIGNAL_POINTS = {           # 대시보드 신호별 가산점
    "guru_top10": 3,        # 대가 5인 13F 상위 10 보유
    "guru_buy": 5,          # 대가 5인 최근분기 매수 (신규·주식 수 10%↑)
    "m3": 3,                # 실적 개선 기업
    "insider_buy": 5,       # 최근 14일 내부자 장내 매수
    "turnaround": 10,       # 흑자전환 임박으로 기본 조건 통과
}
GURU_SELL_PENALTY = 5       # 대가 5인 중 최근분기 매도(청산·주식 수 10%↓) 시 할인

# 후보 다양화: 같은 업종 쏠림 방지 + 경기순환 업종의 사이클 정점 이익 할인
MAX_PER_INDUSTRY_CANDIDATES = 2
CYCLE_POOL = 30             # 정량 상위 30 중 경기순환 업종만 연간 마진 추가 수집
CYCLE_MARGIN_RATIO = 1.5    # 현재 영업이익률 ≥ 최근 연간 평균 × 1.5
CYCLE_MARGIN_GAP = 10       # 그리고 평균 + 10%p 이상이면 정점으로 판단
CYCLE_PENALTY = 15
CYCLICAL_SECTORS = {"Energy", "Basic Materials"}
CYCLICAL_INDUSTRY_WORDS = ("Semiconductor", "Computer Hardware", "Electronic Components", "Auto", "Airlines",
                           "Chemicals", "Steel", "Aluminum", "Copper", "Metals", "Mining", "Oil", "Coal",
                           "Residential Construction", "Building Products", "Farm & Heavy", "Trucking",
                           "Marine Shipping", "Railroads", "Travel Services", "Lodging", "Resorts",
                           "Recreational Vehicles", "Packaging", "Paper", "Lumber")


# ── 1. 정량 점수 ──────────────────────────────────────────────────
def _rank(series, higher_better=True, by=None):
    """퍼센타일(0~1). by 지정 시 그룹(섹터) 내 순위. 결측은 0.5(중립)."""
    if by is None:
        r = series.rank(pct=True, ascending=higher_better)
    else:
        r = series.groupby(by).rank(pct=True, ascending=higher_better)
    return r.fillna(0.5)


def quant_scores(snaps):
    df = pd.DataFrame(snaps.values()).set_index("ticker")
    fcf_margin = df["fcf"] / df["revenue"]
    fcf_yield = df["fcf"] / df["market_cap"]
    net_debt_ebitda = (df["total_debt"].fillna(0) - df["total_cash"].fillna(0)) / df["ebitda"].where(df["ebitda"] > 0)
    fwd_pe = df["forward_pe"].where(df["forward_pe"] > 0)
    peg = df["peg"].where(df["peg"] > 0)
    breadth = (df["up_30d"].fillna(0) - df["down_30d"].fillna(0)) / (df["up_30d"].fillna(0) + df["down_30d"].fillna(0)).replace(0, float("nan"))

    qp = {"gross": _rank(df["gross_margin"]), "op": _rank(df["op_margin"]), "roe": _rank(df["roe"].clip(-1, 1)),
          "fcf_margin": _rank(fcf_margin), "rev_growth": _rank(df["revenue_growth"]), "eps_growth": _rank(df["growth_1y"])}
    quality = sum(qp.values()) / 6
    fcf_positive = (fcf_margin > 0).astype(float).where(fcf_margin.notna())
    hp = {"net_debt": _rank(net_debt_ebitda, higher_better=False), "fcf_positive": _rank(fcf_positive)}
    health = sum(hp.values()) / 2
    vp = {"fwd_pe": _rank(fwd_pe, higher_better=False, by=df["sector"]), "fcf_yield": _rank(fcf_yield, by=df["sector"]),
          "peg": _rank(peg, higher_better=False)}
    value = sum(vp.values()) / 3
    revisions = (_rank(df["rev_30d"]) + _rank(df["rev_90d"]) + _rank(breadth)) / 3
    # 후보 점수 재계산(rescore)용 분포 — 검증된 FCF 값으로 해당 종목의 백분위를 다시 구한다
    global LAST_DIST
    LAST_DIST = {
        "fcf_margin": {tk: (None if pd.isna(v) else round(float(v), 4)) for tk, v in fcf_margin.items()},
        "fcf_yield": {tk: (None if pd.isna(v) else round(float(v), 5)) for tk, v in fcf_yield.items()},
        "sector": {tk: s for tk, s in df["sector"].items()},
    }

    w = WEIGHTS
    total = (quality * w["quality"] + health * w["health"] + value * w["value"] + revisions * w["revisions"]) * 100
    # 영업이익률 흑자, 또는 흑자전환 임박(다음 분기 EPS 컨센서스 흑자 + 최근 30일 추정치 하향 없음)
    turnaround = (df["eps_1q"] > 0) & (df["rev_30d"] >= 0)
    eligible = ((df["forward_eps"] > 0) & (df["analysts"].fillna(0) >= 5)
                & ((df["op_margin"].fillna(0) > 0) | turnaround))

    out = {}
    for tk in df.index:
        out[tk] = {
            "score": round(float(total[tk]), 1),
            "quality": round(float(quality[tk]) * 100), "health": round(float(health[tk]) * 100),
            "value": round(float(value[tk]) * 100), "revisions": round(float(revisions[tk]) * 100),
            "eligible": bool(eligible[tk]),
            # 영업이익률은 아직 적자지만 흑자전환 임박 조건으로 자격을 얻은 종목 → 가산점 대상
            "turnaround": bool(eligible[tk] and not (df["op_margin"].fillna(0)[tk] > 0)),
            "fcf_margin_pct": None if pd.isna(fcf_margin[tk]) else round(float(fcf_margin[tk]) * 100, 1),
            "fcf_yield_pct": None if pd.isna(fcf_yield[tk]) else round(float(fcf_yield[tk]) * 100, 2),
            "net_debt_ebitda": None if pd.isna(net_debt_ebitda[tk]) else round(float(net_debt_ebitda[tk]), 2),
            "parts": {"quality": {k: round(float(v[tk]), 4) for k, v in qp.items()},
                      "health": {k: round(float(v[tk]), 4) for k, v in hp.items()},
                      "value": {k: round(float(v[tk]), 4) for k, v in vp.items()},
                      "revisions": round(float(revisions[tk]), 4)},
        }
    return out


LAST_DIST = {}


def _pct_rank(values, v, higher_better=True):
    """values(자기 자신 제외)에 v 를 넣었을 때의 백분위 (pandas rank(pct=True) 평균 순위와 같은 방식)."""
    vals = [x for x in values if x is not None]
    n = len(vals) + 1
    less = sum(1 for x in vals if x < v)
    eq = sum(1 for x in vals if x == v) + 1
    r = less + (eq + 1) / 2
    return r / n if higher_better else (n - r + 1) / n


def rescore(tk, sc, dist, fcf_margin=None, fcf_yield=None):
    """검증된 FCF 마진·수익률(비율, 0.42 = 42%)로 해당 종목의 기업의 질·재무·가격 매력과 총점 재계산.
    sc: quant_scores 의 종목 dict(parts 포함). 반환: 새 dict (원래 값은 score_before 등으로 보존)."""
    if not sc.get("parts") or not dist:
        return sc
    p = {k: dict(v) if isinstance(v, dict) else v for k, v in sc["parts"].items()}
    others = lambda key: [v for t, v in dist.get(key, {}).items() if t != tk]
    if fcf_margin is not None:
        p["quality"]["fcf_margin"] = _pct_rank(others("fcf_margin"), fcf_margin)
        p["health"]["fcf_positive"] = 1.0 if fcf_margin > 0 else 0.0
    if fcf_yield is not None:
        sector = dist.get("sector", {}).get(tk)
        same = [v for t, v in dist.get("fcf_yield", {}).items() if t != tk and dist["sector"].get(t) == sector]
        p["value"]["fcf_yield"] = _pct_rank(same, fcf_yield)
    q = sum(p["quality"].values()) / 6
    h = (p["health"]["net_debt"] + (sc["parts"]["health"]["fcf_positive"] if fcf_margin is None
                                    else _pct_rank([1.0 if (v or 0) > 0 else 0.0 for t, v in dist["fcf_margin"].items()
                                                    if t != tk and v is not None], p["health"]["fcf_positive"]))) / 2
    v = sum(p["value"].values()) / 3
    w = WEIGHTS
    total = (q * w["quality"] + h * w["health"] + v * w["value"] + p["revisions"] * w["revisions"]) * 100
    out = {**sc, "parts": p, "score": round(total, 1), "quality": round(q * 100), "health": round(h * 100),
           "value": round(v * 100)}
    out["score_before"] = sc.get("score_before", sc["score"])
    out["rescored"] = {"fcf_margin": fcf_margin, "fcf_yield": fcf_yield}
    if fcf_margin is not None:
        out["fcf_margin_pct"] = round(fcf_margin * 100, 1)
    if fcf_yield is not None:
        out["fcf_yield_pct"] = round(fcf_yield * 100, 2)
    return out


# ── 2. 후보 선정 ──────────────────────────────────────────────────
def _signals(tk, m1, m3_tickers, m5, scores=None):
    """가산점 신호 [(설명, 점수)]."""
    out = []
    if m1:
        # 자료 지연(다른 대가보다 이전 분기) 13F 는 가산점에서 제외
        gurus = [inv["investor"] for inv in m1["investors"]
                 if not inv.get("stale") and any(r["ticker"] == tk for r in inv["top10"])]
        if gurus:
            out.append(("M1 대가 상위보유: " + ", ".join(gurus), SIGNAL_POINTS["guru_top10"]))
        if m1.get("bought_by", {}).get(tk):
            out.append(("M1 대가 최근분기 매수: " + ", ".join(m1["bought_by"][tk]), SIGNAL_POINTS["guru_buy"]))
    if tk in m3_tickers:
        out.append(("M3 실적 개선 기업", SIGNAL_POINTS["m3"]))
    if m5 and tk in m5.get("buy_tickers", []):
        out.append(("M5 내부자 장내 매수", SIGNAL_POINTS["insider_buy"]))
    if scores and scores.get(tk, {}).get("turnaround"):
        out.append(("흑자전환 임박(다음 분기 EPS 컨센서스 흑자)", SIGNAL_POINTS["turnaround"]))
    return out


def signal_hits(tk, m1, m3_tickers, m5, scores=None):
    """가산점 신호 설명 목록 (분석 자료·리포트 표시용)."""
    return [f"{label} (+{pts}점)" for label, pts in _signals(tk, m1, m3_tickers, m5, scores)]


def guru_sell_note(tk, m1):
    """대가 최근분기 매도 신호 (할인 대상) 또는 None."""
    sellers = (m1 or {}).get("sold_by", {}).get(tk)
    return f"M1 대가 최근분기 매도(-{GURU_SELL_PENALTY}점): " + ", ".join(sellers) if sellers else None


def _ranked(scores, holdings, m1, m3_tickers, m5, cycle_notes):
    held = {h["ticker"] for h in holdings}
    ranked = []
    for tk, sc in scores.items():
        if tk in held or not sc["eligible"]:
            continue
        bonus = sum(pts for _, pts in _signals(tk, m1, m3_tickers, m5, scores))
        penalty = (CYCLE_PENALTY if tk in cycle_notes else 0) + (GURU_SELL_PENALTY if guru_sell_note(tk, m1) else 0)
        ranked.append((sc["score"] + bonus - penalty, tk))
    ranked.sort(reverse=True)
    return [tk for _, tk in ranked]


def is_cyclical(snap):
    industry = snap.get("industry") or ""
    return snap.get("sector") in CYCLICAL_SECTORS or any(w in industry for w in CYCLICAL_INDUSTRY_WORDS)


def cycle_pool(scores, snaps, holdings, m1, m3_tickers, m5):
    """사이클 점검 대상: 정량 상위 CYCLE_POOL 중 경기순환 업종 (연간 마진 추가 수집 대상)."""
    top = _ranked(scores, holdings, m1, m3_tickers, m5, {})[:CYCLE_POOL]
    return [tk for tk in top if is_cyclical(snaps[tk])]


def cycle_note(snap, annual_margins):
    """현재 영업이익률이 최근 연간 평균보다 과도하게 높으면 사이클 정점 할인 사유, 아니면 None."""
    cur = snap.get("op_margin")
    hist = [m for m in annual_margins if m is not None]
    if cur is None or len(hist) < 3:
        return None
    cur, avg = cur * 100, sum(hist) / len(hist)
    if cur >= max(avg * CYCLE_MARGIN_RATIO, avg + CYCLE_MARGIN_GAP):
        return (f"경기순환 업종 사이클 정점 할인(-{CYCLE_PENALTY}점): 현재 영업이익률 {cur:.0f}% vs "
                f"최근 {len(hist)}년 평균 {avg:.0f}%")
    return None


def pick_candidates(scores, snaps, holdings, m1, m3_tickers, m5, cycle_notes=None):
    """보유 종목 전부 + 신규 후보 상위 NEW_CANDIDATES.
    신호 적중 가산점, 사이클 정점 할인, 같은 업종 최대 MAX_PER_INDUSTRY_CANDIDATES 종목."""
    per_industry, picked = {}, []
    for tk in _ranked(scores, holdings, m1, m3_tickers, m5, cycle_notes or {}):
        industry = snaps[tk].get("industry") or tk
        if per_industry.get(industry, 0) >= MAX_PER_INDUSTRY_CANDIDATES:
            continue
        per_industry[industry] = per_industry.get(industry, 0) + 1
        picked.append(tk)
        if len(picked) == NEW_CANDIDATES:
            break
    return [h["ticker"] for h in holdings if h["ticker"] in snaps], picked


# ── 2-1. 분석 재사용 (비용 절감) ─────────────────────────────────
def load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache, today):
    """오래된 항목(REUSE_MAX_DAYS×2 초과) 정리 후 저장."""
    cutoff = (date.fromisoformat(today) - timedelta(days=REUSE_MAX_DAYS * 2)).isoformat()
    cache = {tk: c for tk, c in cache.items() if c["analyzed_date"] >= cutoff}
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)


def cache_entry(analysis, snap, detail, today):
    return {"analyzed_date": today, "price": snap.get("price"), "eps_next_fy": snap.get("eps_next_fy"),
            "next_earnings": detail.get("next_earnings"), "analysis": analysis}


def fresh_analysis_reason(entry, snap, holding, today):
    """재분석이 필요하면 사유 문자열, 재사용 가능하면 None."""
    if not entry:
        return "첫 분석"
    if not (entry.get("analysis") or {}).get("decision_table"):
        return "판단표·데이터 검증 도입 전 분석 — 새 형식으로 재분석"
    if (date.fromisoformat(today) - date.fromisoformat(entry["analyzed_date"])).days >= REUSE_MAX_DAYS:
        return f"지난 분석 {REUSE_MAX_DAYS}일 경과"
    if entry.get("next_earnings") and entry["next_earnings"] <= today:
        return "분석 이후 실적 발표"
    price, old_price = snap.get("price"), entry.get("price")
    if price and old_price and abs(price / old_price - 1) >= REUSE_PRICE_MOVE:
        return f"분석 이후 주가 {(price / old_price - 1) * 100:+.0f}%"
    eps, old_eps = snap.get("eps_next_fy"), entry.get("eps_next_fy")
    if eps and old_eps and abs(eps / old_eps - 1) >= REUSE_EPS_MOVE:
        return f"분석 이후 내년 EPS 추정치 {(eps / old_eps - 1) * 100:+.1f}%"
    if holding and holding.get("thesis_status") == "약화":
        return "보유 논리 약화 — 실행 때마다 재점검"
    return None


# ── 3. 결정 ──────────────────────────────────────────────────────
def expected_return(a, price):
    """약세25·기준50·강세25 가중 12개월 기대수익률(%)."""
    sc = {s.get("name"): s.get("price") for s in ((a or {}).get("decision_table") or {}).get("scenarios", [])}
    if not price or not all(sc.get(k) for k in ("약세", "기준", "강세")):
        return None
    return round((0.25 * sc["약세"] + 0.5 * sc["기준"] + 0.25 * sc["강세"]) / price * 100 - 100, 1)


def starter_ok(a, price):
    """확신도 3 '탐색 편입' 조건: 편입 적합 + Agent 행동 '신규 매수' + 현재가 ≤ 매수 기준가 + 기대수익 ≥ 최소치."""
    dt = (a or {}).get("decision_table") or {}
    e = expected_return(a, price)
    return (a.get("conviction") == 3 and dt.get("action") in ("신규 매수", "추가 매수")
            and dt.get("buy_below") and price and price <= dt["buy_below"]
            and e is not None and e >= MIN_EXPECTED_RETURN)


def decide(holdings, new_candidates, analyses, snaps, scores, today):
    """
    analyses: {ticker: 분석 dict | None}
    반환: (새 보유 목록, 결정 로그, 교체로 빠진 종목)
    """
    decisions, kept, removed = [], [], []
    sector = lambda tk: snaps.get(tk, {}).get("sector", "Unknown")

    for h in holdings:
        a = analyses.get(h["ticker"])
        if a is None:
            kept.append(h)
            decisions.append({"ticker": h["ticker"], "action": "유지",
                              "reason": "이번 주 분석을 완료하지 못해 기존 판단을 유지합니다 (데이터·API 오류)."})
            continue
        status = a.get("thesis_status")
        if a.get("alert_level") == "논리 붕괴":
            status = "붕괴"
        own_status = status not in (None, "해당없음")
        if not own_status:  # 편입 전 후보 시절 분석을 재사용한 경우 기존 상태 유지
            status = h.get("thesis_status") or "유효"
        reason = (own_status and a.get("thesis_status_reason")) or a["verdict_reason"]
        if status == "붕괴" or a["verdict"] == "부적합":
            removed.append(h)
            decisions.append({"ticker": h["ticker"], "action": "제외", "reason": reason})
        else:
            h = {**h, "last_review": today, "thesis_status": status, "conviction": a["conviction"],
                 "alert_level": a.get("alert_level")}
            kept.append(h)
            note = " (논리 약화 — 관찰 강화)" if status == "약화" else ""
            if a.get("alert_level") in ("추가 매수 중단", "비중 재검토"):
                note += f" [경고: {a['alert_level']}]"
            decisions.append({"ticker": h["ticker"], "action": "유지", "reason": reason + note})

    verified = lambda tk: (analyses[tk].get("evidence") or {}).get("data_verified", "검증 완료") == "검증 완료"
    px = lambda tk: (snaps.get(tk) or {}).get("price")
    passes = lambda tk: analyses[tk]["conviction"] >= MIN_CONVICTION_NEW or starter_ok(analyses[tk], px(tk))
    pending_check = [tk for tk in new_candidates
                     if analyses.get(tk) and analyses[tk]["verdict"] == "편입 적합" and passes(tk) and not verified(tk)]
    qualified = [tk for tk in new_candidates
                 if analyses.get(tk) and analyses[tk]["verdict"] == "편입 적합" and passes(tk) and verified(tk)]
    qualified.sort(key=lambda tk: (-analyses[tk]["conviction"], -(expected_return(analyses[tk], px(tk)) or -999),
                                   -scores[tk]["score"]))

    def sector_count(tk):
        return sum(1 for x in kept if sector(x["ticker"]) == sector(tk))

    for tk in qualified:
        a = analyses[tk]
        if sector_count(tk) >= MAX_PER_SECTOR:
            decisions.append({"ticker": tk, "action": "보류", "reason":
                              f"편입 적합 판정이지만 같은 섹터({sector(tk)}) 보유가 이미 {MAX_PER_SECTOR}종목이라 분산을 위해 보류."})
            continue
        if len(kept) >= MAX_HOLDINGS:
            weak = sorted([x for x in kept if x.get("thesis_status") == "약화" and (x.get("conviction") or 5) <= 3],
                          key=lambda x: x.get("conviction") or 5)
            if a["conviction"] >= SWAP_CONVICTION and weak:
                out = weak[0]
                kept.remove(out)
                removed.append(out)
                for dcs in decisions:
                    if dcs["ticker"] == out["ticker"]:
                        dcs["action"], dcs["reason"] = "제외", (
                            f"투자 논리가 약화된 상태에서 확신도가 더 높은 {tk}로 교체. 기존 판단: {dcs['reason']}")
            else:
                decisions.append({"ticker": tk, "action": "보류", "reason":
                                  f"편입 적합(확신도 {a['conviction']})이나 포트폴리오 {MAX_HOLDINGS}자리가 모두 유효한 논리로 채워져 있어 보류."})
                continue
        s = snaps[tk]
        kept.append({
            "ticker": tk, "name": s["name"], "sector": s["sector"],
            "added_date": today, "added_price": s.get("price"),
            "thesis": a["one_line_thesis"], "thesis_breakers": a["bear_case"]["thesis_breakers"],
            "conviction": a["conviction"], "thesis_status": "유효", "last_review": today,
        })
        if a["conviction"] < MIN_CONVICTION_NEW:
            e = expected_return(a, px(tk))
            note = (f"[탐색 편입] 확신도 {a['conviction']}이지만 현재가 ${px(tk):.2f} ≤ 매수 기준가 ${a['decision_table']['buy_below']}, "
                    f"기대수익 {e:+.1f}% → 작은 비중으로 시작하고 다음 검증 후 확대 여부 결정. ")
        else:
            note = ""
        decisions.append({"ticker": tk, "action": "신규편입", "reason": note + a["verdict_reason"]})

    for tk in new_candidates:
        if tk in qualified:
            continue
        a = analyses.get(tk)
        if tk in pending_check:
            decisions.append({"ticker": tk, "action": "보류", "reason":
                              "편입 적합·확신도 기준은 충족했지만 핵심 수치가 '검증 대기' — 공식 자료 확인 후 재판단. "
                              + a["verdict_reason"]})
            continue
        if a is None:
            reason = "분석 실패로 판단 보류."
        elif a["verdict"] == "편입 적합":
            reason = (f"편입 적합이나 확신도 {a['conviction']}로 기준({MIN_CONVICTION_NEW}) 미달이고 탐색 편입 조건"
                      f"(신규 매수 판단 + 매수 기준가 이하 + 기대수익 {MIN_EXPECTED_RETURN}% 이상)도 미충족. {a['verdict_reason']}")
        else:
            reason = f"[{a['verdict']}] {a['verdict_reason']}"
        decisions.append({"ticker": tk, "action": "미편입", "reason": reason})
    return kept, decisions, removed


# ── 4. 이력 ──────────────────────────────────────────────────────
def load_history():
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"portfolio": [], "closed": [], "weeks": []}


def save_history(hist):
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=1)


def update_history(hist, week, today, kept, decisions, removed, snaps, spy_price):
    for h in kept:
        if h.get("added_date") == today and h.get("spy_at_add") is None:
            h["spy_at_add"] = spy_price
    for h in removed:
        price = snaps.get(h["ticker"], {}).get("price")
        reason = next((d["reason"] for d in decisions if d["ticker"] == h["ticker"] and d["action"] == "제외"), "")
        hist["closed"].append({
            "ticker": h["ticker"], "name": h.get("name"), "added_date": h.get("added_date"), "removed_date": today,
            "added_price": h.get("added_price"), "removed_price": price,
            "return_pct": round((price / h["added_price"] - 1) * 100, 1) if price and h.get("added_price") else None,
            "spy_return_pct": round((spy_price / h["spy_at_add"] - 1) * 100, 1) if spy_price and h.get("spy_at_add") else None,
            "reason": reason,
        })
    hist["portfolio"] = kept
    hist["weeks"] = [w for w in hist["weeks"] if w["week"] != week]
    hist["weeks"].append({"week": week, "date": today, "decisions": decisions,
                          "portfolio_after": [h["ticker"] for h in kept]})
    return hist
