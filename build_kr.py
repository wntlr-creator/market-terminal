"""
국내 전 종목(코스피·코스닥·코넥스) 1년치 일봉 → docs/kr/

데이터: FinanceData/marcap (한국거래소 전 종목 일별 시세, GitHub에 매일 자동 업데이트, 무료)
  https://github.com/FinanceData/marcap

만드는 파일
  docs/kr/index.json      검색용 목록 [티커, 종목명, 시장, 종가, 등락률, 시가총액] + 날짜 목록
  docs/kr/sNN.json        일봉 묶음 64개 (종목 코드로 나눠 담음, 고를 때만 불러옴)

실행:  python build_kr.py            (GitHub Actions에서 매일 실행)
"""
import json
import sys
import urllib.request
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "kr"
CACHE = ROOT / "data" / "marcap"
RAW = "https://raw.githubusercontent.com/FinanceData/marcap/master/data/marcap-{}.parquet"
SHARDS = 64
DAYS = 250
SUFFIX = {"KOSPI": ".KS", "KOSDAQ": ".KQ", "KOSDAQ GLOBAL": ".KQ", "KONEX": ".KN"}
MARKET = {"KOSPI": "KOSPI", "KOSDAQ": "KOSDAQ", "KOSDAQ GLOBAL": "KOSDAQ", "KONEX": "KONEX"}


def log(*a):
    print("[kr]", *a, file=sys.stderr, flush=True)


def shard_of(code):
    """JS 쪽과 같은 규칙: 문자 코드 누적 해시 % 64"""
    h = 0
    for ch in code:
        h = (h * 31 + ord(ch)) % 1000003
    return h % SHARDS


def load_year(y):
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / f"marcap-{y}.parquet"
    try:
        req = urllib.request.Request(RAW.format(y), headers={"User-Agent": "YH Market Terminal"})
        with urllib.request.urlopen(req, timeout=180) as r:
            p.write_bytes(r.read())
    except Exception as e:  # noqa: BLE001
        if not p.exists():
            log(y, "다운로드 실패", e)
            return None
        log(y, "다운로드 실패, 예전 파일 사용", e)
    return read_slim(p)


COLS = ["Code", "Name", "Close", "Open", "High", "Low", "Volume", "Amount", "ChangesRatio", "Marcap", "Market", "Date"]


