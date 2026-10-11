"""
가상 투자 시뮬레이션 — Agent 추천대로 1억원을 투자했다면 계좌는 지금 어떤 상태인가.

규칙
- 시작: 추천 포트폴리오에 첫 종목이 들어간 주간 결정일 (NVDA 2026-09-13)
- 원금 1억원을 시작일 환율(USD/KRW)로 전액 달러 환전해 운용 (이후 평가는 그날 환율로 원화 환산)
- 매주 결정의 편입 종목에 균등 배분, 전액 투자. 편입·제외로 종목 구성이 바뀔 때만 리밸런싱
- 체결가: 결정 당시 기준가(편입가·제외가, 없으면 결정일 직전 종가), 정수 주, 매매 수수료 0.25%
- 세금·환전 스프레드 미반영
- 벤치마크: 같은 날 같은 1억원으로 SPY 매수

simulate() 는 순수 함수(네트워크 없음) → 로컬 테스트 가능.
build() 는 yfinance 로 일별 종가·환율을 받아 simulate() 호출.
"""
import math
from bisect import bisect_right
from datetime import date, timedelta

INITIAL_KRW = 100_000_000
FEE = 0.0025
FX_TICKER = "KRW=X"      # Yahoo: 1달러당 원화
BENCH = "SPY"


class Series:
    """{ticker: [(YYYY-MM-DD, close), ...]} → 특정일 이전 마지막 종가 조회."""

    def __init__(self, data):
        self.d = {}
        for tk, rows in data.items():
            rows = sorted((str(a), float(b)) for a, b in rows if b is not None and b == b)
            self.d[tk] = ([a for a, _ in rows], [b for _, b in rows])

    def at(self, tk, day):
        dates, vals = self.d.get(tk, ([], []))
        i = bisect_right(dates, day)
        return vals[i - 1] if i else None

    def dates(self, tk):
        return self.d.get(tk, ([], []))[0]


def _known_prices(hist):
    buy, sell, names = {}, {}, {}
    for h in hist.get("portfolio", []) + hist.get("closed", []):
        if h.get("added_date") and h.get("added_price"):
            buy[(h["ticker"], h["added_date"])] = float(h["added_price"])
        if h.get("removed_date") and h.get("removed_price"):
            sell[(h["ticker"], h["removed_date"])] = float(h["removed_price"])
        if h.get("name"):
            names[h["ticker"]] = h["name"]
    return buy, sell, names


def start_date(hist):
    weeks = sorted(hist.get("weeks", []), key=lambda w: w["date"])
    first = next((w for w in weeks if w.get("portfolio_after")), None)
    return first["date"] if first else None


def tickers_ever(hist):
    out = []
    for w in hist.get("weeks", []):
        for tk in w.get("portfolio_after", []):
            if tk not in out:
                out.append(tk)
    return out


