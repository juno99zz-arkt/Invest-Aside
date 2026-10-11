"""
매일 17:00 KST GitHub Actions에서 실행 (주간 파이프라인 직후에도 실행).
1. data/weekly_signals.json (주간 신호·뉴스) + data/picks_history.json + 최신 리포트 로드
2. yfinance로 매일 갱신:
   - M6 PEG (30종목)
   - M7 미국 / M8 한국 시총 상위 50 × 5지표 + 신호등
   - M4 흑자전환 현재가 → 추정 PER
   - 추천 포트폴리오 현재가 → 편입 후 수익률 (vs SPY)
3. 빌드 메타(KST 타임스탬프) 주입
4. index.html INJECTED 교체 → gh-pages 배포
"""
import json
import os
import sys
from datetime import date, datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
sys.path.insert(0, BASE_DIR)

from data_fetcher import PEG_TICKERS, fetch_top50_metrics, fetch_kr_top50_metrics, fetch_prices
import picks
import sec_data

KST = timezone(timedelta(hours=9))
STANCE_LEVEL = {"공격적 매수": 1, "선별적 매수": 2, "중립": 3, "방어적": 4}


def _load(path, default=None):
    full = os.path.join(DATA_DIR, path)
    if not os.path.exists(full):
        return default
    with open(full, encoding="utf-8") as f:
        return json.load(f)


# ── PEG 실시간 업데이트 (티커 정의는 data_fetcher.PEG_TICKERS 단일 소스) ────
def fetch_peg():
    try:
        import yfinance as yf
        result = []
        for ticker, name in PEG_TICKERS:
            try:
                info = yf.Ticker(ticker).info
                peg = info.get("trailingPegRatio") or info.get("pegRatio")
                pe  = info.get("trailingPE") or info.get("forwardPE")
                if peg and pe:
                    result.append({
                        "ticker": ticker, "name": name,
                        "peg": round(float(peg), 1),
                        "pe":  round(float(pe),  1),
                        "growth_rate": round(float(pe) / float(peg), 1),
                    })
            except Exception:
                pass
        result.sort(key=lambda x: x["peg"], reverse=True)
        return result or None
    except ImportError:
        return None

# ── M4 추정 PER 계산 ────────────────────────────────────────────
def _signal_pe(pe):
    if pe is None: return "gray"
    if pe < 15: return "green"
    if pe <= 25: return "yellow"
    return "red"

def update_m4_projected_pe(m4):
    """yfinance 현재가 → projected_pe = 현재가 ÷ 향후 12개월 추정 EPS (이미 연간 값)."""
    companies = m4.get("companies", [])
    prices = fetch_prices([c["ticker"] for c in companies])
    for c in companies:
        price = prices.get(c["ticker"])
        if price and c.get("consensus_eps", 0) > 0:
            c["current_price"] = round(price, 2)
            c["projected_pe"]  = round(price / c["consensus_eps"], 1)
        c["sig_proj_pe"] = _signal_pe(c.get("projected_pe"))
    print(f"M4 현재가 {len(prices)}개 갱신 + 추정 PER 계산 완료")


# ── 종합(추천) 탭 ────────────────────────────────────────────────
def build_summary(weekly, hist, report):
    today = datetime.now(KST).date()
    brief = (weekly or {}).get("market_brief") or {}
    sectors = ((weekly or {}).get("m2") or {}).get("sectors", [])

    portfolio = hist.get("portfolio", [])
    prices = fetch_prices([h["ticker"] for h in portfolio] + ["SPY"]) if portfolio else {}
    spy = prices.get("SPY")
    analyses = (report or {}).get("analyses", {})
    holdings = []
    for h in portfolio:
        price = prices.get(h["ticker"])
        a = (analyses.get(h["ticker"]) or {}).get("analysis") or {}
        holdings.append({
            **h,
            "current_price": price,
            "return_pct": round((price / h["added_price"] - 1) * 100, 1) if price and h.get("added_price") else None,
            "spy_return_pct": round((spy / h["spy_at_add"] - 1) * 100, 1) if spy and h.get("spy_at_add") else None,
            "weeks_held": (today - date.fromisoformat(h["added_date"])).days // 7,
            "scores": {k: (a.get(k) or {}).get("score") for k in ("quality", "financial_health", "valuation")},
        })

    last_week = hist["weeks"][-1] if hist.get("weeks") else None
    stance = brief.get("stance")
    added = [d["ticker"] for d in (last_week or {}).get("decisions", []) if d["action"] == "신규편입"]
    lines = [f"📊 [{today.isoformat()}] 주간 장기투자 브리핑", ""]
    if stance:
        lines += [f"🎯 스탠스: {stance}", ""]
    if portfolio:
        lines.append("📌 추천 포트폴리오")
        lines += [f"· {h['ticker']} — {h.get('thesis', '')}" for h in portfolio]
        lines.append("")
    lines.append("🆕 이번 주 신규 편입: " + (", ".join(added) if added else "없음 (기준 충족 종목 없음)"))
    if brief.get("next_events"):
        lines += ["", "📅 다음 주 일정"] + [f"· {e['date']} {e['event']}" for e in brief["next_events"][:3]]

    return {
        "date": today.isoformat(),
        "week": (weekly or {}).get("week"),
        "weekly_built_at": (weekly or {}).get("built_at_kst"),
        "status": (weekly or {}).get("status", {}),
        "stance": stance, "stance_level": STANCE_LEVEL.get(stance), "stance_reason": brief.get("stance_reason"),
        "weekly_message": {"market_env": brief.get("market_env"), "key_change": brief.get("key_change"),
                           "next_events": brief.get("next_events", [])},
        "top_sectors": [
            {"rank": i + 1, "name": s["sector_kr"],
             "reason": f"다음 회계연도 EPS 추정치 30일 {s['eps_revision_pct_30d']:+.2f}%"
                       + (f" · 상향 비율 {round(s['upgrade_ratio'] * 100)}%" if s.get("upgrade_ratio") is not None else "")}
            for i, s in enumerate(sectors[:3])
        ],
        "portfolio": holdings,
        "last_week": last_week,
        "weeks": hist.get("weeks", [])[-12:],
        "closed": hist.get("closed", []),
        "brief_text": "\n".join(lines),
        # 화면 '?' 로직 설명용 — 실제 코드 설정값을 그대로 전달
        "logic": {
            "weights": {k: round(v * 100) for k, v in picks.WEIGHTS.items()},
            "new_candidates": picks.NEW_CANDIDATES, "industry_cap": picks.MAX_PER_INDUSTRY_CANDIDATES,
            "signal_points": picks.SIGNAL_POINTS, "guru_sell_penalty": picks.GURU_SELL_PENALTY,
            "guru_min_weight_pct": sec_data.GURU_MIN_WEIGHT * 100, "guru_change_pct": sec_data.GURU_CHANGE,
            "cycle_pool": picks.CYCLE_POOL, "cycle_penalty": picks.CYCLE_PENALTY,
            "cycle_ratio": picks.CYCLE_MARGIN_RATIO, "cycle_gap": picks.CYCLE_MARGIN_GAP,
            "max_holdings": picks.MAX_HOLDINGS, "sector_cap": picks.MAX_PER_SECTOR,
            "min_conviction": picks.MIN_CONVICTION_NEW, "swap_conviction": picks.SWAP_CONVICTION,
            "reuse_days": picks.REUSE_MAX_DAYS, "reuse_price_pct": round(picks.REUSE_PRICE_MOVE * 100),
            "reuse_eps_pct": round(picks.REUSE_EPS_MOVE * 100),
        },
    }