def read_slim(p):
    """메모리 절약(무료 서버 512MB): 필요한 열만, 작은 자료형으로 읽기"""
    import pyarrow.parquet as pq
    since = pd.Timestamp(date.today()) - pd.Timedelta(days=400)
    import pyarrow as pa
    import pyarrow.compute as pc
    parts = []
    for b in pq.ParquetFile(p, read_dictionary=["Code", "Name", "Market"]).iter_batches(batch_size=50_000, columns=COLS):
        b = b.filter(pc.greater_equal(b.column("Date"), pa.scalar(since.to_pydatetime()).cast(b.schema.field("Date").type)))
        if b.num_rows:
            parts.append(b.to_pandas())
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=COLS)
    del parts
    for col in ("Code", "Name", "Market"):
        df[col] = df[col].astype("category")
    for col in ("Close", "Open", "High", "Low", "ChangesRatio"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float32")
    df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce").fillna(0).astype("float64")
    df["Marcap"] = pd.to_numeric(df["Marcap"], errors="coerce").fillna(0).astype("float64")
    df["Amount"] = pd.to_numeric(df["Amount"], errors="coerce").fillna(0).astype("float64")
    df["Date"] = pd.to_datetime(df["Date"])
    return df


def build(src_dir=None):
    y = date.today().year
    frames = []
    for yr in (y - 1, y):
        if src_dir:
            f = Path(src_dir) / f"marcap-{yr}.parquet"
            frames.append(read_slim(f) if f.exists() else None)
        else:
            frames.append(load_year(yr))
    frames = [f for f in frames if f is not None]
    dates = sorted(set().union(*[set(f["Date"].unique()) for f in frames]))[-DAYS:]
    frames = [f[f["Date"] >= dates[0]] for f in frames]
    df = pd.concat(frames, ignore_index=True)
    del frames
    for col in ("Code", "Name", "Market"):
        df[col] = df[col].astype(str)
    dstr = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in dates]
    di = {d: i for i, d in enumerate(dates)}
    last = dates[-1]

    # 거래 정지일(시가 0)은 종가로 채움
    for col in ("Open", "High", "Low"):
        df.loc[df[col] <= 0, col] = df.loc[df[col] <= 0, "Close"]

    index, shards = [], [dict() for _ in range(SHARDS)]
    for code, g in df.sort_values("Date").groupby("Code"):
        g = g.drop_duplicates("Date", keep="last")
        lastrow = g.iloc[-1]
        mk = str(lastrow["Market"])
        if mk not in SUFFIX or lastrow["Date"] != last:
            continue  # 상장폐지 등으로 최근 거래가 없는 종목은 제외
        sym = code + SUFFIX[mk]
        c = g["Close"].round().astype("int64").tolist()
        o = g["Open"].round().astype("int64").tolist()
        h = g["High"].round().astype("int64").tolist()
        lo = g["Low"].round().astype("int64").tolist()
        v = g["Volume"].fillna(0).round().astype("int64").tolist()
        s = di[g["Date"].iloc[0]]
        gaps = [di[d] for d in g["Date"]]
        # 압축: 종가는 전일 대비 차이, 시·고·저는 종가 대비 차이
        cd = [c[0]] + [c[i] - c[i - 1] for i in range(1, len(c))]
        rec = {"s": s, "c": cd, "o": [o[i] - c[i] for i in range(len(c))], "h": [h[i] - c[i] for i in range(len(c))],
               "l": [lo[i] - c[i] for i in range(len(c))], "v": v}
        if gaps != list(range(s, s + len(c))):
            rec["d"] = [x - s for x in gaps]  # 중간에 빠진 날이 있을 때만 날짜 위치 저장
        shards[shard_of(sym)][sym] = rec
        # 거래량 급등: 마지막 날 거래량 ÷ 직전 20거래일 평균 (거래 있던 날만)
        prev = [x for x in v[-21:-1] if x > 0]
        vr = round(v[-1] / (sum(prev) / len(prev)), 2) if prev and v[-1] > 0 else 0
        index.append([sym, str(lastrow["Name"]), MARKET[mk], int(round(lastrow["Close"])),
                      round(float(lastrow["ChangesRatio"] or 0), 2), int(lastrow["Marcap"] or 0), vr, int(lastrow["Amount"] or 0)])

    index.sort(key=lambda r: -r[5])
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "index.json").write_text(json.dumps({"asOf": dstr[-1], "dates": dstr, "shards": SHARDS, "items": index},
                                               ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    total = 0
    for i, sh in enumerate(shards):
        p = OUT / f"s{i:02d}.json"
        p.write_text(json.dumps(sh, separators=(",", ":")), encoding="utf-8")
        total += p.stat().st_size
    log(f"{len(index)}종목 · {dstr[0]} ~ {dstr[-1]} · 묶음 합계 {total // 1024 // 1024}MB")
    return index, dstr


KIND_URL = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13"
KIND_MK = {"유가": "KOSPI", "코스닥": "KOSDAQ", "코넥스": "KONEX"}


def build_sectors(index=None):
    """docs/kr/sectors.json  {"inds":[업종...], "m":{티커:[업종번호, 주요제품]}} — 동일 업종 종목 찾기에 사용"""
    import re
    try:
        req = urllib.request.Request(KIND_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            html = r.read().decode("euc-kr", errors="replace")
    except Exception as e:  # noqa: BLE001
        log("KIND 업종 목록 실패 (예전 파일 유지)", e)
        return False
    if index is None:
        index = json.loads((OUT / "index.json").read_text(encoding="utf-8"))["items"]
    by_code = {it[0].split(".")[0]: it[0] for it in index}
    strip = lambda t: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", t)).strip()
    inds, ix, m = [], {}, {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S | re.I):
        tds = [strip(t) for t in re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S | re.I)]
        if len(tds) < 5:
            continue
        code, ind, prod = tds[2].strip().zfill(6), tds[3], tds[4][:40]
        sym = by_code.get(code)
        if not sym:
            continue
        if ind not in ix:
            ix[ind] = len(inds)
            inds.append(ind)
        m[sym] = [ix[ind], prod]
    if len(m) < 1000:
        log("KIND 업종 목록이 너무 적어요 (예전 파일 유지)", len(m))
        return False
    (OUT / "sectors.json").write_text(json.dumps({"inds": inds, "m": m}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"업종 {len(inds)}개 · {len(m)}종목")
    return True


if __name__ == "__main__":
    idx, _ = build(sys.argv[1] if len(sys.argv) > 1 else None)
    build_sectors(idx)
