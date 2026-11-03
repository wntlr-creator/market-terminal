"""
YH 마켓 터미널 서버 (FastAPI)

  화면           /                      docs/ 폴더의 웹앱(PWA)
  시세 전체      GET  /api/market         시세판·관심종목·차트·섹터·실적·뉴스 (10분마다 백그라운드 갱신)
  종목 차트      GET  /api/candles?sym=   아무 종목이나 1년치 일봉 (10분 캐시)
  전 종목 목록   GET  /api/universe       검색용 국내·미국 상장 종목 (하루 1회 갱신)
  포트폴리오     GET  /api/portfolios     버핏·트럼프·캐시 우드·골드만삭스 (6시간마다 확인)
  AI 분석        POST /api/ai             ANTHROPIC_API_KEY 가 있을 때만 (AI_ACCESS_CODE 로 보호)
  상태           GET  /api/health

실행:  uvicorn app:app --host 0.0.0.0 --port 8000
"""
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import fetch_data as fd          # noqa: E402
import fetch_portfolios as fp    # noqa: E402
import universe as uv            # noqa: E402
import build_kr                  # noqa: E402

DOCS = ROOT / "docs"
MARKET_EVERY = int(os.environ.get("MARKET_EVERY_MIN", "10")) * 60
PORTFOLIO_EVERY = int(os.environ.get("PORTFOLIO_EVERY_MIN", "360")) * 60
AI_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
AI_CODE = os.environ.get("AI_ACCESS_CODE", "").strip()
AI_MODEL = os.environ.get("AI_MODEL", "claude-sonnet-5-5")
NO_BG = os.environ.get("NO_BACKGROUND") == "1"   # 테스트용

app = FastAPI(title="YH Market Terminal", docs_url=None, redoc_url=None)

S = {"market": None, "market_t": 0.0, "refreshing": False, "universe": None, "ai_last": 0.0}
LOCK = threading.Lock()
_cache = {}