# ── 데이터 조합 ─────────────────────────────────────────────────
def build_data():
    weekly = _load("weekly_signals.json")
    hist = _load("picks_history.json", {"portfolio": [], "closed": [], "weeks": []})
    report = _load(f"reports/{hist['weeks'][-1]['week']}.json") if hist.get("weeks") else None
    if weekly is None:
        print("경고: data/weekly_signals.json 없음 — 주간 섹션은 빈 상태로 표시")
        weekly = {}

    m6 = weekly.get("m6") or {}
    peg = fetch_peg()
    if peg:
        m6["peg_ratios"] = peg
        print(f"PEG 실시간 데이터 {len(peg)}개 반영")
    else:
        print("yfinance 실패 — PEG 비움")

    top50 = fetch_top50_metrics()
    print(f"M7 미국 상위 50종목 {len(top50)}개 갱신" if top50 else "yfinance 실패 — M7 비움")
    kr_top50 = fetch_kr_top50_metrics()
    print(f"M8 한국 상위 50종목 {len(kr_top50)}개 갱신" if kr_top50 else "yfinance 실패 — M8 비움")

    m4 = weekly.get("m4") or {"companies": []}
    update_m4_projected_pe(m4)

    now_kst = datetime.now(KST)
    build_meta = {
        "built_at_kst":   now_kst.strftime("%Y-%m-%d %H:%M"),
        "built_at_iso":   now_kst.isoformat(),
        "schedule_label": "매일 KST 17:00 시세 갱신 · 매주 토 신호·추천 갱신 (Aside AI 분석)",
        "weekly_built_at": weekly.get("built_at_kst"),
    }

    return {
        "summary": build_summary(weekly, hist, report),
        "report": report,
        "news": (weekly.get("market_brief") or {}).get("news", []),
        "m1": weekly.get("m1"),
        "m2": weekly.get("m2"),
        "m3": weekly.get("m3"),
        "m4": m4,
        "m5": weekly.get("m5"),
        "m6": m6,
        "m7": {"stocks": top50 or []},
        "m8": {"stocks": kr_top50 or []},
        "meta": build_meta,
    }

def inject_data(html, data_json):
    lines = html.split('\n')
    start_idx = None
    end_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if start_idx is None and stripped.startswith('const INJECTED ='):
            start_idx = i
            if stripped.endswith(';'):
                end_idx = i
                break
        elif start_idx is not None and stripped == '};':
            end_idx = i
            break
    if start_idx is not None and end_idx is not None:
        html = '\n'.join(lines[:start_idx] + ['const INJECTED = ' + data_json + ';'] + lines[end_idx+1:])
        print(f"INJECTED 교체 성공 (line {start_idx}~{end_idx})")
    else:
        print(f"경고: INJECTED 블록 찾기 실패 (start={start_idx}, end={end_idx})")
    return html

# ── HTML 생성 ───────────────────────────────────────────────────
def generate():
    data = build_data()
    # </script> 조기 종료 방지 (외부 텍스트가 포함되므로)
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    html_path = os.path.join(BASE_DIR, "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    html = inject_data(html, data_json)

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"생성 완료: {html_path}")

if __name__ == "__main__":
    generate()
