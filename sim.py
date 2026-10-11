"""
가상 투자 시뮬레이션 — Agent 추천 비중대로 1억원을 운용했다면 계좌는 지금 어떤 상태인가.

규칙 (data/risk_profile.json 의 비용·이자 가정 사용)
- 시작: 추천 포트폴리오에 첫 종목이 들어간 주간 결정일 (NVDA 2026-09-13 결정)
- 체결: 결정(신호) 다음 거래일 시가. 기록 날짜(신호일)와 체결일을 분리해 거래 내역에 남긴다
- 원금 1억원(원화). 매수 시 체결일 환율 + 환전 스프레드로 달러 환전, 매도 대금은 스프레드 차감 후 원화 현금 복귀
- 비중: 주간 결정의 weights(개별주 + 코어 SPY, 나머지 현금). 기록 없는 과거 주는 보유 종목당 20%
- 정수 주, 매매 수수료 0.25%, 미국 배당 원천징수 후 원화 현금 입금, 원화 현금 연 이자(일할)
- 세금(양도세)은 미반영
비교
- 같은 비중 SPY: Agent 와 같은 주식 비중을 전부 SPY 로 (종목 선택 효과)
- 전액 SPY: 원금 전부 SPY (현금 보유 효과)
- 정량 상위 전략: 같은 개별주 비중을 매주 정량 상위 8개 후보에 균등 배분 + 같은 코어 SPY (AI 분석의 추가 가치)
- 제외 후보 사후 추적: 분석했던 모든 후보의 이후 수익률을 판정·결정별로 집계

simulate()/run() 은 순수 함수(네트워크 없음) → 로컬 테스트 가능. build() 는 yfinance 로 시세 수집 후 실행.
"""
import json
import math
import os
from bisect import bisect_right
from datetime import date, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INITIAL_KRW = 100_000_000
FEE = 0.0025
SLOT_PCT = 20            # 비중 기록이 없을 때 종목당 기본 비중 (5종목 × 20%)
STEP = 5
FX_TICKER = "KRW=X"      # Yahoo: 1달러당 원화
BENCH = "SPY"
DEFAULT_OPTS = {"cash_rate_pct": 2.5, "fx_spread_pct": 0.1, "dividend_tax_pct": 15}


class Series:
    """{ticker: [(YYYY-MM-DD, close, open, dividend), ...]} (open·dividend 생략 가능)."""

    def __init__(self, data):
        self.d = {}
        for tk, rows in data.items():
            clean = []
            for r in rows:
                r = list(r) + [None, 0.0]
                c = r[1]
                if c is None or c != c:
                    continue
                o = r[2] if r[2] is not None and r[2] == r[2] else c
                clean.append((str(r[0]), float(c), float(o), float(r[3] or 0)))
            clean.sort()
            self.d[tk] = ([x[0] for x in clean], [x[1] for x in clean], [x[2] for x in clean], [x[3] for x in clean])

    def _idx(self, tk, day):
        return bisect_right(self.d.get(tk, ([],))[0], day)

    def at(self, tk, day):
        """day 이하 마지막 종가."""
        i = self._idx(tk, day)
        return self.d[tk][1][i - 1] if i else None

    def open_on(self, tk, day):
        dates = self.d.get(tk, ([],))[0]
        i = bisect_right(dates, day)
        return self.d[tk][2][i - 1] if i and dates[i - 1] == day else None

    def div_on(self, tk, day):
        dates = self.d.get(tk, ([],))[0]
        i = bisect_right(dates, day)
        return self.d[tk][3][i - 1] if i and dates[i - 1] == day else 0.0

    def dates(self, tk):
        return self.d.get(tk, ([],))[0]


def normalize_weights(weights):
    """5% 단위 정수, 합계 100 이하 (넘으면 큰 비중부터 5%씩 축소)."""
    w = {}
    for tk, v in (weights or {}).items():
        try:
            v = int(round(float(v) / STEP) * STEP)
        except (TypeError, ValueError):
            continue
        if v > 0:
            w[tk] = min(v, 100)
    while sum(w.values()) > 100:
        big = max(w, key=lambda k: w[k])
        w[big] -= STEP
        if w[big] <= 0:
            w.pop(big)
    return w


def week_weights(week, prev):
    """주간 결정의 비중. weights 기록이 있으면 그대로(코어 SPY 포함), 없으면 보유 종목당 기본 20%."""
    if week.get("weights"):
        return normalize_weights(week["weights"])
    tickers = list(dict.fromkeys(week.get("portfolio_after", [])))
    return normalize_weights({tk: prev.get(tk, SLOT_PCT) for tk in tickers})


def start_date(hist):
    weeks = sorted(hist.get("weeks", []), key=lambda w: w["date"])
    first = next((w for w in weeks if w.get("portfolio_after")), None)
    return first["date"] if first else None


