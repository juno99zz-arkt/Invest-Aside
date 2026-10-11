"""
SEC EDGAR 수집 (주간 실행).
- fetch_13f            : 대가 5인 최신 13F-HR 2개 분기 비교 (M1)
- fetch_insider_trades : 유니버스 종목의 최근 Form 4 장내 매수/매도 (M5)

SEC 정책상 User-Agent 에 연락처(이메일)가 필요 → 환경변수 SEC_USER_AGENT.
미설정 시 SEC 수집은 건너뛰고 None 반환.
"""
import os
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import requests

INVESTORS = [
    ("Warren Buffett", "Berkshire Hathaway", "0001067983"),
    ("Bill Ackman", "Pershing Square", "0001336528"),
    ("Cathie Wood", "ARK Invest", "0001697748"),
    ("Stan Druckenmiller", "Duquesne Family Office", "0001536411"),
    ("Chase Coleman", "Tiger Global", "0001167483"),
]

_lock = threading.Lock()
_last_call = [0.0]


def _ua():
    return os.environ.get("SEC_USER_AGENT", "").strip()


def _get(url, as_json=False, ua=None):
    """SEC 요청 (초당 8회 이하로 제한)."""
    with _lock:
        wait = 0.125 - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
    for i in range(3):
        r = requests.get(url, headers={"User-Agent": ua or _ua(), "Accept-Encoding": "gzip, deflate"}, timeout=30)
        if r.status_code == 200:
            return r.json() if as_json else r.text
        if r.status_code in (429, 503):
            time.sleep(3 * (i + 1))
            continue
        r.raise_for_status()
    r.raise_for_status()


def check_access():
    """SEC 접근 점검. 실패 시 원인(User-Agent 형식 vs 서버 IP 차단)을 값 노출 없이 로그로 안내."""
    ua = _ua()
    if not ua:
        print("::warning::SEC_USER_AGENT 미설정 — 13F·Form 4 수집 건너뜀")
        return False
    probe = "https://data.sec.gov/submissions/CIK0001067983.json"
    try:
        _get(probe, as_json=True)
        print("SEC 접근 OK")
        return True
    except Exception as e:
        print(f"::warning::SEC 접근 실패 ({e.__class__.__name__}) — 13F·Form 4 수집 건너뜀")
    # 공개 로그이므로 값은 출력하지 않고 형식만 점검
    words = ua.split()
    print("  User-Agent 형식 점검:",
          {"ASCII만 사용": ua.isascii(), "공백으로 구분된 단어 2개 이상": len(words) >= 2,
           "마지막 단어가 이메일(@ 포함)": "@" in words[-1] and "." in words[-1].split("@")[-1],
           "@ 개수 1개": ua.count("@") == 1})
    try:
        _get(probe, as_json=True, ua="Invest Dashboard admin@example.org")
        print("  기준 User-Agent 로는 접근 성공 → 등록한 SEC_USER_AGENT 값의 형식 문제")
    except Exception:
        print("  기준 User-Agent 로도 실패 → SEC가 이 서버(IP)를 차단 중")
    return False


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _find(el, name):
    for child in el.iter():
        if _local(child.tag) == name:
            return child
    return None


def _text(el, name):
    f = _find(el, name) if el is not None else None
    if f is None:
        return None
    v = _find(f, "value")
    return (v.text if v is not None else f.text or "").strip() or None