def log(*a):
    print("[server]", time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ 백그라운드 갱신
def refresh_market():
    if S["refreshing"]:
        return
    S["refreshing"] = True
    try:
        data = fd.build_data(prev=S["market"] or fd.load_prev())
        with LOCK:
            S["market"], S["market_t"] = data, time.time()
        try:
            fd.save(data)
        except Exception as e:  # noqa: BLE001  (읽기 전용 디스크여도 서비스는 계속)
            log("시세 파일 저장 실패", e)
        log("시세 갱신 완료")
    except Exception as e:  # noqa: BLE001
        log("시세 갱신 실패", e)
    finally:
        S["refreshing"] = False


def refresh_portfolios():
    try:
        fp.main()
        log("포트폴리오 확인 완료")
    except Exception as e:  # noqa: BLE001
        log("포트폴리오 갱신 실패", e)


def refresh_universe():
    try:
        S["universe"] = uv.get_universe()
        log("전 종목 목록", S["universe"].get("count"))
    except Exception as e:  # noqa: BLE001
        log("종목 목록 실패", e)


KR_REPO = os.environ.get("KR_FROM_GITHUB", "").strip() or os.environ.get("RENDER_GIT_REPO_SLUG", "").strip()
KR_BRANCH = os.environ.get("RENDER_GIT_BRANCH", "").strip() or "main"


def pull_kr_from_github():
    """무료 서버(메모리 512MB)에서는 직접 만들지 않고, GitHub Actions가 매일 만든 파일을 받아 와요."""
    import urllib.request
    base = f"https://raw.githubusercontent.com/{KR_REPO}/{KR_BRANCH}/docs/kr/"

    def get(name):
        req = urllib.request.Request(base + name, headers={"User-Agent": "YH Market Terminal"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()

    idx = get("index.json")
    new_asof = json.loads(idx).get("asOf", "")
    out = DOCS / "kr"
    try:
        cur = json.loads((out / "index.json").read_text(encoding="utf-8")).get("asOf", "")
    except Exception:  # noqa: BLE001
        cur = ""
    if new_asof <= cur:
        log("국내 전 종목: 이미 최신", cur)
        return
    shards = json.loads(idx).get("shards", 64)
    tmp = {f"s{i:02d}.json": get(f"s{i:02d}.json") for i in range(shards)}
    try:
        tmp["sectors.json"] = get("sectors.json")   # 동일 업종 (없어도 계속)
    except Exception:  # noqa: BLE001
        pass
    out.mkdir(parents=True, exist_ok=True)
    for name, b in tmp.items():
        (out / name).write_bytes(b)
    (out / "index.json").write_bytes(idx)   # 목록은 마지막에 바꿔서 중간 상태가 보이지 않게
    log("국내 전 종목 GitHub에서 받음", new_asof)


def refresh_kr():
    try:
        if KR_REPO:
            pull_kr_from_github()
        else:
            build_kr.build()
            log("국내 전 종목 일봉 갱신 완료")
    except Exception as e:  # noqa: BLE001
        log("국내 전 종목 갱신 실패", e)


def loop(fn, every, first_delay=0):
    time.sleep(first_delay)
    while True:
        fn()
        time.sleep(every)


@app.on_event("startup")
def startup():
    S["market"] = fd.load_prev() or None
    if NO_BG:
        return
    threading.Thread(target=loop, args=(refresh_market, MARKET_EVERY), daemon=True).start()
    threading.Thread(target=loop, args=(refresh_universe, 24 * 3600, 5), daemon=True).start()
    threading.Thread(target=loop, args=(refresh_portfolios, PORTFOLIO_EVERY, 30), daemon=True).start()
    threading.Thread(target=loop, args=(refresh_kr, 6 * 3600, 60), daemon=True).start()


# ------------------------------------------------------------------ API
@app.get("/api/health")
def health():
    m = S["market"] or {}
    return {"ok": True, "ai": bool(AI_KEY), "aiCode": bool(AI_CODE), "marketUpdated": m.get("meta", {}).get("updated"),
            "refreshing": S["refreshing"], "universe": (S["universe"] or {}).get("count")}


@app.get("/api/market")
def market():
    m = S["market"]
    if not m:
        if not S["refreshing"] and not NO_BG:
            threading.Thread(target=refresh_market, daemon=True).start()
        raise HTTPException(503, "시세를 처음 받는 중이에요. 잠시 후 다시 시도해 주세요.")
    # 서버가 잠들었다 깨어나 오래된 데이터면 바로 갱신 시작
    if time.time() - S["market_t"] > MARKET_EVERY * 1.5 and not S["refreshing"] and not NO_BG:
        threading.Thread(target=refresh_market, daemon=True).start()
    body = dict(m)
    body["meta"] = {**m.get("meta", {}), "live": True, "refreshing": S["refreshing"]}
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


SYM_RE = re.compile(r"[A-Z0-9.\-=^]{1,15}")


def candles_for(sym):
    hit = _cache.get(sym)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    import yfinance as yf
    df = yf.download(sym, period="1y", interval="1d", auto_adjust=False, progress=False)
    if hasattr(df.columns, "levels") and len(df.columns.levels) > 1:
        df = df.xs(sym, axis=1, level=1) if sym in df.columns.get_level_values(1) else df.droplevel(1, axis=1)
    df = df.dropna(subset=["Close"])
    if df.empty:
        raise ValueError("시세를 찾지 못했어요. 티커를 확인해 주세요.")
    out = {"sym": sym, "candles": fd.candles_from(df), "quote": fd.quote_from(df)}
    _cache[sym] = (time.time(), out)
    if len(_cache) > 300:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
            _cache.pop(k, None)
    return out


@app.get("/api/candles")
def candles(sym: str = ""):
    sym = sym.strip().upper()
    if not SYM_RE.fullmatch(sym):
        raise HTTPException(400, "티커 형식이 올바르지 않아요")
    try:
        return candles_for(sym)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(404, str(e))


_live = {}
SPARK = "https://{host}/v8/finance/spark?symbols={syms}&range=1d&interval=5m"


def parse_spark(j):
    """야후 묶음 시세 → {티커: {price, chg, chgPct, open, high, low, date, at}}  (화면 쪽 parseSpark와 같은 규칙)"""
    out = {}
    for k, v in (j or {}).items():
        ts, cl = v.get("timestamp") or [], v.get("close") or []
        pts = [(i, c) for i, c in enumerate(cl) if c is not None]
        if not pts:
            continue
        last_i, p = pts[-1]
        vals = [c for _, c in pts]
        prev = v.get("chartPreviousClose") or v.get("previousClose")
        chg = (p - prev) if prev else None
        t = ts[last_i] if last_i < len(ts) else None
        out[k] = {"price": p, "chg": chg, "chgPct": (chg / prev * 100) if prev else None, "open": vals[0],
                  "high": max(vals), "low": min(vals),
                  "date": time.strftime("%Y-%m-%d", time.gmtime(t)) if t else None, "at": t * 1000 if t else None}
    return out


def yahoo_spark(syms):
    import urllib.parse
    import urllib.request
    q = urllib.parse.quote(",".join(syms), safe=",")
    last = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            req = urllib.request.Request(SPARK.format(host=host, syms=q), headers={"User-Agent": "Mozilla/5.0 (YH Market Terminal)"})
            with urllib.request.urlopen(req, timeout=8) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


@app.get("/api/live")
def live(syms: str = ""):
    """화면이 5초마다 부르는 실시간(무료 야후 기준) 시세. 같은 종목은 4초 동안 재사용."""
    lst = [x for x in dict.fromkeys(s.strip() for s in syms.split(",")) if SYM_RE.fullmatch(x.upper())][:80]
    if not lst:
        raise HTTPException(400, "종목이 없어요")
    now, out, need = time.time(), {}, []
    for s in lst:
        hit = _live.get(s)
        if hit and now - hit[0] < 4:
            out[s] = hit[1]
        else:
            need.append(s)
    for i in range(0, len(need), 20):
        try:
            got = parse_spark(yahoo_spark(need[i:i + 20]))
        except Exception as e:  # noqa: BLE001
            log("실시간 시세 실패", e)
            continue
        for k, q in got.items():
            _live[k] = (now, q)
            out[k] = q
    if len(_live) > 2000:
        _live.clear()
    return JSONResponse({"t": now, "quotes": out}, headers={"Cache-Control": "no-store"})


_news = {}


@app.get("/api/news")
def news(sym: str = "", name: str = ""):
    """고른 종목의 뉴스 (구글 뉴스 한국어 + 야후). 10분 캐시."""
    sym, name = sym.strip(), name.strip()[:40]
    if not SYM_RE.fullmatch(sym.upper()) or not name:
        raise HTTPException(400, "종목이 올바르지 않아요")
    hit = _news.get(sym)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    items = fd.stock_news(sym, name, 8)
    seen, out = set(), []
    for n in sorted(items, key=lambda n: n.get("time") or "", reverse=True):
        k = n["title"].strip()[:60]
        if k not in seen:
            seen.add(k)
            out.append(n)
    body = {"sym": sym, "items": out[:15]}
    _news[sym] = (time.time(), body)
    if len(_news) > 300:
        _news.clear()
    return body


@app.get("/api/universe")
def universe():
    u = S["universe"]
    if not u:
        if uv.OUT.exists():
            u = S["universe"] = json.loads(uv.OUT.read_text(encoding="utf-8"))
        else:
            raise HTTPException(503, "종목 목록을 준비 중이에요")
    return JSONResponse(u, headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/portfolios")
def portfolios():
    p = DOCS / "portfolios.json"
    if not p.exists():
        raise HTTPException(404, "포트폴리오 데이터가 없어요")
    return FileResponse(p, media_type="application/json", headers={"Cache-Control": "no-store"})


def extract_json(text):
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    raw = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    return json.loads(raw)


@app.post("/api/ai")
async def ai(request: Request, x_app_code: str = Header(default="")):
    if not AI_KEY:
        raise HTTPException(501, "서버에 ANTHROPIC_API_KEY 가 설정되지 않았어요")
    if AI_CODE and x_app_code != AI_CODE:
        raise HTTPException(401, "접속 코드가 맞지 않아요")
    if time.time() - S["ai_last"] < 8:
        raise HTTPException(429, "잠시 후 다시 시도해 주세요")
    body = await request.json()
    prompt = str(body.get("prompt", ""))
    if not prompt or len(prompt) > 80_000:
        raise HTTPException(400, "요청 내용이 비었거나 너무 길어요")
    S["ai_last"] = time.time()
    import anthropic
    client = anthropic.Anthropic(api_key=AI_KEY)
    try:
        msg = client.messages.create(model=AI_MODEL, max_tokens=3000,
                                     messages=[{"role": "user", "content": prompt + "\n\n반드시 JSON 객체 하나만 출력해."}])
        text = "".join(getattr(b, "text", "") for b in msg.content)
        return extract_json(text)
    except json.JSONDecodeError:
        raise HTTPException(502, "AI 답변을 읽지 못했어요. 다시 시도해 주세요.")
    except Exception as e:  # noqa: BLE001
        log("AI 실패", e)
        raise HTTPException(502, "AI 호출에 실패했어요")


# ------------------------------------------------------------------ 화면 (PWA)
@app.get("/sw.js")
def sw():
    return FileResponse(DOCS / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})


app.mount("/", StaticFiles(directory=str(DOCS), html=True), name="static")
