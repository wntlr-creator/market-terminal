"""
ETF 데이터 (docs/etf/) — 국내·미국 ETF 검색, 차트, 구성종목 분석용.

만드는 파일
- docs/etf/index.json    : ETF 목록 [티커, 이름, 시장(KR/US), 종가, 등락률, 순자산(원·달러), 구성종목 있음 여부]
- docs/etf/sNN.json      : 1년치 일봉 (ETF + 국내·미국 일봉 묶음에 없는 구성종목)
- docs/etf/holdings.json : 구성종목 상위 15개 {ETF: {asOf, src, h: [[티커, 이름, 비중%]]}}
- data/etf_log.txt       : 마지막 실행 기록 (어디서 실패했는지 확인용)

출처
- 국내 ETF 목록: 네이버 금융 ETF 목록
- 국내 구성종목: KRX 정보데이터시스템(pykrx, 저장소 Secrets에 KRX_ID·KRX_PW가 있을 때) → 없으면 네이버 금융 종목 화면의 구성종목 표
- 미국 ETF: 주요 ETF 목록 + 야후 파이낸스 구성종목(yfinance)
- 일봉: 야후 파이낸스
"""
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "etf"
LOG = ROOT / "data" / "etf_log.txt"
KR_INDEX = ROOT / "docs" / "kr" / "index.json"
US_INDEX = ROOT / "docs" / "us" / "index.json"
SHARDS = 32
KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
LINES = []

# 미국 주요 ETF (지수·섹터·테마·원자재·채권·레버리지)
US_ETFS = """SPY VOO IVV VTI QQQ QQQM DIA IWM RSP VT VEA VWO EFA EEM SCHD VYM DGRO JEPI JEPQ DIVO
XLK XLF XLE XLV XLY XLP XLI XLB XLU XLRE XLC VGT SMH SOXX XSD IGV CIBR HACK SKYY CLOU WCLD
ARKK ARKW ARKG ARKQ ARKF ARKX BOTZ ROBO AIQ CHAT IRBO QTUM UFO ITA XAR PPA DRIV LIT BATT REMX
COPX URA URNM NLR SIL SILJ GDX GDXJ GLD IAU SLV PPLT PALL USO BNO UNG DBC DBA CPER WEAT CORN
TAN ICLN QCLN PBW FAN XBI IBB ARKG KWEB FXI MCHI EWY EWJ EWT INDA EWZ EWG EWU VNQ
TLT IEF SHY BIL SGOV TIP LQD HYG JNK AGG BND EMB MUB
TQQQ SQQQ SOXL SOXS UPRO SPXU TNA TZA LABU FNGU NVDL TSLL CONL BITX
IBIT FBTC ETHA BITO GBTC MSTY YMAX ULTY""".split()


def log(*a):
    msg = " ".join(str(x) for x in a)
    LINES.append(msg)
    print("[etf]", msg, file=sys.stderr, flush=True)