def agent_events(hist):
    s0 = start_date(hist)
    events, prev = [], None
    for w in sorted(hist.get("weeks", []), key=lambda w: (w["date"], w.get("interim", False))):
        if w["date"] < s0:
            continue
        cur = week_weights(w, prev or {})
        if prev is None or cur != prev:
            events.append((w["date"], cur, w.get("week")))
        prev = cur
    return events


def run(events, series, names=None, initial=INITIAL_KRW, opts=None, today=None, s0=None):
    """events: [(신호일, {ticker: %}, 라벨)] → 신호 다음 거래일 시가 체결로 일별 평가."""
    opts = {**DEFAULT_OPTS, **(opts or {})}
    names = names or {}
    spread, tax, rate = opts["fx_spread_pct"] / 100, opts["dividend_tax_pct"] / 100, opts["cash_rate_pct"] / 100
    s0 = s0 or (events[0][0] if events else None)
    if not s0:
        return None
    fx0 = series.at(FX_TICKER, s0)
    if not fx0:
        return None
    st = {"cash": float(initial), "pos": {}}
    tot = {"fee": 0.0, "fx": 0.0, "div": 0.0, "interest": 0.0}
    trades, wlog = [], []

    def fx_at(day):
        return series.at(FX_TICKER, day) or fx0

    def trade(sig, day, tk, side, n, px, why, target):
        if n <= 0:
            return
        fx = fx_at(day)
        usd = n * px
        krw = usd * fx
        p = st["pos"].setdefault(tk, {"shares": 0, "cost_usd": 0.0, "cost_krw": 0.0})
        fee, fxc = krw * FEE, krw * spread
        tot["fee"] += fee
        tot["fx"] += fxc
        if side == "buy":
            st["cash"] -= krw + fee + fxc
            p["shares"] += n
            p["cost_usd"] += usd
            p["cost_krw"] += krw + fee + fxc
        else:
            ratio = n / p["shares"]
            st["cash"] += krw - fee - fxc
            p["cost_usd"] -= p["cost_usd"] * ratio
            p["cost_krw"] -= p["cost_krw"] * ratio
            p["shares"] -= n
            if p["shares"] == 0:
                st["pos"].pop(tk)
        trades.append({"signal_date": sig, "date": day, "ticker": tk, "side": "매수" if side == "buy" else "매도",
                       "shares": n, "price_usd": round(px, 2), "fx": round(fx, 2), "amount_krw": round(krw),
                       "cost_krw": round(fee + fxc), "target_pct": target, "reason": why})

    def rebalance(sig, day, weights):
        fx = fx_at(day)
        px = lambda tk: series.open_on(tk, day) or series.at(tk, day)
        for tk in list(st["pos"]):
            if tk not in weights and px(tk):
                trade(sig, day, tk, "sell", st["pos"][tk]["shares"], px(tk), "제외·비중 0", 0)
        live = {tk: w for tk, w in weights.items() if px(tk)}
        total = st["cash"] + sum(p["shares"] * (px(tk) or 0) * fx for tk, p in st["pos"].items())
        for tk, w in live.items():
            if tk in st["pos"]:
                excess = st["pos"][tk]["shares"] * px(tk) * fx - total * w / 100
                n = math.floor(excess / (px(tk) * fx))
                if n > 0:
                    trade(sig, day, tk, "sell", n, px(tk), f"비중 축소 → {w}%", w)
        for tk, w in live.items():
            held = tk in st["pos"]
            have = st["pos"][tk]["shares"] * px(tk) * fx if held else 0
            budget = min(total * w / 100 - have, st["cash"])
            n = math.floor(budget / (px(tk) * fx * (1 + FEE + spread)))
            if n > 0:
                trade(sig, day, tk, "buy", n, px(tk), (f"비중 확대 → {w}%" if held else f"신규 {w}%"), w)
        wlog.append({"date": sig, "fill_date": day, "weights": dict(weights), "cash_pct": 100 - sum(weights.values())})

    days = [d for d in series.dates(BENCH) if d > s0 and (today is None or d <= today)]
    pending = sorted(events)
    curve = {"dates": [s0], "value": [round(initial)]}
    last_day = s0
    for d in days:
        gap = (date.fromisoformat(d) - date.fromisoformat(last_day)).days
        if st["cash"] > 0 and gap > 0:
            it = st["cash"] * rate * gap / 365
            st["cash"] += it
            tot["interest"] += it
        last_day = d
        while pending and pending[0][0] < d:          # 신호일 다음 거래일 시가 체결
            sig, wts, _ = pending.pop(0)
            rebalance(sig, d, wts)
        fx = fx_at(d)
        for tk, p in list(st["pos"].items()):
            dv = series.div_on(tk, d)
            if dv:
                amt = p["shares"] * dv * (1 - tax) * fx * (1 - spread)
                st["cash"] += amt
                tot["div"] += amt
        val = st["cash"] + sum(p["shares"] * (series.at(tk, d) or 0) * fx for tk, p in st["pos"].items())
        curve["dates"].append(d)
        curve["value"].append(round(val))

    as_of = days[-1] if days else s0
    fx_now = fx_at(as_of)
    stock_now = sum(p["shares"] * (series.at(tk, as_of) or 0) * fx_now for tk, p in st["pos"].items())
    total_now = st["cash"] + stock_now
    peak, mdd = 0, 0.0
    for v in curve["value"]:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    cur_w = wlog[-1]["weights"] if wlog else {}
    holdings = []
    for tk, p in st["pos"].items():
        px = series.at(tk, as_of) or 0
        val = p["shares"] * px * fx_now
        holdings.append({"ticker": tk, "name": names.get(tk, tk), "shares": p["shares"],
                         "avg_cost_usd": round(p["cost_usd"] / p["shares"], 2), "price_usd": round(px, 2),
                         "cost_krw": round(p["cost_krw"]), "value_krw": round(val),
                         "weight_pct": round(val / total_now * 100, 1), "target_pct": cur_w.get(tk),
                         "return_pct": round((val / p["cost_krw"] - 1) * 100, 1) if p["cost_krw"] else None})
    holdings.sort(key=lambda h: -h["value_krw"])
    return {"as_of": as_of, "fx_start": round(fx0, 2), "fx_now": round(fx_now, 2), "value_krw": round(total_now),
            "pnl_krw": round(total_now - initial), "return_pct": round((total_now / initial - 1) * 100, 2),
            "stock_krw": round(stock_now), "cash_krw": round(st["cash"]),
            "invested_pct": round(stock_now / total_now * 100, 1) if total_now else 0,
            "target_invested_pct": sum(cur_w.values()), "max_drawdown_pct": round(mdd * 100, 2),
            "costs": {k: round(v) for k, v in tot.items()}, "holdings": holdings, "trades": trades,
            "weights_history": wlog, "curve": curve,
            # 아직 다음 거래일이 오지 않아 체결 전인 신호 (주말 결정 등)
            "pending": [{"signal_date": s, "weights": w, "label": l} for s, w, l in pending]}


