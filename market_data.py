"""
yfinance 기반 시장 데이터 수집 (주간 실행).
- fetch_snapshots : 유니버스 전체 1차 스냅샷 (밸류에이션·수익성·추정치 변화)
- fetch_details   : 후보·보유 종목 2차 상세 (분기 실적·현금흐름·희석·서프라이즈·주가)
"""
import math
import time
from concurrent.futures import ThreadPoolExecutor

import yfinance as yf


def _num(v):
    """숫자형만 float 로, NaN/None/문자열은 None."""
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def _pct_change(new, old):
    if new is None or old is None or old == 0:
        return None
    return (new - old) / abs(old) * 100


def _retry(fn, tries=3):
    for i in range(tries):
        try:
            return fn()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def _row(df, period, col):
    try:
        return _num(df.loc[period, col])
    except Exception:
        return None


def _snapshot(ticker, name):
    t = yf.Ticker(ticker)
    info = _retry(lambda: t.info)
    if not info or _num(info.get("marketCap")) is None:
        return None
    s = {
        "ticker": ticker,
        "name": info.get("shortName") or name,
        "sector": info.get("sector") or "Unknown",
        "industry": info.get("industry"),
        "market_cap": _num(info.get("marketCap")),
        "price": _num(info.get("currentPrice") or info.get("regularMarketPrice")),
        "trailing_pe": _num(info.get("trailingPE")),
        "forward_pe": _num(info.get("forwardPE")),
        "peg": _num(info.get("trailingPegRatio")),
        "trailing_eps": _num(info.get("trailingEps")),
        "forward_eps": _num(info.get("forwardEps")),
        "revenue_growth": _num(info.get("revenueGrowth")),
        "earnings_growth": _num(info.get("earningsGrowth")),
        "gross_margin": _num(info.get("grossMargins")),
        "op_margin": _num(info.get("operatingMargins")),
        "roe": _num(info.get("returnOnEquity")),
        "fcf": _num(info.get("freeCashflow")),
        "ocf": _num(info.get("operatingCashflow")),
        "total_debt": _num(info.get("totalDebt")),
        "total_cash": _num(info.get("totalCash")),
        "ebitda": _num(info.get("ebitda")),
        "revenue": _num(info.get("totalRevenue")),
        "analysts": _num(info.get("numberOfAnalystOpinions")),
        "rec_mean": _num(info.get("recommendationMean")),
        "target_mean": _num(info.get("targetMeanPrice")),
        "eps_low_1y": None, "eps_high_1y": None,
        # 회계연도 종료일(epoch) — PER 기준(올해/내년/향후 12개월) 구분용
        "last_fy_end": _num(info.get("lastFiscalYearEnd")),
        "next_fy_end": _num(info.get("nextFiscalYearEnd")),
    }
    # 추정치 변화 (다음 회계연도 +1y EPS 컨센서스)
    try:
        trend = _retry(lambda: t.eps_trend)
        cur = _row(trend, "+1y", "current")
        s["eps_next_fy"] = cur
        s["rev_7d"] = _pct_change(cur, _row(trend, "+1y", "7daysAgo"))
        s["rev_30d"] = _pct_change(cur, _row(trend, "+1y", "30daysAgo"))
        s["rev_90d"] = _pct_change(cur, _row(trend, "+1y", "90daysAgo"))
        base = _row(trend, "+1y", "90daysAgo")
        s["rev_path"] = [
            _pct_change(_row(trend, "+1y", c), base)
            for c in ("90daysAgo", "60daysAgo", "30daysAgo", "7daysAgo", "current")
        ]
        s["eps_0q"] = _row(trend, "0q", "current")
        s["eps_1q"] = _row(trend, "+1q", "current")
        s["eps_0y"] = _row(trend, "0y", "current")
    except Exception:
        pass
    try:  # 내년 EPS 추정치 분산 (근거 상태 표시용)
        ee = _retry(lambda: t.earnings_estimate)
        s["eps_low_1y"] = _row(ee, "+1y", "low")
        s["eps_high_1y"] = _row(ee, "+1y", "high")
    except Exception:
        pass
    try:
        revs = _retry(lambda: t.eps_revisions)
        s["up_30d"] = _row(revs, "+1y", "upLast30days")
        s["down_30d"] = _row(revs, "+1y", "downLast30days")
    except Exception:
        pass
    try:
        g = _retry(lambda: t.growth_estimates)
        s["growth_0q"] = _row(g, "0q", "stockTrend")
        s["growth_1q"] = _row(g, "+1q", "stockTrend")
        s["growth_0y"] = _row(g, "0y", "stockTrend")
        s["growth_1y"] = _row(g, "+1y", "stockTrend")
    except Exception:
        pass
    return s