def simulate(hist, series, today=None):
    s0 = start_date(hist)
    if not s0:
        return None
    buy_px, sell_px, names = _known_prices(hist)
    weeks = [w for w in sorted(hist["weeks"], key=lambda w: w["date"]) if w["date"] >= s0]
    reasons = {}
    for w in weeks:
        for d in w.get("decisions", []):
            reasons[(d["ticker"], w["date"])] = d.get("action")

    fx0 = series.at(FX_TICKER, s0)
    if not fx0:
        return None
    state = {"cash": INITIAL_KRW / fx0, "pos": {}}  # pos: tk -> {"shares", "cost"}
    trades = []

    def price(tk, day, side):
        known = (buy_px if side == "buy" else sell_px).get((tk, day))
        return known or series.at(tk, day)

    def trade(day, tk, side, shares, px, why):
        if shares <= 0:
            return
        amt = shares * px
        fee = amt * FEE
        p = state["pos"].setdefault(tk, {"shares": 0, "cost": 0.0})
        if side == "buy":
            state["cash"] -= amt + fee
            p["shares"] += shares
            p["cost"] += amt
        else:
            avg = p["cost"] / p["shares"] if p["shares"] else 0
            state["cash"] += amt - fee
            p["cost"] -= avg * shares
            p["shares"] -= shares
            if p["shares"] == 0:
                state["pos"].pop(tk)
        fx = series.at(FX_TICKER, day) or fx0
        trades.append({"date": day, "ticker": tk, "side": "매수" if side == "buy" else "매도", "shares": shares,
                       "price_usd": round(px, 2), "amount_krw": round(amt * fx), "fee_krw": round(fee * fx),
                       "reason": why})

    def rebalance(day, target):
        held = list(state["pos"])
        for tk in held:                                   # 1) 제외 종목 전량 매도
            if tk not in target:
                px = price(tk, day, "sell")
                if px:
                    trade(day, tk, "sell", state["pos"][tk]["shares"], px, "제외")
        live = [tk for tk in target if price(tk, day, "buy")]
        if not live:
            return
        # 기존 보유는 결정일 직전 종가, 신규는 편입가로 체결
        pxs = {tk: (series.at(tk, day) if tk in state["pos"] else None) or price(tk, day, "buy") for tk in live}
        total = state["cash"] + sum(state["pos"][tk]["shares"] * pxs[tk] for tk in live if tk in state["pos"])
        per = total / len(live)
        for tk in live:                                   # 2) 비중 초과분 매도
            if tk in state["pos"]:
                excess = state["pos"][tk]["shares"] * pxs[tk] - per
                n = math.floor(excess / pxs[tk])
                if n > 0:
                    trade(day, tk, "sell", n, pxs[tk], "리밸런싱")
        for tk in live:                                   # 3) 부족분 매수 (수수료 포함 정수 주)
            have = state["pos"].get(tk, {}).get("shares", 0) * pxs[tk]
            budget = min(per - have, state["cash"])
            n = math.floor(budget / (pxs[tk] * (1 + FEE)))
            if n > 0:
                why = "리밸런싱" if tk in state["pos"] else "신규편입"
                trade(day, tk, "buy", n, pxs[tk], why)

    pending = []
    prev = None
    for w in weeks:
        tgt = list(dict.fromkeys(w.get("portfolio_after", [])))
        if prev is None or set(tgt) != set(prev):
            pending.append((w["date"], tgt))
        prev = tgt

    # 일별 평가 곡선
    days = [d for d in series.dates(BENCH) if d > s0 and (today is None or d <= today)]
    spy0 = series.at(BENCH, s0)
    spy_shares = (INITIAL_KRW / fx0) * (1 - FEE) / spy0 if spy0 else 0

    def value(day):
        fx = series.at(FX_TICKER, day) or fx0
        usd = state["cash"] + sum(p["shares"] * (series.at(tk, day) or 0) for tk, p in state["pos"].items())
        return usd, usd * fx, spy_shares * (series.at(BENCH, day) or spy0) * fx

    curve = {"dates": [s0], "value": [INITIAL_KRW], "spy": [INITIAL_KRW]}
    while pending and pending[0][0] <= s0:
        rebalance(*pending.pop(0))
    for d in days:
        while pending and pending[0][0] <= d:
            rebalance(*pending.pop(0))
        _, v, sv = value(d)
        curve["dates"].append(d)
        curve["value"].append(round(v))
        curve["spy"].append(round(sv))
    while pending:                                        # 마지막 결정 이후 아직 거래일이 없는 경우
        rebalance(*pending.pop(0))

    as_of = days[-1] if days else s0
    usd_now, krw_now, spy_now = value(as_of)
    fx_now = series.at(FX_TICKER, as_of) or fx0
    peak, mdd = 0, 0.0
    for v in curve["value"]:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)

    holdings = []
    for tk, p in state["pos"].items():
        px = series.at(tk, as_of) or 0
        val = p["shares"] * px * fx_now
        holdings.append({
            "ticker": tk, "name": names.get(tk, tk), "shares": p["shares"],
            "avg_cost_usd": round(p["cost"] / p["shares"], 2), "price_usd": round(px, 2),
            "value_krw": round(val), "weight_pct": round(val / krw_now * 100, 1) if krw_now else None,
            "return_pct": round((px / (p["cost"] / p["shares"]) - 1) * 100, 1) if p["cost"] else None,
        })
    holdings.sort(key=lambda h: -h["value_krw"])

    return {
        "start_date": s0, "as_of": as_of, "initial_krw": INITIAL_KRW, "fee_pct": FEE * 100,
        "fx_start": round(fx0, 2), "fx_now": round(fx_now, 2),
        "value_krw": round(krw_now), "pnl_krw": round(krw_now - INITIAL_KRW),
        "return_pct": round((krw_now / INITIAL_KRW - 1) * 100, 2),
        "usd_return_pct": round((usd_now / (INITIAL_KRW / fx0) - 1) * 100, 2),
        "spy_value_krw": round(spy_now), "spy_return_pct": round((spy_now / INITIAL_KRW - 1) * 100, 2),
        "max_drawdown_pct": round(mdd * 100, 2),
        "cash_usd": round(state["cash"], 2), "cash_krw": round(state["cash"] * fx_now),
        "holdings": holdings, "trades": trades, "curve": curve,
        "rule": "매주 결정의 편입 종목에 균등 배분·전액 투자, 종목 구성이 바뀔 때만 리밸런싱. 체결은 결정 당시 기준가(편입가·제외가), "
                "정수 주, 수수료 0.25%. 원금은 시작일 환율로 달러 환전, 평가는 당일 환율. 세금·환전 스프레드 미반영.",
    }


def _history(tk, start):
    import yfinance as yf
    df = yf.Ticker(tk).history(start=start, auto_adjust=False)
    if df is None or df.empty:
        return []
    return [(idx.strftime("%Y-%m-%d"), float(v)) for idx, v in df["Close"].items()]


def build(hist):
    """yfinance 로 일별 종가·환율 수집 → 시뮬레이션. 실패 시 None (대시보드는 빈 카드 표시)."""
    s0 = start_date(hist)
    if not s0:
        return None
    since = (date.fromisoformat(s0) - timedelta(days=10)).isoformat()
    try:
        data = {tk: _history(tk, since) for tk in tickers_ever(hist) + [BENCH, FX_TICKER]}
        res = simulate(hist, Series(data))
        if res:
            print(f"가상 투자: {res['value_krw']:,}원 ({res['return_pct']:+.2f}%) vs SPY {res['spy_return_pct']:+.2f}%")
        return res
    except Exception as e:  # 시세 수집 실패가 대시보드 생성을 막지 않게
        print(f"가상 투자 시뮬레이션 실패: {e}")
        return None
