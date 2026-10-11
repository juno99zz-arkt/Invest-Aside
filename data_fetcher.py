"""
매일 17:00 KST 실행되는 데이터 업데이트 모듈.
- PEG_TICKERS       : M6 PEG 과열 모니터 (30종목)
- TOP50_TICKERS     : M7 미국 시총 상위 50종목 5지표 + 신호등
- KR_TOP50_TICKERS  : M8 한국 시총 상위 50종목 5지표 + 신호등
- M4 흑자전환 종목 추정 PER 계산용 현재가 fetch
"""

# 미국 시총 상위 50종목 (대형주 기준, yfinance 호환 티커)
TOP50_TICKERS = [
    ("AAPL",  "Apple"),
    ("MSFT",  "Microsoft"),
    ("NVDA",  "Nvidia"),
    ("GOOGL", "Alphabet"),
    ("AMZN",  "Amazon"),
    ("META",  "Meta"),
    ("TSLA",  "Tesla"),
    ("BRK-B", "Berkshire"),
    ("AVGO",  "Broadcom"),
    ("WMT",   "Walmart"),
    ("JPM",   "JPMorgan"),
    ("ORCL",  "Oracle"),
    ("LLY",   "Eli Lilly"),
    ("V",     "Visa"),
    ("MA",    "Mastercard"),
    ("NFLX",  "Netflix"),
    ("XOM",   "ExxonMobil"),
    ("COST",  "Costco"),
    ("JNJ",   "Johnson & Johnson"),
    ("HD",    "Home Depot"),
    ("PG",    "P&G"),
    ("BAC",   "Bank of America"),
    ("ABBV",  "AbbVie"),
    ("KO",    "Coca-Cola"),
    ("CVX",   "Chevron"),
    ("CRM",   "Salesforce"),
    ("AMD",   "AMD"),
    ("MRK",   "Merck"),
    ("TMO",   "Thermo Fisher"),
    ("PEP",   "PepsiCo"),
    ("ACN",   "Accenture"),
    ("ADBE",  "Adobe"),
    ("LIN",   "Linde"),
    ("MCD",   "McDonald's"),
    ("CSCO",  "Cisco"),
    ("ABT",   "Abbott"),
    ("IBM",   "IBM"),
    ("GE",    "GE Aerospace"),
    ("NOW",   "ServiceNow"),
    ("AXP",   "American Express"),
    ("PM",    "Philip Morris"),
    ("DIS",   "Disney"),
    ("INTU",  "Intuit"),
    ("T",     "AT&T"),
    ("MS",    "Morgan Stanley"),
    ("ISRG",  "Intuitive Surgical"),
    ("CAT",   "Caterpillar"),
    ("GS",    "Goldman Sachs"),
    ("VZ",    "Verizon"),
    ("RTX",   "RTX"),
]

# 미국 시총 상위 20 + 전세계 반도체 상위 10 (NVDA·AVGO 중복 제외) = 30종목
PEG_TICKERS = [
    # ── US market cap top 20 ──
    ("AAPL",      "Apple"),
    ("MSFT",      "Microsoft"),
    ("NVDA",      "Nvidia"),
    ("GOOGL",     "Alphabet"),
    ("AMZN",      "Amazon"),
    ("META",      "Meta"),
    ("TSLA",      "Tesla"),
    ("BRK-B",     "Berkshire"),
    ("AVGO",      "Broadcom"),
    ("WMT",       "Walmart"),
    ("JPM",       "JPMorgan"),
    ("ORCL",      "Oracle"),
    ("LLY",       "Eli Lilly"),
    ("V",         "Visa"),
    ("MA",        "Mastercard"),
    ("NFLX",      "Netflix"),
    ("XOM",       "ExxonMobil"),
    ("COST",      "Costco"),
    ("JNJ",       "Johnson & Johnson"),
    ("HD",        "Home Depot"),
    # ── Global semiconductor top 10 (NVDA·AVGO 제외) ──
    ("TSM",       "TSMC"),
    ("ASML",      "ASML"),
    ("AMD",       "AMD"),
    ("QCOM",      "Qualcomm"),
    ("TXN",       "Texas Instruments"),
    ("ARM",       "ARM Holdings"),
    ("MU",        "Micron"),
    ("AMAT",      "Applied Materials"),
    ("005930.KS", "삼성전자"),
    ("000660.KS", "SK하이닉스"),
]

