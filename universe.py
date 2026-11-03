"""
검색용 전 종목 목록 → data/universe.json  (하루 한 번 갱신)

  국내: 한국거래소 KIND 상장법인 목록 (코스피 .KS / 코스닥 .KQ, 한글 종목명)
  미국: 나스닥 트레이더 심볼 파일 (NASDAQ·NYSE·NYSE American·NYSE Arca 등 전 상장 종목, ETF 포함)

각 항목: [티커(야후 형식), 이름, 시장]   시장 = KOSPI | KOSDAQ | US | ETF
"""
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "universe.json"
UA = {"User-Agent": "Mozilla/5.0 (YH Market Terminal; personal use)"}

KIND = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13&marketType={}"
NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"


def log(*a):
    print("[universe]", *a, file=sys.stderr)


def get(url, timeout=40):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def strip_tags(s):
    return re.sub(r"<[^>]+>", "", s).replace("&amp;", "&").replace("&nbsp;", " ").strip()


def parse_kind(html, suffix, market):
    """KIND 다운로드는 EUC-KR HTML 표. 1열 회사명, 2열 종목코드."""
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S | re.I):
        tds = [strip_tags(t) for t in re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S | re.I)]
        if len(tds) < 2:
            continue
        name, code = tds[0], re.sub(r"\D", "", tds[1])
        if not name or not code:
            continue
        out.append([code.zfill(6) + suffix, name, market])
    return out


SUFFIX_RE = re.compile(r"\s*-?\s*(Common Stock|Ordinary Shares?|Class [A-Z] (Common Stock|Ordinary Shares?)|American Depositary Shares?.*|Common Shares?.*)$", re.I)


def clean_us_name(n):
    n = SUFFIX_RE.sub("", n).strip()
    return re.sub(r"\s+", " ", n)[:60]


def parse_nasdaq_listed(txt):
    """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares"""
    out = []
    lines = txt.splitlines()
    head = lines[0].split("|") if lines else []
    ix = {h: i for i, h in enumerate(head)}
    for ln in lines[1:]:
        f = ln.split("|")
        if len(f) < len(head) or ln.startswith("File Creation Time"):
            continue
        sym, name = f[ix["Symbol"]], f[ix["Security Name"]]
        if f[ix.get("Test Issue", 3)] == "Y" or not re.fullmatch(r"[A-Z]{1,5}", sym):
            continue
        if re.search(r"\b(Warrant|Right|Unit)s?\b", name, re.I):
            continue
        out.append([sym, clean_us_name(name), "ETF" if f[ix.get("ETF", 6)] == "Y" else "US"])
    return out


def parse_other_listed(txt):
    """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol"""
    out = []
    lines = txt.splitlines()
    head = lines[0].split("|") if lines else []
    ix = {h: i for i, h in enumerate(head)}
    for ln in lines[1:]:
        f = ln.split("|")
        if len(f) < len(head) or ln.startswith("File Creation Time"):
            continue
        sym, name = f[ix["ACT Symbol"]], f[ix["Security Name"]]
        if f[ix.get("Test Issue", 6)] == "Y" or "$" in sym:
            continue
        if re.search(r"\b(Warrant|Right|Unit)s?\b", name, re.I):
            continue
        ysym = sym.replace(".", "-")  # BRK.B → BRK-B (야후 형식)
        if not re.fullmatch(r"[A-Z]{1,5}(-[A-Z])?", ysym):
            continue
        out.append([ysym, clean_us_name(name), "ETF" if f[ix.get("ETF", 4)] == "Y" else "US"])
    return out


def build():
    items = []
    for mtype, suffix, market in (("stockMkt", ".KS", "KOSPI"), ("kosdaqMkt", ".KQ", "KOSDAQ")):
        try:
            html = get(KIND.format(mtype)).decode("euc-kr", "ignore")
            rows = parse_kind(html, suffix, market)
            log(market, len(rows))
            items += rows
        except Exception as e:  # noqa: BLE001
            log(market, "실패", e)
    for url, parser in ((NASDAQ_LISTED, parse_nasdaq_listed), (OTHER_LISTED, parse_other_listed)):
        try:
            rows = parser(get(url).decode("utf-8", "ignore"))
            log(url.rsplit("/", 1)[-1], len(rows))
            items += rows
        except Exception as e:  # noqa: BLE001
            log(url, "실패", e)
    seen, uniq = set(), []
    for it in items:
        if it[0] not in seen:
            seen.add(it[0])
            uniq.append(it)
    return uniq


def get_universe(max_age_h=24):
    """캐시가 하루 이내면 그대로, 아니면 새로 만든다. 실패하면 예전 캐시라도 돌려준다."""
    cached = None
    if OUT.exists():
        try:
            cached = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cached = None
    if cached and time.time() - cached.get("t", 0) < max_age_h * 3600 and cached.get("items"):
        return cached
    items = build()
    if len(items) < 500 and cached:
        log("새 목록이 너무 작아 예전 목록 유지", len(items))
        return cached
    data = {"t": int(time.time()), "count": len(items), "items": items}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return data


if __name__ == "__main__":
    d = get_universe(max_age_h=0)
    log("총", d["count"], "종목")
