"""
기본적 분석 지표 (docs/fund.json) — 종목 추천에 쓰는 PER·ROE 등.

- 국내: 네이버 금융 '시가총액' 목록 (코스피·코스닥 전 종목의 PER, ROE, 외국인 비율)
- 미국: 야후 파이낸스 스크리너 (PER, 예상 PER, PBR, 배당수익률, EPS)
GitHub Actions(portfolios.yml)가 하루 두 번 실행해요. 실행 기록은 data/fund_log.txt.
"""
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "fund.json"
LOG = ROOT / "data" / "fund_log.txt"
KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
LINES = []


def log(*a):
    m = " ".join(str(x) for x in a)
    LINES.append(m)
    print("[fund]", m, file=sys.stderr, flush=True)


def get(url, enc="utf-8"):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
        return r.read().decode(enc, "replace")


def num(t):
    t = (t or "").replace(",", "").strip()
    try:
        return float(t)
    except ValueError:
        return None


def kr_fund():
    """네이버 금융 시가총액 목록: 표 머리글에서 PER·ROE·외국인비율 열 위치를 찾아 읽음"""
    out = {}
    for sosok, suffix in ((0, ".KS"), (1, ".KQ")):
        for page in range(1, 60):
            try:
                html = get(f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={sosok}&page={page}", "euc-kr")
            except Exception as e:  # noqa: BLE001
                log("네이버 시가총액 실패", sosok, page, e)
                break
            heads = [re.sub(r"<[^>]+>|\s", "", h) for h in re.findall(r'<th[^>]*scope="col"[^>]*>(.*?)</th>', html, re.S)]
            if page == 1 and sosok == 0:
                log("머리글", heads)
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)
            got = 0
            for tr in rows:
                m = re.search(r"code=(\d{6})", tr)
                if not m:
                    continue
                tds = [re.sub(r"<[^>]+>|\s", "", td) for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
                if len(tds) < len(heads) - 1:
                    continue
                rec = dict(zip(heads, tds))
                out[m.group(1) + suffix] = {"per": num(rec.get("PER")), "roe": num(rec.get("ROE")), "frgn": num(rec.get("외국인비율"))}
                got += 1
            if not got:
                break
            time.sleep(0.2)
    log(f"국내 {len(out)}종목")
    return out


def us_fund():
    out = {}
    for scr, n in (("largest_market_cap", 250), ("most_actives", 100)):
        try:
            j = json.loads(get(f"https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds={scr}&count={n}&lang=en-US&region=US"))
        except Exception as e:  # noqa: BLE001
            log("야후 스크리너 실패", scr, e)
            continue
        for x in ((j.get("finance") or {}).get("result") or [{}])[0].get("quotes", []):
            s = x.get("symbol")
            if not s or s in out:
                continue
            out[s] = {"per": x.get("trailingPE"), "fper": x.get("forwardPE"), "pbr": x.get("priceToBook"),
                      "div": x.get("dividendYield"), "eps": x.get("epsTrailingTwelveMonths"),
                      "epsF": x.get("epsForward"), "mcap": x.get("marketCap")}
    log(f"미국 {len(out)}종목")
    return out


def debug_kr():
    """국내 PER 출처 진단: 화면 원본·API 응답 일부를 data/에 저장"""
    tries = [("sise", "https://finance.naver.com/sise/sise_market_sum.naver?sosok=0&page=1", "euc-kr"),
             ("m_value", "https://m.stock.naver.com/api/stocks/marketValue/KOSPI?page=1&pageSize=5", "utf-8"),
             ("m_integ", "https://m.stock.naver.com/api/stock/005930/integration", "utf-8"),
             ("m_basic", "https://m.stock.naver.com/api/stock/005930/basic", "utf-8"),
             ("m_finance", "https://m.stock.naver.com/api/stock/005930/finance/annual", "utf-8")]
    for name, url, enc in tries:
        try:
            t = get(url, enc)
            i = max(t.find("PER"), 0)
            (ROOT / "data" / f"fund_debug_{name}.txt").write_text(f"len={len(t)} idx={i}\n" + t[:6000] + "\n....\n" + t[max(0, i - 1500):i + 3000], encoding="utf-8")
            log("진단", name, len(t), i)
        except Exception as e:  # noqa: BLE001
            log("진단 실패", name, e)


def main():
    LOG.parent.mkdir(parents=True, exist_ok=True)
    log("시작", datetime.now(KST).isoformat(timespec="seconds"))
    prev = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    kr = {}
    debug_kr()
    try:
        kr = kr_fund()
    except Exception as e:  # noqa: BLE001
        log("국내 중단", repr(e))
    us = us_fund()
    data = {"updated": datetime.now(KST).isoformat(timespec="seconds"),
            "kr": kr if len(kr) > 500 else prev.get("kr", {}), "us": us or prev.get("us", {})}
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log("저장", len(data["kr"]), len(data["us"]))
    LOG.write_text("\n".join(LINES), encoding="utf-8")


if __name__ == "__main__":
    main()
