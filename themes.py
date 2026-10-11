"""
공통 위험(테마) 노출 계산 — 종목 수가 아니라 같은 위험에 얼마나 몰려 있는지 본다.
- 종목은 여러 테마에 속할 수 있다 (예: NVDA = AI·반도체 인프라 + 미국 대형 기술주).
- ETF 는 테마 비중(대략치)으로 분해한다.
- '달러 자산' 은 원화 투자자 관점의 환율 위험 (미국 주식·미국 ETF 전부).
"""

THEME_TICKERS = {
    "AI·반도체 인프라": {"NVDA", "AMD", "AVGO", "TSM", "MU", "ASML", "AMAT", "LRCX", "KLAC", "ARM", "MRVL", "ANET",
                     "SMCI", "CRWV", "ORCL", "VRT", "CIEN", "INTC", "QCOM", "TXN", "ADI", "NXPI", "MCHP", "ON",
                     "WDC", "STX", "SNDK", "DELL", "HPE", "CSCO", "APH", "CEG", "VST", "005930.KS", "000660.KS"},
    "미국 대형 기술주": {"AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "AVGO", "ORCL", "TSLA", "NFLX", "ADBE",
                   "CRM", "NOW", "INTU", "PLTR"},
    "가상자산": {"MSTR", "COIN", "CRCL", "HOOD", "MARA", "RIOT", "IBIT"},
    "에너지·원자재": set(),   # 섹터로 판정
}
SECTOR_THEMES = {"Energy": "에너지·원자재", "Basic Materials": "에너지·원자재",
                 "에너지": "에너지·원자재", "소재": "에너지·원자재"}
INDUSTRY_WORDS = {"Semiconductor": "AI·반도체 인프라"}

# ETF → {테마: 비중(0~1)} (대략치, 지수 구성 변화에 따라 달라짐)
ETF_THEMES = {
    "SPY": {"AI·반도체 인프라": 0.15, "미국 대형 기술주": 0.33, "에너지·원자재": 0.05},
    "VOO": {"AI·반도체 인프라": 0.15, "미국 대형 기술주": 0.33, "에너지·원자재": 0.05},
    "IVV": {"AI·반도체 인프라": 0.15, "미국 대형 기술주": 0.33, "에너지·원자재": 0.05},
    "QQQ": {"AI·반도체 인프라": 0.30, "미국 대형 기술주": 0.55},
    "QQQM": {"AI·반도체 인프라": 0.30, "미국 대형 기술주": 0.55},
    "SOXX": {"AI·반도체 인프라": 1.0}, "SMH": {"AI·반도체 인프라": 1.0}, "SOXL": {"AI·반도체 인프라": 3.0},
    "XLK": {"AI·반도체 인프라": 0.40, "미국 대형 기술주": 0.60},
    "XLE": {"에너지·원자재": 1.0},
}
KRW_ASSET_SUFFIX = (".KS", ".KQ")


def themes_of(ticker, sector=None, industry=None):
    """{테마: 노출 계수}. 종목은 계수 1, ETF 는 분해 비중."""
    tk = (ticker or "").upper()
    if tk in ETF_THEMES:
        out = dict(ETF_THEMES[tk])
    else:
        out = {th: 1.0 for th, s in THEME_TICKERS.items() if tk in s}
        if sector in SECTOR_THEMES:
            out[SECTOR_THEMES[sector]] = 1.0
        for w, th in INDUSTRY_WORDS.items():
            if industry and w in industry:
                out[th] = 1.0
    if not tk.endswith(KRW_ASSET_SUFFIX) and tk not in ("CASH", "KRW"):
        out["달러 자산(환율)"] = 1.0
    return out


def exposure(weights, meta=None):
    """weights: {ticker: % of total} → {테마: %} (높은 순)."""
    meta = meta or {}
    exp = {}
    for tk, w in weights.items():
        m = meta.get(tk, {})
        for th, k in themes_of(tk, m.get("sector"), m.get("industry")).items():
            exp[th] = exp.get(th, 0) + w * k
    return dict(sorted(((k, round(v, 1)) for k, v in exp.items()), key=lambda kv: -kv[1]))
