"""
가상 투자 시뮬레이션 — Agent 추천 비중대로 5천만원을 운용했다면 계좌는 지금 어떤 상태인가.

규칙
- 시작: 추천 포트폴리오에 첫 종목이 들어간 주간 결정일 (NVDA 2026-09-13)
- 원금 5천만원(원화). 종목 매수 시 그날 환율로 환전, 매도 대금은 원화 현금으로 복귀. 미투자분은 원화 현금
- 비중: 주간 결정의 weights(5% 단위, Agent 가 결정). 기록이 없으면 보유 종목당 20%(최대 5종목 = 5칸)
- 비중·구성이 바뀐 주에만 리밸런싱. 체결가는 결정 당시 기준가(편입가·제외가, 없으면 결정일 직전 종가)
- 정수 주, 매매 수수료 0.25%. 세금·환전 스프레드 미반영
- 벤치마크: ① 같은 비중만큼 SPY 에 투자(나머지 현금) ② 원금 전액 SPY

simulate() 는 순수 함수(네트워크 없음) → 로컬 테스트 가능. build() 는 yfinance 로 시세 수집 후 simulate().
"""
import math
from bisect import bisect_right
from datetime import date, timedelta

INITIAL_KRW = 50_000_000
FEE = 0.0025
SLOT_PCT = 20            # 비중 기록이 없을 때 종목당 기본 비중 (5종목 × 20%)
STEP = 5                 # 비중 단위 (%)
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


