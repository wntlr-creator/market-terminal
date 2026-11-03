"""
무료 데이터(야후 파이낸스, 업비트, 구글 뉴스 RSS)를 모아 docs/data.json 으로 저장합니다.
API 키가 필요 없습니다. 한 섹션이 실패해도 이전에 받아둔 값을 유지합니다.

실행:  python fetch_data.py
"""
import json
import math
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
OUT = ROOT / "docs" / "data.json"
KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (market-terminal personal dashboard)"}


def log(*a):
    print("[fetch]", *a, file=sys.stderr)


def num(x, nd=4):
    """NaN/None 을 걸러내고 반올림."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, nd)


def http_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- 가격 데이터
def download_daily(symbols):
    """1년치 일봉을 한 번에 받는다. {sym: DataFrame(Open,High,Low,Close,Volume)}"""
    symbols = sorted(set(symbols))
    data = yf.download(
        symbols, period="1y", interval="1d", group_by="ticker",
        auto_adjust=False, progress=False, threads=True,
    )
    out = {}
    for s in symbols:
        try:
            if isinstance(data.columns, pd.MultiIndex):
                lv0 = data.columns.get_level_values(0)
                df = data[s] if s in lv0 else data.xs(s, axis=1, level=1)
            else:
                df = data
            df = df.dropna(subset=["Close"])
            if len(df):
                out[s] = df
        except Exception as e:  # noqa: BLE001
            log("일봉 없음", s, e)
    return out


def quote_from(df, mult=1.0):
    closes = df["Close"].astype(float) * mult
    last = float(closes.iloc[-1])
    prev = float(closes.iloc[-2]) if len(closes) > 1 else last
    chg = last - prev
    month_ago = float(closes.iloc[-22]) if len(closes) > 22 else float(closes.iloc[0])
    return {
        "price": num(last),
        "chg": num(chg),
        "chgPct": num(chg / prev * 100 if prev else 0, 3),
        "chg1mPct": num((last / month_ago - 1) * 100 if month_ago else 0, 3),
        "high52": num(closes.max()),
        "low52": num(closes.min()),
        "spark": [num(v) for v in closes.iloc[-30:].tolist()],
        "date": df.index[-1].strftime("%Y-%m-%d"),
    }


def candles_from(df):
    rows = []
    for idx, r in df.iterrows():
        o, h, l, c = num(r["Open"]), num(r["High"]), num(r["Low"]), num(r["Close"])
        if None in (o, h, l, c):
            continue
        v = num(r.get("Volume"), 0) or 0
        rows.append([idx.strftime("%Y-%m-%d"), o, h, l, c, int(v)])
    return rows


def upbit_tickers(markets):
    if not markets:
        return {}
    url = "https://api.upbit.com/v1/ticker?markets=" + ",".join(markets)
    return {d["market"]: d for d in http_json(url)}


# ---------------------------------------------------------------- 실적 일정
def earnings_for(sym, name):
    cal = yf.Ticker(sym).calendar
    dates, eps, rev = [], None, None
    if isinstance(cal, dict):
        dates = cal.get("Earnings Date") or []
        eps = cal.get("Earnings Average")
        rev = cal.get("Revenue Average")
    elif cal is not None and hasattr(cal, "loc"):  # 구버전 DataFrame 형식
        try:
            dates = list(cal.loc["Earnings Date"].dropna())
            eps = cal.loc["Earnings Average"].iloc[0]
            rev = cal.loc["Revenue Average"].iloc[0]
        except Exception:  # noqa: BLE001
            pass
    if not isinstance(dates, (list, tuple)):
        dates = [dates]
    if not dates:
        return None
    d = pd.Timestamp(dates[0]).date()
    return {"sym": sym, "name": name, "date": d.isoformat(),
            "epsEst": num(eps, 2), "revEst": num(rev, 0)}


# ---------------------------------------------------------------- 뉴스
def ticker_news(sym, name, n):
    items = []
    for raw in (yf.Ticker(sym).news or [])[:n]:
        c = raw.get("content") or raw  # 신버전은 content 안에 들어있음
        title = c.get("title")
        link = (c.get("canonicalUrl") or {}).get("url") or c.get("link")
        src = (c.get("provider") or {}).get("displayName") or c.get("publisher")
        t = c.get("pubDate") or c.get("providerPublishTime")
        if isinstance(t, (int, float)):
            t = datetime.fromtimestamp(t, timezone.utc).isoformat()
        if title and link:
            items.append({"title": title, "link": link, "source": src or "Yahoo",
                          "time": t, "tag": name, "kind": "ticker"})
    return items


def us_earnings(names=None, days=30):
    """시가총액 상위 미국 100종목의 다음 실적 발표 시각 (야후 '시가총액 상위' 목록에 들어 있음)"""
    url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=largest_market_cap&count=100&lang=en-US&region=US"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        j = json.loads(r.read())
    now = datetime.now(timezone.utc).timestamp()
    out, seen = [], set()
    for x in ((j.get("finance") or {}).get("result") or [{}])[0].get("quotes", []):
        ts = x.get("earningsTimestamp")
        if not ts or not (now - 2 * 86400 <= ts <= now + days * 86400):
            continue
        if x.get("exchange") == "PNK" or x.get("quoteType") != "EQUITY":   # 장외(OTC) 중복 상장 제외
            continue
        key = (x.get("shortName"), ts)
        if key in seen:   # GOOG/GOOGL 같은 같은 회사 두 종류
            continue
        seen.add(key)
        et = datetime.fromtimestamp(ts, timezone(timedelta(hours=-4)))  # 미국 동부 기준 날짜 (서머타임 기준, 날짜 판정용)
        out.append({"sym": x["symbol"], "name": (names or {}).get(x["symbol"]) or x.get("shortName") or x["symbol"],
                    "date": et.date().isoformat(), "ts": int(ts * 1000), "est": bool(x.get("isEarningsDateEstimate")),
                    "mcap": x.get("marketCap")})
    return sorted(out, key=lambda e: e["ts"])


def us_movers():
    """야후 '거래 상위' 25종목 + 3개월 평균 대비 거래량 배수 (화면의 '거래량 급등 · 미국')"""
    url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=most_actives&count=25&lang=en-US&region=US"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        j = json.loads(r.read())
    items = []
    for x in ((j.get("finance") or {}).get("result") or [{}])[0].get("quotes", []):
        vol, avg = x.get("regularMarketVolume") or 0, x.get("averageDailyVolume3Month") or 0
        if not x.get("symbol") or not vol:
            continue
        items.append({"sym": x["symbol"], "name": x.get("shortName") or x.get("longName") or x["symbol"],
                      "price": x.get("regularMarketPrice"), "chgPct": x.get("regularMarketChangePercent"),
                      "vol": vol, "ratio": round(vol / avg, 2) if avg else 0})
    return {"t": int(datetime.now(timezone.utc).timestamp() * 1000), "items": items}


def stock_news(sym, name, n=5):
    """한 종목의 뉴스: 구글 뉴스(한국어 종목명) + 야후 종목 뉴스, 각 항목에 sym 표시"""
    out = []
    try:
        for it in google_news(name, n, days=3):
            it.update(kind="stock", sym=sym, tag=name)
            out.append(it)
    except Exception as e:  # noqa: BLE001
        log("종목 구글 뉴스 실패", sym, e)
    if not re.search(r"\.(KS|KQ|KN)$", sym):
        try:
            for it in ticker_news(sym, name, 3):
                it.update(kind="stock", sym=sym)
                out.append(it)
        except Exception as e:  # noqa: BLE001
            log("종목 야후 뉴스 실패", sym, e)
    return out


def google_news(keyword, n=6, days=1):
    q = urllib.parse.quote(f"{keyword} when:{days}d")
    url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        root = ET.fromstring(r.read())
    items = []
    for it in root.iter("item"):
        title = it.findtext("title") or ""
        src = it.findtext("source") or ""
        if src and title.endswith(" - " + src):
            title = title[: -len(" - " + src)]
        pub = it.findtext("pubDate")
        try:
            pub = parsedate_to_datetime(pub).isoformat() if pub else None
        except Exception:  # noqa: BLE001
            pub = None
        items.append({"title": title, "link": it.findtext("link"), "source": src,
                      "time": pub, "tag": keyword, "kind": "market"})
        if len(items) >= n:
            break
    return items


# ---------------------------------------------------------------- 메인
def load_prev():
    try:
        prev = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    except Exception:  # noqa: BLE001
        prev = {}
    return {} if prev.get("meta", {}).get("sample") else prev  # 예시 데이터는 대체값으로 쓰지 않음


def build_data(prev=None):
    """모든 데이터를 새로 받아 dict로 돌려준다 (서버와 GitHub Actions 공용)."""
    prev = load_prev() if prev is None else prev

    board_items = [i for g in CONFIG["board"] for i in g["items"]]
    syms = ([i["sym"] for i in board_items]
            + [w["sym"] for w in CONFIG["watchlist"]]
            + CONFIG.get("chart_extra", [])
            + [s["sym"] for s in CONFIG["sectors"]])

    daily = {}
    try:
        daily = download_daily(syms)
        log(f"일봉 {len(daily)}/{len(set(syms))}개 수신")
    except Exception as e:  # noqa: BLE001
        log("일봉 다운로드 실패", e)

    # 업비트 원화 시세 (김치 프리미엄 계산용)
    upbit = {}
    try:
        upbit = upbit_tickers([i["upbit"] for i in board_items if i.get("upbit")])
    except Exception as e:  # noqa: BLE001
        log("업비트 실패", e)

    usdkrw = None
    if "KRW=X" in daily:
        usdkrw = float(daily["KRW=X"]["Close"].iloc[-1])

    # --- 시세판
    prev_board = {i["sym"]: i for g in prev.get("board", []) for i in g["items"]}
    board = []
    for g in CONFIG["board"]:
        items = []
        for it in g["items"]:
            s = it["sym"]
            row = {"sym": s, "name": it["name"], "unit": it.get("unit")}
            if s in daily:
                row.update(quote_from(daily[s], it.get("mult", 1.0)))
            elif s in prev_board:
                row = prev_board[s]
            else:
                continue
            u = upbit.get(it.get("upbit", ""))
            if u:
                row["krw"] = num(u["trade_price"], 0)
                row["krwChgPct"] = num(u["signed_change_rate"] * 100, 3)
                if usdkrw and row.get("price"):
                    row["kimp"] = num((u["trade_price"] / (row["price"] * usdkrw) - 1) * 100, 2)
            items.append(row)
        board.append({"id": g["id"], "label": g["label"], "items": items})

    # --- 관심종목
    prev_watch = {w["sym"]: w for w in prev.get("watchlist", [])}
    watchlist = []
    for w in CONFIG["watchlist"]:
        s = w["sym"]
        if s not in daily:
            if s in prev_watch:
                watchlist.append(prev_watch[s])
            continue
        row = {"sym": s, "name": w["name"],
               "market": "KR" if s.endswith((".KS", ".KQ")) else "US"}
        row.update(quote_from(daily[s]))
        row["volume"] = int(num(daily[s]["Volume"].iloc[-1], 0) or 0)
        try:
            fi = yf.Ticker(s).fast_info
            row["mcap"] = num(fi.get("market_cap") if hasattr(fi, "get") else fi.market_cap, 0)
            row["currency"] = fi.get("currency") if hasattr(fi, "get") else fi.currency
        except Exception:  # noqa: BLE001
            row["currency"] = "KRW" if row["market"] == "KR" else "USD"
        watchlist.append(row)

    # --- 차트용 일봉
    candles = dict(prev.get("candles", {}))
    for s in [w["sym"] for w in CONFIG["watchlist"]] + CONFIG.get("chart_extra", []):
        if s in daily:
            candles[s] = candles_from(daily[s])

    # --- 섹터
    sectors = []
    for sec in CONFIG["sectors"]:
        if sec["sym"] in daily:
            q = quote_from(daily[sec["sym"]])
            sectors.append({"sym": sec["sym"], "name": sec["name"],
                            "chgPct": q["chgPct"], "chg1mPct": q["chg1mPct"]})
    if not sectors:
        sectors = prev.get("sectors", [])

    # --- 실적 일정 (미국 종목만, 오늘 이후)
    earnings = []
    today = datetime.now(KST).date()
    targets = [w for w in CONFIG["watchlist"] if not w["sym"].endswith((".KS", ".KQ"))]
    targets += CONFIG.get("earnings_extra", [])
    for t in targets:
        try:
            e = earnings_for(t["sym"], t["name"])
            if e and e["date"] >= (today - timedelta(days=1)).isoformat():
                earnings.append(e)
        except Exception as ex:  # noqa: BLE001
            log("실적 일정 실패", t["sym"], ex)
    # 시가총액 상위 100종목 일정으로 넓히기 (발표 시각 포함). 같은 종목은 EPS 추정치가 있는 쪽 정보와 합침
    try:
        names = {t["sym"]: t["name"] for t in targets}
        have = {e["sym"]: e for e in earnings}
        for e in us_earnings(names):
            if e["sym"] in have:
                have[e["sym"]].update({k: e[k] for k in ("ts", "est", "date")})
            else:
                earnings.append(e)
    except Exception as ex:  # noqa: BLE001
        log("실적 일정(시가총액 상위) 실패", ex)
    earnings.sort(key=lambda e: e.get("ts") or e["date"])
    if not earnings:
        earnings = prev.get("earnings", [])

    # --- 뉴스
    news = []
    for kw in CONFIG.get("news_keywords", []):
        try:
            news += google_news(kw)
        except Exception as e:  # noqa: BLE001
            log("구글 뉴스 실패", kw, e)
    for w in CONFIG["watchlist"]:
        news += stock_news(w["sym"], w["name"], CONFIG.get("news_per_ticker", 5))
    seen, uniq = set(), []
    for n in news:
        key = n["title"].strip()[:60]
        if key not in seen:
            seen.add(key)
            uniq.append(n)
    uniq.sort(key=lambda n: n.get("time") or "", reverse=True)
    news = uniq[:200] or prev.get("news", [])

    # --- 거래량 급등 (미국 거래 상위)
    try:
        movers = {"us": us_movers()}
    except Exception as e:  # noqa: BLE001
        log("미국 거래 상위 실패", e)
        movers = prev.get("movers", {})

    data = {
        "meta": {"updated": datetime.now(KST).isoformat(timespec="seconds"),
                 "sample": False,
                 "sources": ["Yahoo Finance (yfinance)", "Upbit", "Google News RSS"]},
        "board": board, "watchlist": watchlist, "candles": candles,
        "sectors": sectors, "earnings": earnings, "news": news, "movers": movers,
    }
    return data


def save(data):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"저장 완료: {OUT} ({OUT.stat().st_size // 1024} KB)")


def main():
    save(build_data())


if __name__ == "__main__":
    main()
