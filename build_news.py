"""종목별 뉴스 요약 재료 + 같은 업종 + 함께 보는 종목 → docs/news/

- 한국 종목: 네이버 증권 종목 뉴스(기사 앞부분 포함), 네이버 동일 업종 비교
- 미국 종목: 네이버 해외주식 뉴스(한국어 번역 기사), 부족하면 야후 파이낸스 RSS(영문 요약)
             네이버 업종 비교 + 기업 개요(한국어)
- 공통: 야후 '이 종목을 본 투자자들이 함께 본 종목'

AI 없이 만드는 데이터라 '요약'은 기사 첫 문장(리드)을 모아 보여주는 방식이에요.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from html import unescape
from pathlib import Path

ROOT = Path(__file__).parent
DOCS = ROOT / "docs"
OUT = DOCS / "news"
CODES = ROOT / "data" / "naver_world_codes.json"
KST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
SHARDS = 16
KR_TOP = int(os.environ.get("NEWS_KR_TOP", "400"))
T0 = time.time()
BUDGET = 16 * 60


def log(*a):
    print("[news]", *a, flush=True)


def get(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*", "Referer": "https://m.stock.naver.com/"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def jget(url):
    try:
        return json.loads(get(url))
    except Exception:
        return None


def clean(s, n=None):
    s = unescape(re.sub(r"<[^>]+>", " ", str(s or "")))
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^\[[^\]]{1,20}\]\s*", "", s)  # [속보] 같은 머리말
    if n and len(s) > n:
        s = s[:n].rsplit(" ", 1)[0] + "…"
    return s


def iso_from_naver(dt):
    dt = str(dt or "")
    try:
        if re.fullmatch(r"\d{12,14}", dt):
            return datetime.strptime(dt[:12], "%Y%m%d%H%M").replace(tzinfo=KST).isoformat()
        return datetime.fromisoformat(dt.replace("Z", "+00:00")).astimezone(KST).isoformat()
    except Exception:
        return ""


def shard_of(sym):
    h = 0
    for ch in sym:
        h = (h * 31 + ord(ch)) % 1000003
    return h % SHARDS


# ---------- 한국 ----------
def kr_news(code):
    j = jget(f"https://m.stock.naver.com/api/news/stock/{code}?pageSize=12&page=1")
    out = []
    for grp in j or []:
        for it in grp.get("items", []) if isinstance(grp, dict) else []:
            t = clean(it.get("titleFull") or it.get("title"))
            if not t:
                continue
            url = it.get("mobileNewsUrl") or f"https://n.news.naver.com/mnews/article/{it.get('officeId')}/{it.get('articleId')}"
            out.append([t, clean(it.get("body"), 220), it.get("officeName") or "", iso_from_naver(it.get("datetime")), url, "ko"])
    return out


def kr_info(code):
    j = jget(f"https://m.stock.naver.com/api/stock/{code}/integration") or {}
    peers = []
    for p in j.get("industryCompareInfo") or []:
        c = p.get("itemCode")
        if c and c != code:
            peers.append([f"{c}.{'KS' if str(p.get('sosok')) == '0' else 'KQ'}", p.get("stockName") or c])
    return {"pe": peers[:8]}


# ---------- 미국 ----------
def world_code(sym, codes):
    if sym in codes:
        return codes[sym]
    base = sym.replace("-", ".")
    m = re.fullmatch(r"([A-Z]+)\.([A-Z])", base)
    root = f"{m.group(1)}{m.group(2).lower()}" if m else base
    for suf in (".O", ".N", ".A", ".K", ""):
        j = jget(f"https://api.stock.naver.com/stock/{root}{suf}/basic")
        if j and j.get("reutersCode"):
            codes[sym] = j["reutersCode"]
            return codes[sym]
    codes[sym] = None
    return None


def us_info(sym, rc):
    j = jget(f"https://api.stock.naver.com/stock/{rc}/integration") or {}
    peers = []
    ici = j.get("industryCompareInfo") or {}
    for p in (ici.get("globalStocks") if isinstance(ici, dict) else ici) or []:
        s = p.get("symbolCode") or ""
        if s and s != sym and p.get("nationType") in (None, "USA"):
            peers.append([s.replace(".", "-"), p.get("stockName") or s])
    ov = clean(j.get("corporateOverview") or (j.get("summaries") or {}).get("summary"), 260)
    return {"pe": peers[:8], "ov": ov}


def us_news(sym, rc, names):
    out = []
    if rc:
        j = jget(f"https://api.stock.naver.com/news/worldStock/{rc}?pageSize=15&page=1")
        for it in j or []:
            if not isinstance(it, dict):
                continue
            t = clean(it.get("tit") or it.get("title") or it.get("titleFull"))
            b = clean(it.get("subcontent") or it.get("body"), 400)
            if not t:
                continue
            # 시장 전체 시황 기사는 빼고, 이 종목 이름이나 티커가 나오는 기사만
            hay = (t + " " + b).lower()
            if not any(n and n.lower() in hay for n in names):
                continue
            oid, aid = it.get("oid") or it.get("officeId"), it.get("aid") or it.get("articleId")
            url = it.get("mobileNewsUrl") or it.get("linkUrl") or (f"https://n.news.naver.com/mnews/article/{oid}/{aid}" if oid and aid else "")
            out.append([t, clean(b, 220), it.get("ohnm") or it.get("officeName") or "", iso_from_naver(it.get("dt") or it.get("datetime")), url, "ko"])
    if len(out) < 4:
        try:
            root = ET.fromstring(get(f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"))
            for it in root.iter("item"):
                t = clean(it.findtext("title"))
                if not t:
                    continue
                try:
                    d = datetime.strptime(it.findtext("pubDate") or "", "%a, %d %b %Y %H:%M:%S %z").astimezone(KST).isoformat()
                except Exception:
                    d = ""
                link = it.findtext("link") or ""
                src = re.sub(r"^www\.", "", urllib.parse.urlparse(link).netloc) if link else "Yahoo"
                out.append([t, clean(it.findtext("description"), 220), src, d, link, "en"])
        except Exception:
            pass
    return out


def recs(sym):
    j = jget(f"https://query2.finance.yahoo.com/v6/finance/recommendationsbysymbol/{urllib.parse.quote(sym)}") or {}
    try:
        return [r["symbol"] for r in j["finance"]["result"][0]["recommendedSymbols"]][:6]
    except Exception:
        return []


def dedup(items):
    seen, out = set(), []
    for it in sorted(items, key=lambda x: x[3] or "", reverse=True):
        k = re.sub(r"\W", "", it[0])[:40]
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out[:10]


import urllib.parse  # noqa: E402


def work(sym, kind, name, codes):
    if time.time() - T0 > BUDGET:
        return sym, None
    try:
        if kind == "KR":
            code = sym.split(".")[0]
            rec = {"n": dedup(kr_news(code)), **kr_info(code)}
        else:
            rc = world_code(sym, codes)
            info = us_info(sym, rc) if rc else {"pe": [], "ov": ""}
            short = re.split(r"[ ,.]", name or "")[0]
            names = [sym, short if len(short) >= 3 else ""]
            if rc:
                ko = jget(f"https://api.stock.naver.com/stock/{rc}/basic") or {}
                kn = (ko.get("stockName") or "").split(" ")[0]
                names.append(kn if len(kn) >= 2 else "")
            rec = {"n": dedup(us_news(sym, rc, names)), **info}
        rec["rc"] = recs(sym)
        return sym, rec
    except Exception as e:  # noqa: BLE001
        log("실패", sym, e)
        return sym, None


def main():
    kr = json.loads((DOCS / "kr" / "index.json").read_text(encoding="utf-8"))
    us = json.loads((DOCS / "us" / "index.json").read_text(encoding="utf-8"))
    data = json.loads((DOCS / "data.json").read_text(encoding="utf-8"))
    names = {}
    todo = []
    for i in sorted(kr["items"], key=lambda r: -(r[5] or 0))[:KR_TOP]:
        todo.append((i[0], "KR")); names[i[0]] = i[1]
    for i in us["items"]:
        todo.append((i[0], "US")); names[i[0]] = i[1]
    for s in data.get("candles", {}):
        if s.startswith("^") or "-USD" in s or any(s == t for t, _ in todo):
            continue
        todo.append((s, "KR" if re.search(r"\.(KS|KQ)$", s) else "US"))
    codes = json.loads(CODES.read_text(encoding="utf-8")) if CODES.exists() else {}
    prev = {}
    for f in OUT.glob("s*.json"):
        try:
            prev.update(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    log("대상", len(todo))
    res, ok = dict(prev), 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(work, s, k, names.get(s, s), codes) for s, k in todo]
        for f in as_completed(futs):
            sym, rec = f.result()
            if rec:
                res[sym] = rec; ok += 1
    keep = {s for s, _ in todo}
    res = {s: v for s, v in res.items() if s in keep}
    OUT.mkdir(parents=True, exist_ok=True)
    buckets = [{} for _ in range(SHARDS)]
    for s, v in res.items():
        buckets[shard_of(s)][s] = v
    for i, b in enumerate(buckets):
        (OUT / f"s{i:02d}.json").write_text(json.dumps(b, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (OUT / "index.json").write_text(json.dumps({"t": datetime.now(KST).isoformat(timespec="minutes"), "shards": SHARDS, "n": len(res)}, ensure_ascii=False), encoding="utf-8")
    CODES.parent.mkdir(parents=True, exist_ok=True)
    CODES.write_text(json.dumps(codes, ensure_ascii=False, sort_keys=True, indent=0), encoding="utf-8")
    withnews = sum(1 for v in res.values() if v.get("n"))
    log("완료", f"갱신 {ok}/{len(todo)}", f"뉴스 있는 종목 {withnews}", f"{time.time() - T0:.0f}s")
    if "NBIS" in res:
        log("예시 NBIS", json.dumps(res["NBIS"], ensure_ascii=False)[:1200])
    if ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