def normalize_weights(weights):
    """5% 단위 정수, 종목당 5~100, 합계 100 이하 (넘으면 큰 비중부터 5%씩 축소)."""
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
    """주간 결정의 비중. 기록이 없으면 이전 비중 유지, 신규는 기본 20%."""
    tickers = list(dict.fromkeys(week.get("portfolio_after", [])))
    given = week.get("weights") or {}
    return normalize_weights({tk: given.get(tk, prev.get(tk, SLOT_PCT)) for tk in tickers})


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
    fx0 = series.at(FX_TICKER, s0)
    if not fx0:
        return None

    st = {"cash": float(INITIAL_KRW), "pos": {}}       # pos: tk -> {shares, cost_usd, cost_krw}
    bench = {"cash": float(INITIAL_KRW), "shares": 0.0}  # 같은 비중 SPY
    trades, weight_log = [], []

    def fx_at(day):
        return series.at(FX_TICKER, day) or fx0

    def px_for(tk, day, side, held):
        if held:                      # 기존 보유 리밸런싱은 결정일 직전 종가
            return series.at(tk, day) or (buy_px if side == "buy" else sell_px).get((tk, day))
        return (buy_px if side == "buy" else sell_px).get((tk, day)) or series.at(tk, day)

    def trade(day, tk, side, n, px, why, target):
        if n <= 0:
            return
        fx = fx_at(day)
        usd = n * px
        krw = usd * fx
        p = st["pos"].setdefault(tk, {"shares": 0, "cost_usd": 0.0, "cost_krw": 0.0})
        if side == "buy":
            st["cash"] -= krw * (1 + FEE)
            p["shares"] += n
            p["cost_usd"] += usd
            p["cost_krw"] += krw * (1 + FEE)
        else:
            ratio = n / p["shares"]
            st["cash"] += krw * (1 - FEE)
            p["cost_usd"] -= p["cost_usd"] * ratio
            p["cost_krw"] -= p["cost_krw"] * ratio
            p["shares"] -= n
            if p["shares"] == 0:
                st["pos"].pop(tk)
        trades.append({"date": day, "ticker": tk, "side": "매수" if side == "buy" else "매도", "shares": n,
                       "price_usd": round(px, 2), "fx": round(fx, 2), "amount_krw": round(krw),
                       "fee_krw": round(krw * FEE), "target_pct": target, "reason": why})

    def rebalance(day, weights):
        fx = fx_at(day)
        for tk in list(st["pos"]):                       # 1) 제외 종목 전량 매도
            if tk not in weights:
                px = px_for(tk, day, "sell", False)
                if px:
                    trade(day, tk, "sell", st["pos"][tk]["shares"], px, "제외", 0)
        live = {tk: w for tk, w in weights.items() if px_for(tk, day, "buy", tk in st["pos"])}
        pxs = {tk: px_for(tk, day, "buy", tk in st["pos"]) for tk in live}
        total = st["cash"] + sum(st["pos"][tk]["shares"] * pxs[tk] * fx for tk in live if tk in st["pos"])
        for tk, w in live.items():                       # 2) 목표 초과분 매도
            if tk in st["pos"]:
                excess = st["pos"][tk]["shares"] * pxs[tk] * fx - total * w / 100
                n = math.floor(excess / (pxs[tk] * fx))
                if n > 0:
                    trade(day, tk, "sell", n, pxs[tk], f"비중 축소 → {w}%", w)
        for tk, w in live.items():                       # 3) 목표 부족분 매수
            held = tk in st["pos"]
            have = st["pos"][tk]["shares"] * pxs[tk] * fx if held else 0
            budget = min(total * w / 100 - have, st["cash"])
            n = math.floor(budget / (pxs[tk] * fx * (1 + FEE)))
            if n > 0:
                trade(day, tk, "buy", n, pxs[tk], (f"비중 확대 → {w}%" if held else f"신규편입 {w}%"), w)
        # 같은 비중 SPY 벤치마크
        spy = series.at(BENCH, day)
        if spy:
            btotal = bench["cash"] + bench["shares"] * spy * fx
            target = btotal * sum(weights.values()) / 100
            diff = target - bench["shares"] * spy * fx
            if diff > 0:
                bench["shares"] += diff / (1 + FEE) / (spy * fx)
                bench["cash"] -= diff
            elif diff < 0:
                bench["shares"] += diff / (spy * fx)
                bench["cash"] += -diff * (1 - FEE)
        weight_log.append({"date": day, "weights": dict(weights), "cash_pct": 100 - sum(weights.values())})

    events, prev = [], None
    for w in weeks:
        cur = week_weights(w, prev or {})
        if prev is None or cur != prev:
            events.append((w["date"], cur))
        prev = cur

    spy0 = series.at(BENCH, s0)
    spy_full = INITIAL_KRW * (1 - FEE) / (spy0 * fx0) if spy0 else 0

    def value(day):
        fx = fx_at(day)
        stock = sum(p["shares"] * (series.at(tk, day) or 0) * fx for tk, p in st["pos"].items())
        spy = series.at(BENCH, day) or spy0
        return stock, st["cash"] + stock, bench["cash"] + bench["shares"] * spy * fx, spy_full * spy * fx

    days = [d for d in series.dates(BENCH) if d > s0 and (today is None or d <= today)]
    curve = {"dates": [s0], "value": [INITIAL_KRW], "bench": [INITIAL_KRW], "spy_full": [INITIAL_KRW]}
    while events and events[0][0] <= s0:
        rebalance(*events.pop(0))
    for d in days:
        while events and events[0][0] <= d:
            rebalance(*events.pop(0))
        _, v, b, f = value(d)
        curve["dates"].append(d)
        curve["value"].append(round(v))
        curve["bench"].append(round(b))
        curve["spy_full"].append(round(f))
    while events:                                        # 마지막 결정 이후 아직 거래일이 없는 경우
        rebalance(*events.pop(0))

    as_of = days[-1] if days else s0
    stock_now, total_now, bench_now, full_now = value(as_of)
    fx_now = fx_at(as_of)
    peak, mdd = 0, 0.0
    for v in curve["value"]:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    cur_w = weight_log[-1]["weights"] if weight_log else {}
    cost_krw = sum(p["cost_krw"] for p in st["pos"].values())

    holdings = []
    for tk, p in st["pos"].items():
        px = series.at(tk, as_of) or 0
        val = p["shares"] * px * fx_now
        holdings.append({
            "ticker": tk, "name": names.get(tk, tk), "shares": p["shares"],
            "avg_cost_usd": round(p["cost_usd"] / p["shares"], 2), "price_usd": round(px, 2),
            "cost_krw": round(p["cost_krw"]), "value_krw": round(val),
            "weight_pct": round(val / total_now * 100, 1), "target_pct": cur_w.get(tk),
            "return_pct": round((val / p["cost_krw"] - 1) * 100, 1) if p["cost_krw"] else None,
        })
    holdings.sort(key=lambda h: -h["value_krw"])

    return {
        "start_date": s0, "as_of": as_of, "initial_krw": INITIAL_KRW, "fee_pct": FEE * 100, "slot_pct": SLOT_PCT,
        "fx_start": round(fx0, 2), "fx_now": round(fx_now, 2),
        "value_krw": round(total_now), "pnl_krw": round(total_now - INITIAL_KRW),
        "return_pct": round((total_now / INITIAL_KRW - 1) * 100, 2),
        "stock_krw": round(stock_now), "cash_krw": round(st["cash"]),
        "invested_pct": round(stock_now / total_now * 100, 1) if total_now else 0,
        "target_invested_pct": sum(cur_w.values()),
        "stock_return_pct": round((stock_now / cost_krw - 1) * 100, 2) if cost_krw else None,
        "bench_value_krw": round(bench_now), "bench_return_pct": round((bench_now / INITIAL_KRW - 1) * 100, 2),
        "spy_full_value_krw": round(full_now), "spy_full_return_pct": round((full_now / INITIAL_KRW - 1) * 100, 2),
        "max_drawdown_pct": round(mdd * 100, 2),
        "holdings": holdings, "trades": trades, "weights_history": weight_log, "curve": curve,
        "rule": "원금 5천만원(원화). Agent가 정한 비중(5% 단위, 기본 종목당 20%)만큼만 매수하고 나머지는 원화 현금으로 보유. "
                "비중·구성이 바뀐 주에만 리밸런싱, 체결은 결정 당시 기준가·당일 환율, 정수 주, 수수료 0.25%. "
                "벤치마크는 같은 비중만큼 SPY에 투자한 계좌. 세금·환전 스프레드 미반영.",
    }


def _history(tk, start):
    import yfinance as yf
    df = yf.Ticker(tk).history(start=start, auto_adjust=False)
    if df is None or df.empty:
        return []
    return [(idx.strftime("%Y-%m-%d"), float(v)) for idx, v in df["Close"].items()]


def build(hist):
    """yfinance 로 일별 종가·환율 수집 → 시뮬레이션. 실패 시 None (대시보드는 대기 카드 표시)."""
    s0 = start_date(hist)
    if not s0:
        return None
    since = (date.fromisoformat(s0) - timedelta(days=10)).isoformat()
    try:
        data = {tk: _history(tk, since) for tk in tickers_ever(hist) + [BENCH, FX_TICKER]}
        res = simulate(hist, Series(data))
        if res:
            print(f"가상 투자: {res['value_krw']:,}원 ({res['return_pct']:+.2f}%) · 주식 {res['invested_pct']}% · "
                  f"같은 비중 SPY {res['bench_return_pct']:+.2f}%")
        return res
    except Exception as e:  # 시세 수집 실패가 대시보드 생성을 막지 않게
        print(f"가상 투자 시뮬레이션 실패: {e}")
        return None