# ── 13F ──────────────────────────────────────────────────────────
def _cusip_to_ticker(cusips):
    """OpenFIGI (무료, 키 없음: 요청당 10건)로 CUSIP → 미국 티커."""
    out = {}
    cusips = list(cusips)
    for i in range(0, len(cusips), 10):
        batch = cusips[i:i + 10]
        try:
            r = requests.post("https://api.openfigi.com/v3/mapping",
                              json=[{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in batch], timeout=30)
            if r.status_code == 429:
                time.sleep(60)
                r = requests.post("https://api.openfigi.com/v3/mapping",
                                  json=[{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in batch], timeout=30)
            for c, res in zip(batch, r.json()):
                data = res.get("data") or []
                if data and data[0].get("ticker"):
                    out[c] = data[0]["ticker"].replace("/", "-")
        except Exception as e:
            print(f"  OpenFIGI 실패: {e}")
        time.sleep(2.5)
    return out


def _holdings(cik, accession):
    """13F 정보표 → {cusip: {name, value, shares}} (옵션 행 제외)."""
    folder = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"
    idx = _get(folder + "index.json", as_json=True)
    xmls = [it["name"] for it in idx["directory"]["item"]
            if it["name"].lower().endswith(".xml") and it["name"] != "primary_doc.xml"]
    root = ET.fromstring(_get(folder + xmls[0]))
    out = {}
    for row in root:
        if _local(row.tag) != "infoTable" or _find(row, "putCall") is not None:
            continue
        cusip = _find(row, "cusip").text.strip()
        h = out.setdefault(cusip, {"name": _find(row, "nameOfIssuer").text.strip(), "value": 0.0, "shares": 0.0})
        h["value"] += float(_find(row, "value").text)
        h["shares"] += float(_find(row, "sshPrnamt").text)
    return out


GURU_MIN_WEIGHT = 0.005   # 포트폴리오 비중 0.5% 이상 종목만 매수·매도 신호로 인정
GURU_CHANGE = 10.0        # 주식 수 ±10% 이상 변화 (신규 편입 = +∞, 전량 청산 = -100%)


def _share_change(before, after):
    """직전→최근 분기 주식 수 변화율(%). 주식 분할로 보이는 정수배 변화는 0 처리."""
    b = before["shares"] if before else 0
    a = after["shares"] if after else 0
    if b == 0:
        return float("inf") if a > 0 else 0.0
    if a == 0:
        return -100.0
    ratio = a / b
    for k in (2, 3, 4, 5, 8, 10, 15, 20, 25, 50):
        if abs(ratio - k) / k < 0.01 or abs(ratio - 1 / k) * k < 0.01:
            return 0.0
    return (ratio - 1) * 100


def fetch_13f():
    if not _ua():
        print("SEC_USER_AGENT 미설정 — 13F 건너뜀")
        return None
    raw = []
    for investor, firm, cik in INVESTORS:
        try:
            sub = _get(f"https://data.sec.gov/submissions/CIK{cik}.json", as_json=True)
            rec = sub["filings"]["recent"]
            filings = [(rec["filingDate"][i], rec["accessionNumber"][i], rec["reportDate"][i])
                       for i, f in enumerate(rec["form"]) if f == "13F-HR"][:2]
            cur = _holdings(cik, filings[0][1])
            prev = _holdings(cik, filings[1][1]) if len(filings) > 1 else {}
            raw.append((investor, firm, filings[0], cur, prev))
        except Exception as e:
            print(f"  13F 실패 {investor}: {e}")
    if not raw:
        return None

    # 표시·점수에 필요한 CUSIP만 티커 매핑 (상위 10 + 신규/청산 + 비중 0.5%↑ 매수·매도 종목)
    need, prepared = set(), []
    for investor, firm, filing, cur, prev in raw:
        total, prev_total = sum(h["value"] for h in cur.values()), sum(h["value"] for h in prev.values())
        top = sorted(cur.items(), key=lambda kv: -kv[1]["value"])[:10]
        new = sorted([(c, h) for c, h in cur.items() if c not in prev and prev],
                     key=lambda kv: -kv[1]["value"])[:5]
        exited = sorted([(c, h) for c, h in prev.items() if c not in cur and h["value"] / prev_total >= 0.005],
                        key=lambda kv: -kv[1]["value"])[:5]
        bought = [c for c, h in cur.items() if prev and h["value"] / total >= GURU_MIN_WEIGHT
                  and _share_change(prev.get(c), h) >= GURU_CHANGE]
        sold = [c for c, h in prev.items() if h["value"] / prev_total >= GURU_MIN_WEIGHT
                and _share_change(h, cur.get(c)) <= -GURU_CHANGE]
        need.update([c for c, _ in top + new + exited] + bought + sold)
        prepared.append((investor, firm, filing, cur, prev, total, top, new, exited, bought, sold))
    tick = _cusip_to_ticker(need)
    label = lambda c, h: tick.get(c) or h["name"].title()

    # 서로 다른 분기를 섞지 않기: 가장 최근 분기말보다 오래된 13F 는 '자료 지연' → 가산점·공통 보유에서 제외
    latest_q = max(p[2][2] for p in prepared)
    investors, bought_by, sold_by = [], {}, {}
    for investor, firm, filing, cur, prev, total, top, new, exited, bought, sold in prepared:
        if filing[2] < latest_q:
            bought, sold = [], []
        for c in bought:
            if c in tick:
                bought_by.setdefault(tick[c], []).append(investor)
        for c in sold:
            if c in tick:
                sold_by.setdefault(tick[c], []).append(investor)
        rows = []
        for c, h in top:
            p = prev.get(c)
            if not prev:
                change, flag = None, "—"
            elif p is None:
                change, flag = "NEW", "신규"
            else:
                change = round((h["shares"] - p["shares"]) / p["shares"] * 100, 1) if p["shares"] else 0.0
                flag = "증가" if change >= 5 else "감소" if change <= -5 else "유지"
            rows.append({"ticker": label(c, h), "name": h["name"].title(),
                         "weight_pct": round(h["value"] / total * 100, 1), "change": change, "flag": flag})
        investors.append({
            "investor": investor, "firm": firm,
            "filed_date": filing[0], "report_date": filing[2],
            "stale": filing[2] < latest_q, "latest_quarter": latest_q,
            "top10": rows,
            "new_positions": [label(c, h) for c, h in new],
            "exited_positions": [label(c, h) for c, h in exited],
        })

    # 2인 이상 상위 10에 공통 등장 → 등장 횟수·합산 비중 순
    count = {}
    for inv in investors:
        if inv["stale"]:
            continue
        for r in inv["top10"]:
            cnt, w = count.get(r["ticker"], (0, 0.0))
            count[r["ticker"]] = (cnt + 1, w + r["weight_pct"])
    common = [t for t, (cnt, w) in sorted(count.items(), key=lambda kv: (-kv[1][0], -kv[1][1])) if cnt >= 2][:3]
    print(f"13F {len(investors)}명 수집 · 최근분기 매수 {len(bought_by)}종목 / 매도 {len(sold_by)}종목")
    return {"investors": investors, "common_top3": common, "bought_by": bought_by, "sold_by": sold_by}


# ── Form 4 ───────────────────────────────────────────────────────
def _cik_map():
    data = _get("https://www.sec.gov/files/company_tickers.json", as_json=True)
    return {v["ticker"].replace(".", "-"): str(v["cik_str"]).zfill(10) for v in data.values()}


def _parse_form4(xml_text):
    root = ET.fromstring(xml_text)
    issuer = _find(root, "issuer")
    owner = _find(root, "reportingOwner")
    rel = _find(owner, "reportingOwnerRelationship")
    title = _text(rel, "officerTitle")
    if not title:
        title = "이사" if (_text(rel, "isDirector") or "0") in ("1", "true") else \
                "10% 대주주" if (_text(rel, "isTenPercentOwner") or "0") in ("1", "true") else "내부자"
    plan = (_text(root, "aff10b5One") or "0") in ("1", "true")
    trades = {"P": [0.0, 0.0], "S": [0.0, 0.0]}  # [주식수, 금액]
    post_shares = None
    for tx in root.iter():
        if _local(tx.tag) != "nonDerivativeTransaction":
            continue
        code = _text(tx, "transactionCode")
        if code not in trades:
            continue
        shares = float(_text(tx, "transactionShares") or 0)
        price = float(_text(tx, "transactionPricePerShare") or 0)
        trades[code][0] += shares
        trades[code][1] += shares * price
        post = _text(tx, "sharesOwnedFollowingTransaction")
        post_shares = float(post) if post else post_shares
    return {
        "issuer_cik": (_text(issuer, "issuerCik") or "").lstrip("0"),
        "issuer_symbol": (_text(issuer, "issuerTradingSymbol") or "").upper().replace(".", "-"),
        "insider_name": _text(owner, "rptOwnerName"),
        "title": title,
        "plan_10b5_1": plan,
        "date": _text(root, "periodOfReport"),
        "buy": trades["P"], "sell": trades["S"], "post_shares": post_shares,
    }


def _merge_same_insider(rows):
    """같은 종목·같은 내부자의 여러 신고(분할 체결·공동 신고)를 1건으로 합산."""
    merged = {}
    for r in rows:
        key = (r["ticker"], r["insider_name"])
        if key not in merged:
            merged[key] = dict(r)
            continue
        m = merged[key]
        m["amount_usd"] += r["amount_usd"]
        m["date"] = max(m["date"] or "", r["date"] or "")
        if "shares" in r:
            m["shares"] += r["shares"]
        if "shares_sold_pct" in r and r["shares_sold_pct"] is not None:
            m["shares_sold_pct"] = max(m["shares_sold_pct"] or 0, r["shares_sold_pct"])
    return list(merged.values())


def fetch_insider_trades(tickers, days=14, min_buy=100_000, min_sell=1_000_000):
    if not _ua():
        print("SEC_USER_AGENT 미설정 — Form 4 건너뜀")
        return None
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    try:
        ciks = _cik_map()
    except Exception as e:
        print(f"::warning::SEC 티커-CIK 목록 수집 실패 ({e}) — Form 4 건너뜀")
        return None

    def filings_for(tk):
        cik = ciks.get(tk)
        if not cik:
            return []
        try:
            rec = _get(f"https://data.sec.gov/submissions/CIK{cik}.json", as_json=True)["filings"]["recent"]
        except Exception:
            return []
        out = []
        for i, form in enumerate(rec["form"]):
            if rec["filingDate"][i] < cutoff:
                break
            if form == "4":
                doc = rec["primaryDocument"][i].split("/")[-1]
                out.append((tk, cik, f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                                     f"{rec['accessionNumber'][i].replace('-', '')}/{doc}"))
        return out

    with ThreadPoolExecutor(max_workers=6) as ex:
        urls = [u for lst in ex.map(filings_for, tickers) for u in lst]
    print(f"  Form 4 {len(urls)}건 파싱")

    def parse(item):
        tk, cik, url = item
        try:
            f = _parse_form4(_get(url))
        except Exception:
            return tk, None
        # 회사가 '신고자'인 공시(예: Berkshire 명의의 Lennar 매수)는 제외 — 거래 대상(issuer)이 이 종목일 때만
        if f.get("issuer_cik") and f["issuer_cik"] != cik.lstrip("0"):
            return tk, None
        return tk, f

    buys, sells, seen = [], [], set()
    with ThreadPoolExecutor(max_workers=6) as ex:
        for tk, f in ex.map(parse, urls):
            if not f:
                continue
            key = (tk, f["insider_name"], f["date"], round(f["buy"][1]), round(f["sell"][1]))
            if key in seen:            # 같은 공시가 중복 수집된 경우
                continue
            seen.add(key)
            if f["buy"][1] >= min_buy:
                buys.append({"ticker": tk, "insider_name": f["insider_name"], "title": f["title"],
                             "transaction": "매수", "date": f["date"],
                             "amount_usd": round(f["buy"][1]), "shares": round(f["buy"][0])})
            if f["sell"][1] >= min_sell:
                held_before = (f["post_shares"] or 0) + f["sell"][0]
                pct = round(f["sell"][0] / held_before * 100, 1) if held_before else None
                sells.append({"ticker": tk, "insider_name": f["insider_name"], "title": f["title"],
                              "transaction": "매도", "date": f["date"], "amount_usd": round(f["sell"][1]),
                              "shares_sold_pct": pct, "plan_10b5_1": f["plan_10b5_1"]})

    buys, sells = _merge_same_insider(buys), _merge_same_insider(sells)
    for s in sells:
        pct = s["shares_sold_pct"]
        if s["plan_10b5_1"]:
            s["signal"], s["signal_type"] = "사전 계획 매도(10b5-1) — 경영 판단 신호 약함", "neutral"
        elif pct is not None and pct >= 20:
            s["signal"], s["signal_type"] = f"보유 지분의 {pct}% 재량 매도 — 주의", "bearish"
        else:
            s["signal"], s["signal_type"] = "재량 매도 (보유분 대비 소규모)", "neutral"

    # 같은 종목 복수 내부자 매수 → 강한 신호
    buyers = {}
    for b in buys:
        buyers.setdefault(b["ticker"], set()).add(b["insider_name"])
    for b in buys:
        if len(buyers[b["ticker"]]) >= 2:
            b["signal"], b["signal_type"] = f"내부자 {len(buyers[b['ticker']])}인 동시 장내 매수 — 강한 신호", "strong_bullish"
        else:
            b["signal"], b["signal_type"] = "장내 매수 (자기 돈으로 매수)", "bullish"
    buys.sort(key=lambda x: -x["amount_usd"])
    sells.sort(key=lambda x: -x["amount_usd"])
    print(f"Form 4 매수 {len(buys)}건 / 대규모 매도 {len(sells)}건")
    return {
        "period": f"{cutoff} ~ {date.today().isoformat()}",
        "buy": buys[:15],
        "sell": sells[:10],
        "buy_tickers": sorted(buyers),
    }


if __name__ == "__main__":
    check_access()
