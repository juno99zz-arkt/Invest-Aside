"""
시장 브리핑용 뉴스 후보 수집 (RSS, 비용 없음).
Claude 가 웹검색으로 기사를 찾는 대신, 여기서 모은 실제 기사 목록 중에서 고르게 해
웹검색 횟수와 검색 결과로 불어나는 입력 토큰을 줄인다.
"""
import html
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import feedparser
import requests

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# 요약문이 들어 있는 언론사 피드
FEEDS = [
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),   # Top News
    ("CNBC", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),    # Economy
    ("CNBC", "https://www.cnbc.com/id/15839069/device/rss/rss.html"),    # Investing
    ("CNBC", "https://www.cnbc.com/id/19854910/device/rss/rss.html"),    # Technology
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("Nasdaq", "https://www.nasdaq.com/feed/rssoutbound?category=Markets"),
    ("Investing.com", "https://www.investing.com/rss/news_25.rss"),
    ("Seeking Alpha", "https://seekingalpha.com/market_currents.xml"),
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
]
# 8-K 항목 코드 → 한글 (기업 공식 공시를 뉴스보다 우선)
ITEMS_8K = {"1.01": "중요 계약 체결", "1.02": "중요 계약 해지", "1.03": "파산·회생", "2.01": "자산 인수·처분 완료",
             "2.02": "실적 발표", "2.03": "채무 발생", "2.05": "구조조정 비용", "2.06": "자산 손상", "3.02": "주식 발행",
             "4.02": "재무제표 신뢰성 문제", "5.02": "경영진·이사 변경", "7.01": "공정공시(IR 자료)", "8.01": "기타 중요 사항"}
MAX_FILINGS = 25
# Google 뉴스 검색 (여러 언론사, 제목 끝의 ' - 언론사' 로 출처 표기)
QUERIES = [
    "stock market", "Federal Reserve interest rates", "inflation CPI jobs report",
    "AI chips data center spending", "earnings guidance", "S&P 500 Nasdaq outlook",
]
MAX_PER_SOURCE = 15
MAX_TOTAL = 70


def _get(url):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    return feedparser.parse(r.content)


def _date(entry):
    for key in ("published", "updated"):
        if entry.get(key):
            try:
                d = parsedate_to_datetime(entry[key])
                return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
    return None


def _clean(text, limit=280):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _norm(title):
    return re.sub(r"[^a-z0-9]", "", title.lower())[:60]


def fetch_filings(tickers, days=14):
    """보유·관심·후보 종목의 최근 8-K 공시 (SEC EDGAR, SEC_USER_AGENT 필요)."""
    try:
        import sec_data
        if not sec_data._ua():
            return []
        ciks = sec_data._cik_map()
    except Exception as e:
        print(f"  공시 목록 실패: {e}")
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    out = []
    for tk in tickers:
        cik = ciks.get(tk)
        if not cik:
            continue
        try:
            rec = sec_data._get(f"https://data.sec.gov/submissions/CIK{cik}.json", as_json=True)["filings"]["recent"]
        except Exception:
            continue
        for i, form in enumerate(rec["form"]):
            if rec["filingDate"][i] < cutoff:
                break
            if form not in ("8-K", "6-K"):
                continue
            codes = [c.strip() for c in (rec.get("items", [""] * len(rec["form"]))[i] or "").split(",") if c.strip()]
            labels = [ITEMS_8K.get(c, c) for c in codes if c != "9.01"] or ["공시"]
            acc = rec["accessionNumber"][i]
            url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/"
                   f"{rec['primaryDocument'][i]}")
            out.append({"title": f"{tk} {form}: {' · '.join(labels)}", "source": "SEC EDGAR (기업 공시)", "url": url,
                        "published": rec["filingDate"][i], "snippet": f"{tk} 공식 공시 항목: {', '.join(codes)}",
                        "kind": "공시", "tickers": [tk]})
    out.sort(key=lambda x: x["published"], reverse=True)
    print(f"  기업 공시 후보 {len(out[:MAX_FILINGS])}건")
    return out[:MAX_FILINGS]


def fetch_candidates(tickers=(), days=14):
    """최근 days일 후보 [{id, title, source, url, published, snippet, kind}] — 기업 공시 먼저, 그다음 기사(최신순)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    sources = list(FEEDS)
    sources += [(None, f"https://news.google.com/rss/search?q={quote(q + f' when:{days}d')}&hl=en-US&gl=US&ceid=US:en")
                for q in QUERIES]
    if tickers:
        q = " OR ".join(f"{t} stock" for t in tickers[:6])
        sources.append((None, f"https://news.google.com/rss/search?q={quote(f'({q}) when:{days}d')}&hl=en-US&gl=US&ceid=US:en"))

    items, seen = [], set()
    for source, url in sources:
        try:
            feed = _get(url)
        except Exception as e:  # 한 피드 실패는 무시
            print(f"  RSS 실패 ({url[:60]}): {e}")
            continue
        n = 0
        for e in feed.entries:
            title, link, d = _clean(e.get("title"), 300), e.get("link"), _date(e)
            if not title or not link or not d or d < cutoff:
                continue
            src = source
            if src is None:  # Google 뉴스: 'Headline - Source'
                src = (e.get("source") or {}).get("title")
                if " - " in title:
                    title, tail = title.rsplit(" - ", 1)
                    src = src or tail
            key = _norm(title)
            if key in seen:
                continue
            seen.add(key)
            snippet = _clean(e.get("summary")) if source else ""
            items.append({"title": title, "source": src or "", "url": link, "kind": "뉴스",
                          "published": d.date().isoformat(), "snippet": snippet, "_dt": d})
            n += 1
            if n >= MAX_PER_SOURCE:
                break

    items.sort(key=lambda x: x["_dt"], reverse=True)
    # 출처 다양화: 한 출처가 후보의 절반을 넘지 않게
    per_src, diverse = {}, []
    for x in items:
        per_src[x["source"]] = per_src.get(x["source"], 0) + 1
        if per_src[x["source"]] <= MAX_TOTAL // 3:
            diverse.append(x)
    items = fetch_filings(list(dict.fromkeys(tickers)), days) + diverse[:MAX_TOTAL]
    for i, x in enumerate(items):
        x.pop("_dt", None)
        x["id"] = i
    print(f"  뉴스 후보 {len(items)}건 수집 (RSS)")
    return items


if __name__ == "__main__":
    for x in fetch_candidates(["NVDA"])[:10]:
        print(x["published"], x["source"], "|", x["title"])