# 한국 시총 상위 50종목 (코스피 + 일부 코스닥 대형주)
KR_TOP50_TICKERS = [
    ("005930.KS", "삼성전자"),
    ("000660.KS", "SK하이닉스"),
    ("373220.KS", "LG에너지솔루션"),
    ("207940.KS", "삼성바이오로직스"),
    ("005380.KS", "현대차"),
    ("000270.KS", "기아"),
    ("005490.KS", "POSCO홀딩스"),
    ("035420.KS", "NAVER"),
    ("005935.KS", "삼성전자우"),
    ("105560.KS", "KB금융"),
    ("035720.KS", "카카오"),
    ("068270.KS", "셀트리온"),
    ("028260.KS", "삼성물산"),
    ("055550.KS", "신한지주"),
    ("012330.KS", "현대모비스"),
    ("138040.KS", "메리츠금융지주"),
    ("086790.KS", "하나금융지주"),
    ("006400.KS", "삼성SDI"),
    ("051910.KS", "LG화학"),
    ("015760.KS", "한국전력"),
    ("011200.KS", "HMM"),
    ("032830.KS", "삼성생명"),
    ("003670.KS", "포스코퓨처엠"),
    ("009150.KS", "삼성전기"),
    ("010130.KS", "고려아연"),
    ("034730.KS", "SK"),
    ("010950.KS", "S-Oil"),
    ("011170.KS", "롯데케미칼"),
    ("251270.KS", "넷마블"),
    ("036570.KS", "엔씨소프트"),
    ("017670.KS", "SK텔레콤"),
    ("030200.KS", "KT"),
    ("018260.KS", "삼성에스디에스"),
    ("003550.KS", "LG"),
    ("000810.KS", "삼성화재"),
    ("024110.KS", "기업은행"),
    ("033780.KS", "KT&G"),
    ("066570.KS", "LG전자"),
    ("047810.KS", "한국항공우주"),
    ("003490.KS", "대한항공"),
    ("009830.KS", "한화솔루션"),
    ("042660.KS", "한화오션"),
    ("010140.KS", "삼성중공업"),
    ("064350.KS", "현대로템"),
    ("326030.KS", "SK바이오팜"),
    ("247540.KQ", "에코프로비엠"),
    ("086520.KQ", "에코프로"),
    ("196170.KQ", "알테오젠"),
    ("097950.KS", "CJ제일제당"),
    ("034220.KS", "LG디스플레이"),
]

def fetch_peg_ratios():
    """yfinance로 PEG 비율 가져오기. 실패 시 이전 값 유지."""
    try:
        import yfinance as yf
        result = []
        for ticker, name in PEG_TICKERS:
            try:
                info = yf.Ticker(ticker).info
                peg  = info.get("trailingPegRatio") or info.get("pegRatio")
                pe   = info.get("trailingPE") or info.get("forwardPE")
                if peg and pe:
                    result.append({
                        "ticker": ticker,
                        "name": name,
                        "peg": round(float(peg), 1),
                        "pe":  round(float(pe), 1),
                        "growth_rate": round(float(pe) / float(peg), 1) if peg else 0,
                    })
            except Exception:
                pass
        result.sort(key=lambda x: x["peg"], reverse=True)
        return result if result else None
    except ImportError:
        return None

def _signal_pe(pe):
    """PER 신호등: <15 녹 / 15-25 주 / >25 빨."""
    if pe is None: return "gray"
    if pe < 15: return "green"
    if pe <= 25: return "yellow"
    return "red"

def _signal_peg(peg):
    """PEG 신호등: <1 녹 / 1-1.5 주 / >1.5 빨."""
    if peg is None: return "gray"
    if peg < 1.0: return "green"
    if peg <= 1.5: return "yellow"
    return "red"

def _signal_growth(g):
    """매출·EPS 성장률 신호등 (%): >15 녹 / 5-15 주 / <5 빨."""
    if g is None: return "gray"
    if g > 15: return "green"
    if g >= 5: return "yellow"
    return "red"

def _signal_trend(arrow):
    """EPS 추이 신호등: ↑ 녹 / → 주 / ↓ 빨."""
    return {"↑": "green", "→": "yellow", "↓": "red"}.get(arrow, "gray")


def _median(xs):
    xs = sorted(x for x in xs if x is not None and x > 0)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _relative_signals(rows):
    """PER·추정 PER·PEG 신호등을 '같은 섹터 중앙값 대비'로 다시 매긴다.
    ≤ 0.8배 녹색 · 0.8~1.25배 노랑 · > 1.25배 빨강. 같은 섹터가 3개 미만이면 목록 전체 중앙값."""
    for key, sig in (("pe", "sig_pe"), ("fwd_pe", "sig_fwd_pe"), ("peg", "sig_peg")):
        overall = _median([r.get(key) for r in rows])
        for r in rows:
            v = r.get(key)
            peers = [x.get(key) for x in rows if x.get("sector") == r.get("sector")]
            med = _median(peers) if len([p for p in peers if p]) >= 3 else overall
            r.setdefault("sector_median", {})[key] = med and round(med, 1)
            if v is None or not med:
                r[sig] = "gray"
            elif v <= 0:
                r[sig] = "red"
            else:
                ratio = v / med
                r[sig] = "green" if ratio <= 0.8 else "yellow" if ratio <= 1.25 else "red"
    return rows


