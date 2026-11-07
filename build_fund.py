"""
기본적 분석 지표 (docs/fund.json) — 종목 추천에 쓰는 PER·ROE 등.

- 국내: 네이버 증권 종목 정보 (PER, 추정 PER, PBR, ROE=EPS÷BPS, 배당수익률) — 거래가 활발한 종목
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
    """네이버 증권(모바일) 종목 정보: PER·추정 PER·PBR·EPS·BPS·배당수익률 (거래가 활발한 종목만)"""
    from concurrent.futures import ThreadPoolExecutor
    idx = json.loads((ROOT / "docs" / "kr" / "index.json").read_text(encoding="utf-8"))
    # 추천 대상과 같은 기준: 거래대금 20억·시총 500억 이상
    syms = [it[0] for it in idx["items"] if (it[7] or 0) >= 2e9 and (it[5] or 0) >= 5e10]

    def val(t):
        return num(re.sub(r"[^0-9.\-]", "", t or "")) if t and t not in ("N/A", "-") else None

    def one(sym):
        try:
            j = json.loads(get(f"https://m.stock.naver.com/api/stock/{sym.split('.')[0]}/integration"))
            info = {x.get("code"): x.get("value") for x in j.get("totalInfos", [])}
            per, eps, bps = val(info.get("per")), val(info.get("eps")), val(info.get("bps"))
            return sym, {"per": per, "fper": val(info.get("cnsPer")), "pbr": val(info.get("pbr")),
                         "roe": round(eps / bps * 100, 1) if eps is not None and bps else None,
                         "div": val(info.get("dividendYieldRatio")), "frgn": val(info.get("foreignRate"))}
        except Exception:  # noqa: BLE001
            return sym, None

    out, fails = {}, 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for sym, rec in ex.map(one, syms):
            if rec:
                out[sym] = rec
            else:
                fails += 1
    log(f"국내 {len(out)}/{len(syms)}종목 (실패 {fails})")
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


def main():
    LOG.parent.mkdir(parents=True, exist_ok=True)
    log("시작", datetime.now(KST).isoformat(timespec="seconds"))
    prev = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    kr = {}
    try:
        kr = kr_fund()
    except Exception as e:  # noqa: BLE001
        log("국내 중단", repr(e))
    us = us_fund()
    data = {"updated": datetime.now(KST).isoformat(timespec="seconds"),
            "kr": kr if len(kr) > 100 else prev.get("kr", {}), "us": us or prev.get("us", {})}
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log("저장", len(data["kr"]), len(data["us"]))
    LOG.write_text("\n".join(LINES), encoding="utf-8")


if __name__ == "__main__":
    main()
