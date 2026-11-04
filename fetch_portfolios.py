"""
투자 대가 포트폴리오 자동 갱신 → docs/portfolios.json

  워런 버핏   : SEC 13F (Berkshire Hathaway)     — 분기 공시, 새 공시가 올라오면 자동 반영
  골드만삭스  : SEC 13F (Goldman Sachs Group)    — 분기 공시
  캐시 우드   : SEC 13F (ARK Investment Mgmt)    — 분기 공시
                + ARK ETF 일일 보유내역 CSV 비교  — 매일 매수/매도 추적
  트럼프      : 미국 정부윤리청(OGE) 278-T 거래 공시 — 새 공시 감지 + 거래내역 추출(가능한 경우)

GitHub Actions에서 매일 실행됩니다. API 키는 필요 없지만, SEC는 연락처가 들어간
User-Agent를 요구하므로 저장소 Secrets에 SEC_USER_AGENT 를 넣어 주세요.
예) "YH Market Terminal myemail@example.com"

실행:  python fetch_portfolios.py
"""
import csv
import io
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "portfolios.json"
CUSIP_CACHE = ROOT / "data" / "cusip_map.json"
ARK_LAST = ROOT / "data" / "ark_last.json"
KST = timezone(timedelta(hours=9))

SEC_UA = os.environ.get("SEC_USER_AGENT", "").strip() or "YH Market Terminal (set SEC_USER_AGENT)"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

TOP_N = {"buffett": 60, "cathie": 40, "goldman": 40}

INVESTORS_13F = [
    {"id": "buffett", "cik": "0001067983", "entity": "Berkshire Hathaway"},
    {"id": "cathie", "cik": "0001697748", "entity": "ARK Investment Management"},
    {"id": "goldman", "cik": "0000886982", "entity": "The Goldman Sachs Group"},
]

ARK_FUNDS = {
    "ARKK": "ARK_INNOVATION_ETF_ARKK_HOLDINGS.csv",
    "ARKQ": "ARK_AUTONOMOUS_TECH._&_ROBOTICS_ETF_ARKQ_HOLDINGS.csv",
    "ARKW": "ARK_NEXT_GENERATION_INTERNET_ETF_ARKW_HOLDINGS.csv",
    "ARKG": "ARK_GENOMIC_REVOLUTION_ETF_ARKG_HOLDINGS.csv",
    "ARKF": "ARK_BLOCKCHAIN_&_FINTECH_INNOVATION_ETF_ARKF_HOLDINGS.csv",
    "ARKX": "ARK_SPACE_EXPLORATION_&_INNOVATION_ETF_ARKX_HOLDINGS.csv",
}
ARK_BASE = "https://assets.ark-funds.com/fund-documents/funds-etf-csv/"
OGE_INDEX = "https://extapps2.oge.gov/201/Presiden.nsf/PAS+Index?OpenView&Count=300"


def log(*a):
    print("[portfolios]", *a, file=sys.stderr)