def _fetch_metrics(tickers_list):
    """(ticker, name) 리스트 → 지표 + 신호등 결과 리스트. 공통 fetcher.
    지표: PER (trailing) / 추정 PER (Yahoo forwardPE = 다음 회계연도 EPS 기준) / PEG / 매출↑ / EPS↑ / EPS추이
    결측은 다른 값으로 채우지 않고 missing 목록에 표시. PER·PEG 신호등은 섹터 중앙값 대비.
    """
    try:
        import yfinance as yf
        result = []
        for ticker, name in tickers_list:
            try:
                info = yf.Ticker(ticker).info
                trailing_pe = info.get("trailingPE")
                forward_pe  = info.get("forwardPE")
                peg = info.get("trailingPegRatio") or info.get("pegRatio")
                rev = info.get("revenueGrowth")     # 분수
                eps = info.get("earningsGrowth")    # 분수
                fwd_eps   = info.get("forwardEps")
                trail_eps = info.get("trailingEps")

                # EPS 컨센서스 추이: forwardEps vs trailingEps
                if fwd_eps is not None and trail_eps is not None and trail_eps != 0:
                    delta = (fwd_eps - trail_eps) / abs(trail_eps)
                    if delta > 0.05:  arrow = "↑"
                    elif delta < -0.05: arrow = "↓"
                    else: arrow = "→"
                else:
                    arrow = None          # 미수집 (변화 없음 '→' 과 구분)

                pe_r     = round(float(trailing_pe), 1) if trailing_pe else None
                fwd_pe_r = round(float(forward_pe),  1) if forward_pe  else None
                missing = [k for k, v in (("PER", pe_r), ("추정PER", fwd_pe_r), ("PEG", peg), ("매출↑", rev),
                                          ("EPS↑", eps), ("추이", arrow)) if v is None]
                same_pe = pe_r is not None and fwd_pe_r is not None and pe_r == fwd_pe_r

                rev_pct = round(rev * 100, 1) if rev is not None else None
                eps_pct = round(eps * 100, 1) if eps is not None else None
                peg_r   = round(float(peg), 1) if peg else None

                result.append({
                    "ticker": ticker,
                    "name": name,
                    "sector": info.get("sector"),
                    "missing": missing,
                    "same_pe": same_pe,
                    "pe": pe_r,
                    "fwd_pe": fwd_pe_r,
                    "peg": peg_r,
                    "rev_growth": rev_pct,
                    "eps_growth": eps_pct,
                    "eps_trend": arrow,
                    "sig_pe":     _signal_pe(pe_r),
                    "sig_fwd_pe": _signal_pe(fwd_pe_r),
                    "sig_peg":    _signal_peg(peg_r),
                    "sig_rev":    _signal_growth(rev_pct),
                    "sig_eps":    _signal_growth(eps_pct),
                    "sig_trend":  _signal_trend(arrow),
                })
            except Exception:
                pass
        return _relative_signals(result) if result else None
    except ImportError:
        return None

def fetch_top50_metrics():
    """미국 시총 상위 50 × 5지표 + 신호등."""
    return _fetch_metrics(TOP50_TICKERS)

def fetch_kr_top50_metrics():
    """한국 시총 상위 50 × 5지표 + 신호등."""
    return _fetch_metrics(KR_TOP50_TICKERS)


def fetch_prices(tickers):
    """현재가 dict 반환 — M4 추정 PER 계산용."""
    try:
        import yfinance as yf
        out = {}
        for t in tickers:
            try:
                info = yf.Ticker(t).info
                price = info.get("currentPrice") or info.get("regularMarketPrice")
                if price:
                    out[t] = float(price)
            except Exception:
                pass
        return out
    except ImportError:
        return {}


def fetch_and_update(m6_data: dict):
    """M6_DATA의 peg_ratios를 실시간 값으로 교체."""
    peg = fetch_peg_ratios()
    if peg:
        m6_data["peg_ratios"] = peg

if __name__ == "__main__":
    data = fetch_peg_ratios()
    if data:
        for d in data:
            flag = " ⚠️" if d["peg"] >= 1.5 else ""
            print(f"{d['ticker']:6s} PEG={d['peg']}  PE={d['pe']}{flag}")
    else:
        print("yfinance 미설치 또는 API 오류")