def get(url, enc="utf-8", headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode(enc, "replace")


def shard_of(sym):
    """화면(JS)과 같은 규칙"""
    h = 0
    for ch in sym:
        h = (h * 31 + ord(ch)) % 1000003
    return h % SHARDS


def r2(v, krw=False):
    if v is None or pd.isna(v):
        return None
    return int(round(float(v))) if krw else round(float(v), 2)


# ---------------------------------------------------------------- 목록
def kr_etf_list():
    """네이버 금융 ETF 목록 → [(코드, 이름, 순자산(억원))]"""
    url = "https://finance.naver.com/api/sise/etfItemList.nhn?etfType=0&targetColumn=market_sum&sortOrder=desc"
    j = json.loads(get(url, enc="euc-kr"))
    items = ((j.get("result") or {}).get("etfItemList")) or []
    out = [(str(x["itemcode"]).zfill(6), x.get("itemname") or "", float(x.get("marketSum") or 0)) for x in items if x.get("itemcode")]
    log(f"국내 ETF 목록 {len(out)}개 (네이버)")
    return out


# ---------------------------------------------------------------- 구성종목
def kr_holdings_pykrx(codes):
    if not (os.environ.get("KRX_ID") and os.environ.get("KRX_PW")):
        log("KRX_ID/KRX_PW 없음 → 네이버 구성종목으로")
        return {}
    try:
        from pykrx import stock
    except Exception as e:  # noqa: BLE001
        log("pykrx 불러오기 실패", e)
        return {}
    out, fails = {}, 0
    for c in codes:
        try:
            df = stock.get_etf_portfolio_deposit_file(c)
            if df is None or not len(df):
                fails += 1
                continue
            col = "비중" if "비중" in df.columns else df.columns[-1]
            df = df.sort_values(col, ascending=False).head(15)
            out[c] = [[str(t), "", round(float(w), 2)] for t, w in zip(df.index, df[col]) if pd.notna(w)]
        except Exception:  # noqa: BLE001
            fails += 1
        time.sleep(0.3)
    log(f"KRX 구성종목 {len(out)}개 ETF (실패 {fails})")
    return out


def debug_dump(code):
    """구성종목 표 위치를 찾기 위한 진단용: 화면 원본 일부를 data/에 저장"""
    for name, url, enc in (("naver_main", f"https://finance.naver.com/item/main.naver?code={code}", "euc-kr"),
                           ("wise_etf", f"https://navercomp.wisereport.co.kr/v2/ETF/index.aspx?cmp_cd={code}", "utf-8")):
        try:
            html = get(url, enc=enc)
            i = max(html.find("구성종목"), html.find("구성자산"), 0)
            (ROOT / "data" / f"etf_debug_{name}.txt").write_text(f"len={len(html)} idx={i}\n" + html[max(0, i - 3000):i + 15000], encoding="utf-8")
            log("진단 저장", name, len(html), i)
        except Exception as e:  # noqa: BLE001
            log("진단 실패", name, e)


def kr_holdings_naver(codes):
    """네이버 금융 ETF 화면의 '구성종목(구성자산)' 표 (상위 10개)"""
    if codes:
        debug_dump(codes[0])
    out, fails = {}, 0
    for c in codes:
        try:
            html = get(f"https://finance.naver.com/item/main.naver?code={c}", enc="euc-kr")
            i = html.find("구성종목")
            if i < 0:
                fails += 1
                continue
            seg = html[i:i + 12000]
            seg = seg[:seg.find("</table>") + 8] if "</table>" in seg else seg
            rows = []
            for tr in re.findall(r"<tr.*?</tr>", seg, re.S):
                m = re.search(r'code=(\d{6})[^>]*>([^<]+)</a>', tr)
                if not m:
                    continue
                text = re.sub(r"<[^>]+>", " ", tr[m.end():])
                pct = re.findall(r"(\d+(?:\.\d+)?)\s*%", text)   # 표 순서: 주식수 · 구성비중 · 현재가 · 전일비 · 등락률 → 첫 % 가 구성비중
                if pct:
                    rows.append([m.group(1), m.group(2).strip(), round(float(pct[0]), 2)])
            if rows:
                out[c] = rows[:15]
            else:
                fails += 1
        except Exception:  # noqa: BLE001
            fails += 1
        time.sleep(0.25)
    log(f"네이버 구성종목 {len(out)}개 ETF (실패 {fails})")
    return out


def us_holdings(syms):
    out, fails = {}, 0
    for s in syms:
        try:
            th = yf.Ticker(s).funds_data.top_holdings
            if th is None or not len(th):
                fails += 1
                continue
            wcol = [c for c in th.columns if "Percent" in c or "Weight" in c]
            ncol = [c for c in th.columns if "Name" in c]
            rows = []
            for sym, r in th.iterrows():
                w = float(r[wcol[0]]) if wcol else None
                if w is None or pd.isna(w):
                    continue
                rows.append([str(sym), str(r[ncol[0]]) if ncol else str(sym), round(w * 100 if w <= 1 else w, 2)])
            if rows:
                out[s] = rows[:15]
        except Exception:  # noqa: BLE001
            fails += 1
        time.sleep(0.2)
    log(f"미국 구성종목 {len(out)}개 ETF (실패 {fails})")
    return out


# ---------------------------------------------------------------- 일봉
def candles(syms, krw):
    out = {}
    for i in range(0, len(syms), 200):
        part = syms[i:i + 200]
        try:
            data = yf.download(part, period="1y", interval="1d", group_by="ticker", auto_adjust=False, progress=False, threads=True)
        except Exception as e:  # noqa: BLE001
            log("일봉 실패", e)
            continue
        for s in part:
            try:
                df = data[s] if isinstance(data.columns, pd.MultiIndex) else data
                df = df.dropna(subset=["Close"])
            except Exception:  # noqa: BLE001
                continue
            if len(df) < 5:
                continue
            k = krw(s)
            out[s] = [[d.strftime("%Y-%m-%d"), r2(r["Open"], k), r2(r["High"], k), r2(r["Low"], k), r2(r["Close"], k),
                       0 if pd.isna(r["Volume"]) else int(r["Volume"])] for d, r in df.iterrows()]
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    log("시작", datetime.now(KST).isoformat(timespec="seconds"))
    kr_idx = json.loads(KR_INDEX.read_text(encoding="utf-8")) if KR_INDEX.exists() else {"items": []}
    kr_sym = {it[0].split(".")[0]: it[0] for it in kr_idx["items"]}          # 6자리 코드 → 티커
    kr_name = {it[0]: it[1] for it in kr_idx["items"]}
    us_have = {it[0] for it in json.loads(US_INDEX.read_text(encoding="utf-8")).get("items", [])} if US_INDEX.exists() else set()

    try:
        kr_list = kr_etf_list()
    except Exception as e:  # noqa: BLE001
        log("국내 ETF 목록 실패", e)
        kr_list = []
    us_list = list(dict.fromkeys(US_ETFS))

    # 구성종목은 일주일에 한 번만 새로 받기 (비중은 자주 안 바뀜)
    hold_path = OUT / "holdings.json"
    prev_h = json.loads(hold_path.read_text(encoding="utf-8")) if hold_path.exists() else {}
    fresh = prev_h.get("_asOf", "") >= (datetime.now(KST) - timedelta(days=6)).strftime("%Y-%m-%d") and len(prev_h) > 20
    holdings = {k: v for k, v in prev_h.items() if not k.startswith("_")}
    if not fresh or os.environ.get("ETF_FORCE"):
        top_kr = [c for c, _, _ in kr_list[:300]]
        krh = kr_holdings_pykrx(top_kr) or kr_holdings_naver(top_kr)
        src = "KRX" if os.environ.get("KRX_ID") and krh else "네이버 금융"
        for c, rows in krh.items():
            fixed = []
            for code, name, w in rows:
                sym = kr_sym.get(code.zfill(6)[-6:]) if code.isdigit() else None
                fixed.append([sym or code, name or kr_name.get(sym, code), w])
            holdings[c + ".KS"] = {"asOf": datetime.now(KST).strftime("%Y-%m-%d"), "src": src, "h": fixed}
        for s, rows in us_holdings(us_list).items():
            holdings[s] = {"asOf": datetime.now(KST).strftime("%Y-%m-%d"), "src": "Yahoo Finance", "h": rows}
        holdings["_asOf"] = datetime.now(KST).strftime("%Y-%m-%d")
        hold_path.write_text(json.dumps(holdings, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    else:
        log("구성종목은 최근 것 유지", prev_h.get("_asOf"))

    # 일봉: ETF 전부 + 국내·미국 묶음에 없는 구성종목
    kr_etf_syms = [c + ".KS" for c, _, _ in kr_list]
    extra = set()
    for k, v in holdings.items():
        if k.startswith("_"):
            continue
        for sym, _, _ in v.get("h", []):
            if sym and sym not in kr_name and sym not in us_have and re.fullmatch(r"[A-Z0-9.\-=^]{1,15}", sym):
                extra.add(sym)
    kr_c = candles(kr_etf_syms, lambda s: True) if kr_etf_syms else {}
    us_c = candles(us_list + sorted(extra - set(us_list)), lambda s: s.endswith((".KS", ".KQ")))
    log(f"일봉: 국내 ETF {len(kr_c)} · 미국 ETF·구성종목 {len(us_c)}")
    if len(kr_c) + len(us_c) < 50:
        log("일봉이 너무 적어 저장하지 않음")
        LOG.write_text("\n".join(LINES), encoding="utf-8")
        return

    shards = [dict() for _ in range(SHARDS)]
    for s, rows in {**kr_c, **us_c}.items():
        shards[shard_of(s)][s] = rows
    names = {c + ".KS": n for c, n, _ in kr_list}
    aum = {c + ".KS": int(m * 1e8) for c, _, m in kr_list}
    items = []
    for s, rows in kr_c.items():
        last, prev = rows[-1][4], rows[-2][4] if len(rows) > 1 else rows[-1][4]
        items.append([s, names.get(s, s), "KR", last, round((last / prev - 1) * 100, 2) if prev else 0, aum.get(s, 0), 1 if s in holdings else 0])
    us_set = set(us_list)
    us_names = {}
    for s in us_list:
        if s in us_c:
            us_names[s] = s
    try:
        tk = yf.Tickers(" ".join(us_list))
        for s in us_list:
            try:
                info = tk.tickers[s].info or {}
                us_names[s] = info.get("shortName") or info.get("longName") or s
                aum[s] = int(info.get("totalAssets") or 0)
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        log("미국 ETF 이름 실패", e)
    for s, rows in us_c.items():
        last, prev = rows[-1][4], rows[-2][4] if len(rows) > 1 else rows[-1][4]
        kind = "US" if s in us_set else "C"   # C = 구성종목용 일봉 (ETF 아님)
        items.append([s, us_names.get(s, s), kind, last, round((last / prev - 1) * 100, 2) if prev else 0, aum.get(s, 0), 1 if s in holdings else 0])
    as_of = max(r[-1][0] for r in {**kr_c, **us_c}.values())
    (OUT / "index.json").write_text(json.dumps({"asOf": as_of, "shards": SHARDS, "items": items}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    for i, sh in enumerate(shards):
        (OUT / f"s{i:02d}.json").write_text(json.dumps(sh, separators=(",", ":")), encoding="utf-8")
    log(f"저장 완료: {len(items)}개 (기준 {as_of})")
    LOG.write_text("\n".join(LINES), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log("중단", repr(e))
        LOG.write_text("\n".join(LINES), encoding="utf-8")
        raise
