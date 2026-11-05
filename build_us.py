"""
미국 주요 종목 1년치 일봉 묶음 (docs/us/) — 서버 없이도 검색한 미국 종목 차트가 바로 뜨도록.

- 대상: 야후 '시가총액 상위 250' + '거래 상위 100' (장외시장 제외)
- 형식: docs/us/index.json (목록) + docs/us/sNN.json (종목별 [날짜, 시, 고, 저, 종, 거래량])
- GitHub Actions(portfolios.yml)가 하루 두 번 실행해요.
"""
import json
import sys
import urllib.request
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "us"
SHARDS = 16
UA = {"User-Agent": "Mozilla/5.0 (market-terminal personal dashboard)"}


def log(*a):
    print("[us]", *a, file=sys.stderr, flush=True)


def shard_of(sym):
    """화면(JS)과 같은 규칙: 문자 코드 누적 해시 % 16"""
    h = 0
    for ch in sym:
        h = (h * 31 + ord(ch)) % 1000003
    return h % SHARDS


def screener(scr, n):
    url = f"https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds={scr}&count={n}&lang=en-US&region=US"
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
        j = json.loads(r.read())
    return ((j.get("finance") or {}).get("result") or [{}])[0].get("quotes", [])


def r2(v):
    return None if v is None or pd.isna(v) else round(float(v), 2)


def main():
    picks = {}
    for scr, n in (("largest_market_cap", 250), ("most_actives", 100)):
        try:
            for x in screener(scr, n):
                s = x.get("symbol")
                if s and x.get("exchange") != "PNK" and x.get("quoteType") == "EQUITY" and s not in picks:
                    picks[s] = {"name": x.get("shortName") or x.get("longName") or s, "mcap": x.get("marketCap") or 0}
        except Exception as e:  # noqa: BLE001
            log("스크리너 실패", scr, e)
    if len(picks) < 50:
        log("종목 목록이 너무 적어서 이번엔 건너뜀", len(picks))
        return
    syms = sorted(picks)
    data = yf.download(syms, period="1y", interval="1d", group_by="ticker", auto_adjust=False, progress=False, threads=True)
    shards = [dict() for _ in range(SHARDS)]
    items = []
    for s in syms:
        try:
            df = data[s] if isinstance(data.columns, pd.MultiIndex) else data
            df = df.dropna(subset=["Close"])
        except Exception:  # noqa: BLE001
            continue
        if len(df) < 20:
            continue
        rows = [[i.strftime("%Y-%m-%d"), r2(r["Open"]), r2(r["High"]), r2(r["Low"]), r2(r["Close"]), int(r["Volume"] or 0) if not pd.isna(r["Volume"]) else 0]
                for i, r in df.iterrows() if not pd.isna(r["Close"])]
        shards[shard_of(s)][s] = rows
        last, prev = rows[-1][4], rows[-2][4] if len(rows) > 1 else rows[-1][4]
        items.append([s, picks[s]["name"], last, round((last / prev - 1) * 100, 2) if prev else 0, int(picks[s]["mcap"] or 0)])
    items.sort(key=lambda r: -r[4])
    OUT.mkdir(parents=True, exist_ok=True)
    as_of = max(max(v[-1][0] for v in sh.values()) for sh in shards if sh)
    (OUT / "index.json").write_text(json.dumps({"asOf": as_of, "shards": SHARDS, "items": items}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    for i, sh in enumerate(shards):
        (OUT / f"s{i:02d}.json").write_text(json.dumps(sh, separators=(",", ":")), encoding="utf-8")
    log(f"{len(items)}종목 저장 (기준 {as_of})")


if __name__ == "__main__":
    main()