def fetch_snapshots(universe, workers=4):
    """universe: [(ticker, name)] → {ticker: snapshot}"""
    out = {}

    def job(pair):
        try:
            return _snapshot(*pair)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, s in enumerate(ex.map(job, universe), 1):
            if s:
                out[s["ticker"]] = s
            if i % 100 == 0:
                print(f"  스냅샷 {i}/{len(universe)}")
    print(f"스냅샷 {len(out)}/{len(universe)}종목 수집")
    return out


def _series(df, row, n):
    """재무표 행 → 최근 n개 (오래된→최신) [(기간, 값)]."""
    if df is None or df.empty or row not in df.index:
        return []
    cols = sorted(df.columns)[-n:]
    return [(c.strftime("%Y-%m"), _num(df.loc[row, c])) for c in cols]


def fetch_annual_margins(tickers, workers=4):
    """{ticker: [연간 영업이익률 %, 오래된→최신]} — 사이클 정점 할인 판단용."""
    def job(tk):
        try:
            ais = _retry(lambda: yf.Ticker(tk).income_stmt)
            rev, oi = dict(_series(ais, "Total Revenue", 4)), dict(_series(ais, "Operating Income", 4))
            return tk, [round(oi[p] / rev[p] * 100, 1) if rev.get(p) and oi.get(p) is not None else None
                        for p in sorted(rev)]
        except Exception:
            return tk, []

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(ex.map(job, tickers))


def _details(ticker):
    t = yf.Ticker(ticker)
    d = {"ticker": ticker}
    qis = _retry(lambda: t.quarterly_income_stmt)
    d["q_revenue"] = _series(qis, "Total Revenue", 5)
    d["q_op_income"] = _series(qis, "Operating Income", 5)
    d["q_eps"] = _series(qis, "Diluted EPS", 5)
    d["q_op_margin"] = [
        (p, round(oi / rv * 100, 1) if oi is not None and rv else None)
        for (p, rv), (_, oi) in zip(d["q_revenue"], d["q_op_income"])
    ] if len(d["q_revenue"]) == len(d["q_op_income"]) else []

    ais = _retry(lambda: t.income_stmt)
    d["y_revenue"] = _series(ais, "Total Revenue", 4)
    d["y_op_income"] = _series(ais, "Operating Income", 4)

    qcf = _retry(lambda: t.quarterly_cashflow)
    fcf_q = [v for _, v in _series(qcf, "Free Cash Flow", 4)]
    sbc_q = [v for _, v in _series(qcf, "Stock Based Compensation", 4)]
    buyback_q = [v for _, v in _series(qcf, "Repurchase Of Capital Stock", 4)]
    d["fcf_ttm"] = sum(fcf_q) if len(fcf_q) == 4 and None not in fcf_q else None
    d["sbc_ttm"] = sum(sbc_q) if len(sbc_q) == 4 and None not in sbc_q else None
    d["buyback_ttm"] = -sum(buyback_q) if len(buyback_q) == 4 and None not in buyback_q else None
    d["q_capex"] = [(p, -v if v is not None else None) for p, v in _series(qcf, "Capital Expenditure", 5)]

    qbs = _retry(lambda: t.quarterly_balance_sheet)
    shares = _series(qbs, "Ordinary Shares Number", 5)
    if len(shares) >= 2 and shares[0][1] and shares[-1][1]:
        d["share_change_pct"] = round(_pct_change(shares[-1][1], shares[0][1]), 2)
        d["share_change_span"] = f"{shares[0][0]}→{shares[-1][0]}"

    try:
        eh = t.earnings_history
        d["eps_surprises"] = [
            {"quarter": q.strftime("%Y-%m") if hasattr(q, "strftime") else str(q),
             "actual": _num(r["epsActual"]), "estimate": _num(r["epsEstimate"]),
             "surprise_pct": round(_num(r["surprisePercent"]) * 100, 1) if _num(r["surprisePercent"]) is not None else None}
            for q, r in eh.iterrows()
        ]
    except Exception:
        d["eps_surprises"] = []

    try:
        cal = t.calendar or {}
        dates = cal.get("Earnings Date") or []
        d["next_earnings"] = dates[0].isoformat() if dates else None
    except Exception:
        d["next_earnings"] = None

    try:
        hist = t.history(period="1y")["Close"].dropna()  # 빈 거래일 NaN 이 JSON 에 섞이지 않도록
        last = float(hist.iloc[-1])
        d["return_1y_pct"] = round(_pct_change(last, float(hist.iloc[0])), 1)
        d["from_52w_high_pct"] = round(_pct_change(last, float(hist.max())), 1)
    except Exception:
        pass
    return d


def fetch_details(tickers, workers=4):
    out = {}

    def job(tk):
        try:
            return _details(tk)
        except Exception as e:
            print(f"  상세 수집 실패 {tk}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for d in ex.map(job, sorted(set(tickers))):
            if d:
                out[d["ticker"]] = d
    print(f"상세 {len(out)}종목 수집")
    return out