def shadow(reports, series, as_of):
    """분석했던 모든 후보의 이후 성과 (분석일 가격 → 현재) vs 같은 기간 SPY."""
    rows = []
    for wk, rep in sorted(reports.items()):
        d0 = rep.get("date")
        spy0 = series.at(BENCH, d0) or rep.get("spy_price")
        spy1 = series.at(BENCH, as_of)
        dec = {x["ticker"]: x["action"] for x in rep.get("decisions", [])}
        for tk, an in (rep.get("analyses") or {}).items():
            p0 = (an.get("metrics") or {}).get("price") or series.at(tk, d0)
            p1 = series.at(tk, as_of)
            if not p0 or not p1 or not spy0 or not spy1:
                continue
            a = an.get("analysis") or {}
            r, b = (p1 / p0 - 1) * 100, (spy1 / spy0 - 1) * 100
            rows.append({"week": wk, "date": d0, "ticker": tk, "verdict": a.get("verdict"), "conviction": a.get("conviction"),
                         "action": dec.get(tk), "return_pct": round(r, 1), "spy_pct": round(b, 1), "excess_pct": round(r - b, 1)})
    groups = {}
    label = lambda r: ("편입·유지" if r["action"] in ("신규편입", "유지") else
                       "편입 적합이나 제외(확신도 미달·보류)" if r["verdict"] == "편입 적합" else
                       "관찰" if r["verdict"] == "관찰" else "부적합" if r["verdict"] == "부적합" else "기타")
    for r in rows:
        groups.setdefault(label(r), []).append(r["excess_pct"])
    summary = [{"group": g, "n": len(v), "avg_excess_pct": round(sum(v) / len(v), 2)} for g, v in groups.items()]
    return {"rows": sorted(rows, key=lambda r: (r["date"], r["ticker"]), reverse=True)[:40], "summary": summary,
            "note": "분석 당시 가격 대비 현재 수익률 − 같은 기간 SPY. 표본이 적을 때는 우연의 영향이 큼."}