def http(url, ua=SEC_UA, data=None, headers=None, timeout=40):
    h = {"User-Agent": ua, "Accept-Encoding": "identity"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def next_13f_deadline(today=None):
    """분기말 + 45일 (주말이면 다음 월요일)."""
    today = today or date.today()
    for y in (today.year, today.year + 1):
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            due = date(y, m, d) + timedelta(days=45)
            while due.weekday() >= 5:
                due += timedelta(days=1)
            if due >= today:
                return due.isoformat()
    return None


# ------------------------------------------------------------------ 13F 계산 (시드 데이터와 공용)
def compare_holdings(cur, prev, total, prev_total, top_n,
                     prev_complete=True, cur_all_cusips=None):
    """
    cur / prev : [{cusip, sym, name, cls, value(달러), shares}]
    prev_complete : 직전 분기 목록이 전체인지 (아니면 '신규' 판정을 하지 않음)
    cur_all_cusips : 이번 분기 전체 CUSIP 집합 (cur 가 상위 일부만일 때 '전량 매도' 판정용)
    """
    pmap = {p["cusip"]: p for p in prev}
    cur_sorted = sorted(cur, key=lambda r: r["value"], reverse=True)
    holdings, counts = [], {"new": 0, "add": 0, "trim": 0, "hold": 0}
    for r in cur_sorted:
        p = pmap.get(r["cusip"])
        if p is None:
            change = "new" if prev_complete else None
            chg_pct = None
        else:
            chg_pct = (r["shares"] / p["shares"] - 1) * 100 if p["shares"] else None
            if chg_pct is None:
                change = "hold"
            elif chg_pct > 1:
                change = "add"
            elif chg_pct < -1:
                change = "trim"
            else:
                change = "hold"
        if change:
            counts[change] += 1
        holdings.append({
            "sym": r.get("sym") or "", "name": r["name"], "cls": r.get("cls", ""), "cusip": r["cusip"],
            "value": round(r["value"]), "shares": int(r["shares"]),
            "weight": round(r["value"] / total * 100, 3) if total else None,
            "change": change, "chgPct": round(chg_pct, 2) if chg_pct is not None else None,
            "prevShares": int(p["shares"]) if p else None,
            "prevValue": round(p["value"]) if p else None,
            "prevWeight": round(p["value"] / prev_total * 100, 3) if p and prev_total else None,
        })
    sold = []
    if cur_all_cusips is not None or len(cur) == len(cur_sorted):
        have = cur_all_cusips if cur_all_cusips is not None else {r["cusip"] for r in cur}
        for p in sorted(prev, key=lambda r: r["value"], reverse=True):
            if p["cusip"] not in have:
                sold.append({"sym": p.get("sym") or "", "name": p["name"], "cusip": p["cusip"],
                             "prevValue": round(p["value"]), "prevShares": int(p["shares"]),
                             "prevWeight": round(p["value"] / prev_total * 100, 3) if prev_total else None})
    counts["sold"] = len(sold)
    new_list = [h for h in holdings if h["change"] == "new"]
    return {
        "holdings": holdings[:top_n],
        "newBuys": new_list[:20],
        "sold": sold[:20],
        "counts": counts,
        "top10Weight": round(sum(h["value"] for h in holdings[:10]) / total * 100, 1) if total else None,
    }


# ------------------------------------------------------------------ SEC EDGAR
def latest_13f_filings(cik, n=2):
    sub = json.loads(http(f"https://data.sec.gov/submissions/CIK{cik}.json"))
    rec = sub["filings"]["recent"]
    rows = []
    for i, form in enumerate(rec["form"]):
        if form == "13F-HR":
            rows.append({"acc": rec["accessionNumber"][i], "filed": rec["filingDate"][i],
                         "period": rec["reportDate"][i]})
    rows.sort(key=lambda r: (r["period"], r["filed"]), reverse=True)
    seen, out = set(), []
    for r in rows:  # 같은 분기 중복 제거 (가장 최근 제출본)
        if r["period"] not in seen:
            seen.add(r["period"])
            out.append(r)
        if len(out) >= n:
            break
    return out


def load_info_table(cik, acc):
    cik_int = str(int(cik))
    folder = acc.replace("-", "")
    idx = json.loads(http(f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{folder}/index.json"))
    names = [it["name"] for it in idx["directory"]["item"]]
    xmls = [n for n in names if n.lower().endswith(".xml") and n.lower() != "primary_doc.xml"]
    if not xmls:
        raise RuntimeError(f"information table not found in {acc}")
    time.sleep(0.2)
    raw = http(f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{folder}/{xmls[0]}")
    root = ET.fromstring(raw)
    agg, total = {}, 0.0
    for el in root.iter():
        if not el.tag.endswith("infoTable"):
            continue
        g = {c.tag.split("}")[-1]: c for c in el.iter()}
        val = float((g.get("value").text or "0").strip()) if g.get("value") is not None else 0.0
        total += val
        if g.get("putCall") is not None and (g["putCall"].text or "").strip():
            continue  # 옵션 행은 보유 종목에서 제외 (합계에는 포함)
        cusip = (g["cusip"].text or "").strip().upper()
        shares = float((g["sshPrnamt"].text or "0").strip()) if g.get("sshPrnamt") is not None else 0.0
        a = agg.setdefault(cusip, {"cusip": cusip, "name": (g["nameOfIssuer"].text or "").strip(),
                                   "cls": (g["titleOfClass"].text or "").strip(), "value": 0.0, "shares": 0.0})
        a["value"] += val
        a["shares"] += shares
    return list(agg.values()), total


def map_cusips(cusips, cache):
    todo = [c for c in dict.fromkeys(cusips) if c and c not in cache]
    for i in range(0, len(todo), 10):
        batch = todo[i:i + 10]
        body = json.dumps([{"idType": "ID_CUSIP", "idValue": c} for c in batch]).encode()
        try:
            res = json.loads(http("https://api.openfigi.com/v3/mapping", ua=BROWSER_UA, data=body,
                                  headers={"Content-Type": "application/json"}))
        except Exception as e:  # noqa: BLE001
            log("OpenFIGI 실패", e)
            break
        for c, r in zip(batch, res):
            data = r.get("data") or []
            us = [d for d in data if d.get("exchCode") == "US"] or data
            cache[c] = (us[0].get("ticker") or "").replace("/", "-") if us else ""
        time.sleep(2.6)  # 무료 한도 25회/분
    return cache


def build_13f(inv, prev_entry, cache):
    filings = latest_13f_filings(inv["cik"], 2)
    if not filings:
        raise RuntimeError("13F filing not found")
    cur_f = filings[0]
    if prev_entry and prev_entry.get("acc") == cur_f["acc"] and prev_entry.get("source") == "sec":
        log(inv["id"], "새 공시 없음", cur_f["acc"])
        return prev_entry, False
    cur, total = load_info_table(inv["cik"], cur_f["acc"])
    prev, prev_total = ([], 0.0)
    if len(filings) > 1:
        time.sleep(0.3)
        prev, prev_total = load_info_table(inv["cik"], filings[1]["acc"])
    res = compare_holdings(cur, prev, total, prev_total, TOP_N[inv["id"]])
    need = [h["cusip"] for h in res["holdings"] + res["newBuys"] + res["sold"]]
    map_cusips(need, cache)
    for h in res["holdings"] + res["newBuys"] + res["sold"]:
        h["sym"] = cache.get(h["cusip"]) or h.get("sym") or ""
    entry = dict(prev_entry or {})
    entry.update({
        "source": "sec", "acc": cur_f["acc"], "period": cur_f["period"], "filed": cur_f["filed"],
        "prevPeriod": filings[1]["period"] if len(filings) > 1 else None,
        "totalValue": round(total), "prevTotalValue": round(prev_total), "count": len(cur),
        "secUrl": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={inv['cik']}&type=13F-HR",
        **res,
    })
    log(inv["id"], "갱신", cur_f["period"], f"{len(cur)}종목", f"신규 {res['counts']['new']}")
    return entry, True


# ------------------------------------------------------------------ ARK 일일 매매
def ark_daily(entry):
    prev_snap = json.loads(ARK_LAST.read_text(encoding="utf-8")) if ARK_LAST.exists() else {}
    snap, fund_date = {}, {}

    def _iso(d):
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", (d or "").strip())  # ARK CSV는 MM/DD/YYYY
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else None

    for fund, fname in ARK_FUNDS.items():
        try:
            raw = http(ARK_BASE + urllib.request.quote(fname), ua=BROWSER_UA).decode("utf-8-sig")
        except Exception as e:  # noqa: BLE001
            log("ARK CSV 실패", fund, e)
            continue
        for row in csv.DictReader(io.StringIO(raw)):
            tk = (row.get("ticker") or "").strip()
            if not tk or not row.get("shares"):
                continue
            d = _iso(row.get("date"))
            if d:
                fund_date[fund] = max(fund_date.get(fund, d), d)
            snap[f"{fund}|{tk}"] = {
                "fund": fund, "sym": tk, "name": (row.get("company") or "").strip(),
                "shares": float(row["shares"].replace(",", "")),
                "weight": float((row.get("weight (%)") or "0").replace("%", "") or 0),
            }
    if not snap or not fund_date:
        return entry
    # 가장 최근 날짜를 기준으로, 1주일 넘게 갱신이 멈춘 펀드 파일(예: 정리·이름 변경된 ETF)은 빼기
    as_of = max(fund_date.values())
    cut = (datetime.fromisoformat(as_of) - timedelta(days=7)).date().isoformat()
    stale = {f for f, d in fund_date.items() if d < cut}
    if stale:
        log("ARK 오래된 펀드 파일 제외", sorted(stale))
        snap = {k: v for k, v in snap.items() if v["fund"] not in stale}
    live_funds = {v["fund"] for v in snap.values()}
    trades = []
    if prev_snap and prev_snap.get("asOf") != as_of:
        old = {k: o for k, o in prev_snap.get("rows", {}).items() if o.get("fund") in live_funds}   # 같은 펀드끼리만 비교
        for k, r in snap.items():
            o = old.get(k)
            if o is None:
                trades.append({**r, "side": "new", "chgShares": r["shares"]})
            else:
                d = r["shares"] - o["shares"]
                if o["shares"] and abs(d) / o["shares"] > 0.002:
                    trades.append({**r, "side": "buy" if d > 0 else "sell", "chgShares": d})
        for k, o in old.items():
            if k not in snap:
                trades.append({**o, "side": "exit", "chgShares": -o["shares"], "shares": 0})
    ARK_LAST.parent.mkdir(parents=True, exist_ok=True)
    ARK_LAST.write_text(json.dumps({"asOf": as_of, "rows": snap}, ensure_ascii=False), encoding="utf-8")
    if trades:
        day = {"date": as_of, "trades": sorted(trades, key=lambda t: (t["side"] not in ("new", "exit"), t["fund"]))}
        daily = [d for d in entry.get("daily", []) if d["date"] != as_of]
        entry["daily"] = ([day] + daily)[:60]
        log("ARK 일일 매매", as_of, len(trades), "건")
    entry["dailyAsOf"] = as_of
    return entry


# ------------------------------------------------------------------ 트럼프 OGE 278-T
AMOUNT_RE = re.compile(r"\$[\d,]+\s*-\s*\$[\d,]+|Over \$[\d,]+")
DATE_RE = re.compile(r"(\d{2}/\d{2}/\d{4})")


def parse_278t(pdf_bytes):
    try:
        import pdfplumber
    except ImportError:
        return [], False
    trades = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    if len(text.strip()) < 200:
        return [], False  # 스캔본 (텍스트 없음)
    for line in text.splitlines():
        side = "buy" if re.search(r"\bPurchase\b", line) else "sell" if re.search(r"\bSale\b", line) else None
        amt, dt = AMOUNT_RE.search(line), DATE_RE.search(line)
        if not (side and amt and dt):
            continue
        tk = re.search(r"\(([A-Z.\-]{1,6})\)", line)
        name = line[: dt.start()].strip(" -0123456789.")[:80]
        m, d, y = dt.group(1).split("/")
        trades.append({"date": f"{y}-{m}-{d}", "side": side, "name": name,
                       "sym": tk.group(1) if tk else "", "amount": amt.group(0).replace(" ", "")})
    return trades, True


def trump_filings(entry):
    try:
        html = http(OGE_INDEX, ua=BROWSER_UA).decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        log("OGE 목록 실패", e)
        return entry
    known = {f.get("url") for f in entry.get("filings", [])}
    found = []
    for href, title in re.findall(r'href="([^"]+\.pdf)"[^>]*>([^<]*)<', html, flags=re.I):
        if "trump" not in (href + title).lower() or not re.search(r"278[-\s]?T", href + title, re.I):
            continue
        url = href if href.startswith("http") else "https://extapps2.oge.gov" + href
        if url not in known:
            found.append((url, title.strip()))
    for url, title in found[:5]:
        m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", title + url)
        filed = f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else None
        trades, parsed = [], False
        try:
            trades, parsed = parse_278t(http(url, ua=BROWSER_UA, timeout=90))
        except Exception as e:  # noqa: BLE001
            log("OGE PDF 실패", url, e)
        entry.setdefault("filings", []).insert(0, {
            "filed": filed, "title": title or "Periodic Transaction Report (278-T)", "url": url,
            "auto": True, "parsed": parsed, "count": len(trades) if parsed else None,
            "trades": trades[:300],
            "note": None if parsed else "스캔 문서라 자동으로 거래내역을 읽지 못했어요. 원문을 확인해 주세요.",
        })
        log("트럼프 새 공시", filed, url, "거래", len(trades))
    entry["filings"] = sorted(entry.get("filings", []), key=lambda f: f.get("filed") or "", reverse=True)[:12]
    return entry


# ------------------------------------------------------------------ 메인
def main():
    data = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {"investors": {}}
    inv_data = data.setdefault("investors", {})
    cache = json.loads(CUSIP_CACHE.read_text(encoding="utf-8")) if CUSIP_CACHE.exists() else {}
    changed = []
    for inv in INVESTORS_13F:
        try:
            entry, is_new = build_13f(inv, inv_data.get(inv["id"]), cache)
            inv_data[inv["id"]] = {**inv_data.get(inv["id"], {}), **entry}
            if is_new:
                changed.append(inv["id"])
        except Exception as e:  # noqa: BLE001
            log(inv["id"], "13F 실패 (이전 값 유지)", e)
        time.sleep(0.5)
    try:
        inv_data["cathie"] = ark_daily(inv_data.get("cathie", {}))
    except Exception as e:  # noqa: BLE001
        log("ARK 일일 실패", e)
    try:
        before = len(inv_data.get("trump", {}).get("filings", []))
        inv_data["trump"] = trump_filings(inv_data.get("trump", {}))
        if len(inv_data["trump"].get("filings", [])) > before:
            changed.append("trump")
    except Exception as e:  # noqa: BLE001
        log("트럼프 실패", e)
    data["meta"] = {**data.get("meta", {}),
                    "checked": datetime.now(KST).isoformat(timespec="seconds"),
                    "next13F": next_13f_deadline(),
                    "lastChanged": changed or data.get("meta", {}).get("lastChanged", [])}
    CUSIP_CACHE.parent.mkdir(parents=True, exist_ok=True)
    CUSIP_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log("저장 완료", OUT, "변경:", changed or "없음")


if __name__ == "__main__":
    main()
