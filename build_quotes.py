"""
현재가 묶음 (docs/quotes.json) — 앱 안의 모든 종목이 검색하지 않아도 자동으로 새 가격이 되도록.

- 국내: 국내 전 종목(docs/kr/index.json 목록)을 야후 묶음 시세로 한 번에 받음 (평일 장중·장 마감 직후에만)
- 미국: 시가총액 상위 + 거래 상위 종목 (야후 스크리너 한 번에 가격까지 들어 있음)
- 국내 시세는 원래 15~20분 늦게 오는 무료 시세예요.

GitHub Actions(update.yml)가 10분마다 fetch_data.py 다음에 실행해요.
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "quotes.json"
KR_INDEX = ROOT / "docs" / "kr" / "index.json"
US_INDEX = ROOT / "docs" / "us" / "index.json"
ETF_INDEX = ROOT / "docs" / "etf" / "index.json"


def etf_items():
    try:
        return json.loads(ETF_INDEX.read_text(encoding="utf-8")).get("items", [])
    except (FileNotFoundError, ValueError):
        return []
KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (market-terminal personal dashboard)"}


def log(*a):
    print("[quotes]", *a, file=sys.stderr, flush=True)


def get_json(url, tries=2):
    last = None
    for i in range(tries):
        for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
            try:
                req = urllib.request.Request(url.replace("{host}", host), headers=UA)
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                last = e
        time.sleep(1 + i)
    raise last


def r2(v, nd=2):
    return None if v is None else round(float(v), nd)


def spark_batch(syms):
    """야후 묶음 시세(20개씩): 오늘 5분봉으로 현재가·시가·고가·저가, 전일 종가 대비 등락"""
    q = urllib.parse.quote(",".join(syms), safe=",")
    j = get_json(f"https://{{host}}/v8/finance/spark?symbols={q}&range=1d&interval=5m")
    out = {}
    for k, v in (j or {}).items():
        ts, cl = v.get("timestamp") or [], v.get("close") or []
        pts = [(t, c) for t, c in zip(ts, cl) if c is not None]
        if not pts:
            continue
        prev = v.get("chartPreviousClose") or v.get("previousClose")
        p = pts[-1][1]
        vals = [c for _, c in pts]
        day = datetime.fromtimestamp(pts[-1][0], KST).strftime("%Y-%m-%d")
        chg = (p - prev) if prev else None
        # [현재가, 등락, 등락률, 시가, 고가, 저가, 날짜]
        out[k] = [r2(p), r2(chg), r2(chg / prev * 100 if prev else None, 3), r2(vals[0]), r2(max(vals)), r2(min(vals)), day]
    return out


def kr_quotes():
    idx = json.loads(KR_INDEX.read_text(encoding="utf-8"))
    syms = [it[0] for it in idx.get("items", [])] + [it[0] for it in etf_items() if it[2] == "KR"]   # 국내 ETF 포함
    groups = [syms[i:i + 20] for i in range(0, len(syms), 20)]
    out, fails = {}, 0

    def one(g):
        try:
            return spark_batch(g)
        except Exception as e:  # noqa: BLE001
            return e

    with ThreadPoolExecutor(max_workers=6) as ex:
        for res in ex.map(one, groups):
            if isinstance(res, Exception):
                fails += 1
            else:
                out.update(res)
    log(f"국내 {len(out)}/{len(syms)}종목 (실패 묶음 {fails}/{len(groups)})")
    return out


def us_quotes():
    out, names = {}, {}
    for scr, n in (("largest_market_cap", 250), ("most_actives", 100)):
        try:
            j = get_json(f"https://{{host}}/v1/finance/screener/predefined/saved?scrIds={scr}&count={n}&lang=en-US&region=US")
        except Exception as e:  # noqa: BLE001
            log("미국 스크리너 실패", scr, e)
            continue
        for x in ((j.get("finance") or {}).get("result") or [{}])[0].get("quotes", []):
            s, p = x.get("symbol"), x.get("regularMarketPrice")
            if not s or p is None or x.get("exchange") == "PNK":
                continue
            t = x.get("regularMarketTime")
            day = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d") if t else None
            out[s] = [r2(p), r2(x.get("regularMarketChange")), r2(x.get("regularMarketChangePercent"), 3),
                      r2(x.get("regularMarketOpen")), r2(x.get("regularMarketDayHigh")), r2(x.get("regularMarketDayLow")), day]
            names[s] = x.get("shortName") or x.get("longName") or s
    # 미국 일봉 묶음(docs/us)에 있는 종목 중 스크리너에 안 잡힌 건 묶음 시세로
    try:
        extra = [it[0] for it in json.loads(US_INDEX.read_text(encoding="utf-8")).get("items", []) if it[0] not in out]
        extra += [it[0] for it in etf_items() if it[2] in ("US", "C") and it[0] not in out and it[0] not in extra]   # 미국 ETF·구성종목
        for i in range(0, len(extra), 20):
            try:
                out.update(spark_batch(extra[i:i + 20]))
            except Exception as e:  # noqa: BLE001
                log("미국 묶음 시세 실패", e)
    except FileNotFoundError:
        pass
    log(f"미국 {len(out)}종목")
    return out, names


def kr_session(now):
    """평일 08:50~16:30 (장중 + 마감 직후 한 번 더)"""
    if now.weekday() >= 5:
        return False
    m = now.hour * 60 + now.minute
    return 8 * 60 + 50 <= m <= 16 * 60 + 30


def main():
    now = datetime.now(KST)
    prev = {}
    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            prev = {}
    kr = prev.get("kr") or {}
    stale = kr.get("asOf", "") < now.strftime("%Y-%m-%d") and now.hour >= 9 and now.weekday() < 5
    if kr_session(now) or not kr.get("q") or stale:
        try:
            q = kr_quotes()
            if len(q) > 100:
                kr = {"t": int(time.time() * 1000), "q": q, "asOf": max(v[6] for v in q.values())}
        except Exception as e:  # noqa: BLE001
            log("국내 실패", e)
    us = prev.get("us") or {}
    q, names = us_quotes()
    if q:
        us = {"t": int(time.time() * 1000), "q": q, "names": names, "asOf": max((v[6] or "") for v in q.values())}
    data = {"t": int(time.time() * 1000), "updated": now.isoformat(timespec="seconds"),
            "cols": ["price", "chg", "chgPct", "open", "high", "low", "date"], "kr": kr, "us": us}
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log("저장", OUT, f"{OUT.stat().st_size // 1024}KB")


if __name__ == "__main__":
    main()