def simulate(hist, series, reports=None, opts=None, today=None):
    reports = reports or {}
    s0 = start_date(hist)
    if not s0:
        return None
    names = {h["ticker"]: h.get("name") for h in hist.get("portfolio", []) + hist.get("closed", []) if h.get("name")}
    names[BENCH] = "SPDR S&P 500"
    ev = agent_events(hist)
    main = run(ev, series, names, INITIAL_KRW, opts, today, s0)
    if not main:
        return None
    same = run([(s, {BENCH: sum(w.values())}, l) for s, w, l in ev], series, names, INITIAL_KRW, opts, today, s0)
    full = run([(s0, {BENCH: 100}, "전액 SPY")], series, names, INITIAL_KRW, opts, today, s0)
    alloc = next((w.get("allocation") for w in sorted(hist.get("weeks", []), key=lambda w: w["date"], reverse=True)
                  if w.get("allocation")), None)
    o = {**DEFAULT_OPTS, **(opts or {})}
    return {
        **main, "start_date": s0, "initial_krw": INITIAL_KRW, "fee_pct": FEE * 100, "slot_pct": SLOT_PCT,
        "bench_value_krw": same["value_krw"], "bench_return_pct": same["return_pct"],
        "spy_full_value_krw": full["value_krw"], "spy_full_return_pct": full["return_pct"],
        "stock_return_pct": (lambda hs: round((sum(h["value_krw"] for h in hs) / sum(h["cost_krw"] for h in hs) - 1) * 100, 2)
                             if hs and sum(h["cost_krw"] for h in hs) else None)(
                                 [h for h in main["holdings"] if h["ticker"] != BENCH]),
        "curve": {**main["curve"], "bench": same["curve"]["value"], "spy_full": full["curve"]["value"]},
        "shadow": shadow(reports, series, main["as_of"]),
        "allocation": alloc,
        "assumptions": {"fill": "신호(결정) 다음 거래일 시가 체결", "fee_pct": FEE * 100, "fx_spread_pct": o["fx_spread_pct"],
                        "dividend_tax_pct": o["dividend_tax_pct"], "cash_rate_pct": o["cash_rate_pct"],
                        "not_included": "양도소득세"},
        "rule": "원금 1억원(원화). 최대 5종목 개별주 + 원화 현금만 운용(ETF 없음). 종목 비중은 확신도·기대수익률(약세25·기준50·강세25)·"
                "약세 시나리오 손실 한도로 정하고, 주식 합계는 시장 스탠스별 상한 안에서 기대수익이 낮은 종목부터 줄임. 체결은 신호 다음 거래일 시가·당일 환율, 정수 주, 수수료·환전 스프레드·배당 원천징수·"
                "현금 이자 반영, 양도세 미반영.",
    }


def _history(tk, start):
    import yfinance as yf
    df = yf.Ticker(tk).history(start=start, auto_adjust=False, actions=True)
    if df is None or df.empty:
        return []
    div = df["Dividends"] if "Dividends" in df else None
    return [(idx.strftime("%Y-%m-%d"), float(row["Close"]), float(row["Open"]),
             float(div[idx]) if div is not None else 0.0) for idx, row in df.iterrows()]


def _load_reports():
    out = {}
    d = os.path.join(BASE_DIR, "data", "reports")
    if os.path.isdir(d):
        for fn in os.listdir(d):
            if fn.endswith(".json"):
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    out[fn[:-5]] = json.load(f)
    return out


def build(hist):
    """yfinance 로 일별 시세·배당·환율 수집 → 시뮬레이션. 실패 시 None (대시보드는 대기 카드 표시)."""
    s0 = start_date(hist)
    if not s0:
        return None
    reports = _load_reports()
    prof_path = os.path.join(BASE_DIR, "data", "risk_profile.json")
    opts = {}
    if os.path.exists(prof_path):
        with open(prof_path, encoding="utf-8") as f:
            p = json.load(f)
        opts = {k: p[k] for k in DEFAULT_OPTS if k in p}
    tks = {BENCH, FX_TICKER}
    for w in hist.get("weeks", []):
        tks.update(w.get("portfolio_after", []))
        tks.update((w.get("weights") or {}).keys())
    for rep in reports.values():
        tks.update(c["ticker"] for c in rep.get("candidates", []))
        tks.update((rep.get("analyses") or {}).keys())
    first = min([s0] + [r.get("date") for r in reports.values() if r.get("date")])
    since = (date.fromisoformat(first) - timedelta(days=10)).isoformat()
    try:
        data = {}
        for tk in sorted(tks):
            try:
                data[tk] = _history(tk, since)
            except Exception as e:
                print(f"  시세 실패 {tk}: {e}")
        res = simulate(hist, Series(data), reports, opts)
        if res:
            print(f"가상 투자: {res['value_krw']:,}원 ({res['return_pct']:+.2f}%) · 같은 비중 SPY {res['bench_return_pct']:+.2f}% "
                  f"· 전액 SPY {res['spy_full_return_pct']:+.2f}%")
        return res
    except Exception as e:  # 시세 수집 실패가 대시보드 생성을 막지 않게
        print(f"가상 투자 시뮬레이션 실패: {e}")
        return None
