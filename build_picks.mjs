// 종목 추천 후보 (docs/picks.json)
// 앱과 똑같은 기술적 분석(TA)을 앱 코드에서 그대로 가져와 전 종목에 돌리고,
// PER·ROE(기본적 분석)와 최근 뉴스(호재·악재)를 더해 '살 만한 후보'를 고릅니다.
// GitHub Actions(portfolios.yml)가 하루 두 번 실행해요.  node build_picks.mjs
import fs from "node:fs";
import path from "node:path";

const ROOT = path.dirname(new URL(import.meta.url).pathname);
const D = p => path.join(ROOT, "docs", p);
const readJ = (p, fb = null) => { try { return JSON.parse(fs.readFileSync(D(p), "utf8")); } catch { return fb; } };
const log = (...a) => console.error("[picks]", ...a);

// ---- 앱(index.html)의 분석 코드 그대로 불러오기
const html = fs.readFileSync(D("index.html"), "utf8");
const a0 = html.indexOf("const TA = (() => {"), a1 = html.indexOf("\n})();", a0);
const TA = new Function("return " + html.slice(a0 + "const TA = ".length, a1 + 5))();
const rule = name => { const m = html.match(new RegExp(`const ${name} = (\\[[\\s\\S]*?\\]\\]);`)); return new Function("return " + m[1])(); };
const GOOD = rule("GOOD_RULES"), BAD = rule("BAD_RULES");
function tone(t){
  const g = GOOD.filter(([re]) => re.test(t)), b = BAD.filter(([re]) => re.test(t));
  return g.length > b.length ? { tone: "good", why: g[0][1] } : b.length > g.length ? { tone: "bad", why: b[0][1] } : { tone: "neutral", why: "" };
}

// ---- 일봉 불러오기
const hashShard = (sym, n) => { let h = 0; for (const ch of sym) h = (h * 31 + ch.charCodeAt(0)) % 1000003; return h % n; };
const KR = readJ("kr/index.json", { items: [], dates: [], shards: 64 }), US = readJ("us/index.json", { items: [], shards: 16 }), ETF = readJ("etf/index.json", { items: [], shards: 32 });
const cache = {};
const shard = (dir, k) => cache[dir + k] || (cache[dir + k] = readJ(`${dir}/s${String(k).padStart(2, "0")}.json`, {}));
function krRows(sym){
  const rec = shard("kr", hashShard(sym, KR.shards || 64))[sym]; if (!rec) return null;
  const out = []; let c = 0;
  rec.c.forEach((dc, i) => { c += dc; const di = rec.s + (rec.d ? rec.d[i] : i); out.push([KR.dates[di], c + rec.o[i], c + rec.h[i], c + rec.l[i], c, rec.v[i]]); });
  return out;
}
const plainRows = (dir, n) => sym => shard(dir, hashShard(sym, n))[sym] || null;
const usRows = plainRows("us", US.shards || 16), etfRows = plainRows("etf", ETF.shards || 32);

// 장중 현재가로 마지막 봉 보정
const QJ = readJ("quotes.json", {}), QS = { ...(QJ.kr?.q || {}), ...(QJ.us?.q || {}) };
function withQuote(sym, rows){
  const q = QS[sym]; if (!q || !rows?.length || q[0] == null) return rows;
  const [p, , , o, h, l, d] = q, last = rows[rows.length - 1];
  if (d > last[0]) rows.push([d, o ?? p, h ?? p, l ?? p, p, 0]);
  else if (d === last[0]) rows[rows.length - 1] = [d, last[1], Math.max(last[2], p), Math.min(last[3], p), p, last[5]];
  return rows;
}
const analyze = rows => rows?.length >= 60 ? TA.analyze(rows.map(r => ({ time: r[0], open: r[1], high: r[2], low: r[3], close: r[4], volume: r[5] }))) : null;

// ---- 기본적 분석
const FUND = readJ("fund.json", { kr: {}, us: {} }), SECT = readJ("kr/sectors.json", { inds: [], m: {} });
const median = a => { const s = a.filter(x => x != null && isFinite(x)).sort((x, y) => x - y); return s.length ? s[Math.floor(s.length / 2)] : null; };

// ---- 후보 계산
const rows = [];
for (const it of KR.items){
  const [sym, name, mk, close, chgPct, marcap, vr, amount] = it;
  if (!(amount >= 2e9) || !(marcap >= 5e10)) continue;          // 거래대금 20억·시총 500억 미만은 제외 (유동성)
  const an = analyze(withQuote(sym, krRows(sym))); if (!an) continue;
  rows.push({ sym, name, mkt: "KR", an, f: FUND.kr?.[sym] || {}, ind: SECT.m?.[sym]?.[0], amount, marcap });
}
for (const [sym, name, , , mcap] of US.items){
  const an = analyze(withQuote(sym, usRows(sym))); if (!an) continue;
  rows.push({ sym, name, mkt: "US", an, f: FUND.us?.[sym] || {}, marcap: mcap });
}
const LEV = /레버리지|인버스|선물|2X|3X|Ultra|Bull|Bear|Leveraged|Inverse/i;
for (const [sym, name, kind, , , aum] of ETF.items){
  if (kind === "C" || (kind === "KR" && !(aum >= 5e10))) continue;    // 국내는 순자산 500억 이상
  const an = analyze(withQuote(sym, etfRows(sym))); if (!an) continue;
  rows.push({ sym, name, mkt: kind === "KR" ? "ETF_KR" : "ETF_US", etf: true, lev: LEV.test(name) || /^(TQQQ|SQQQ|SOXL|SOXS|UPRO|SPXU|TNA|TZA|LABU|FNGU|NVDL|TSLL|CONL|BITX)$/.test(sym), an, f: {} });
}
log("분석", rows.length);

// 업종(국내)·시장(미국) PER 중앙값
const indPer = {};
rows.filter(r => r.mkt === "KR" && r.f.per > 0 && r.f.per < 200).forEach(r => (indPer[r.ind] = indPer[r.ind] || []).push(r.f.per));
const krMed = median(rows.filter(r => r.mkt === "KR").map(r => r.f.per > 0 && r.f.per < 200 ? r.f.per : null));
const usMed = median(rows.filter(r => r.mkt === "US").map(r => r.f.per > 0 && r.f.per < 300 ? r.f.per : null));
for (const r of rows){
  const a = r.an, close = a.close;
  r.T = a.score;
  // 가치 점수
  let v = 0; const vt = [];
  if (!r.etf){
    const per = r.f.per, base = r.mkt === "KR" ? (indPer[r.ind]?.length >= 5 ? median(indPer[r.ind]) : krMed) : usMed;
    const baseName = r.mkt === "KR" ? (indPer[r.ind]?.length >= 5 ? "같은 업종" : "국내 전체") : "미국 대형주";
    if (per == null){ vt.push("PER 자료 없음"); }
    else if (per <= 0){ v -= 1.5; vt.push("적자(PER 없음)"); }
    else if (base){
      const ratio = per / base; v += ratio < 0.6 ? 2 : ratio < 0.85 ? 1 : ratio < 1.15 ? 0 : ratio < 1.6 ? -1 : -2;
      vt.push(`PER ${per.toFixed(1)}배 (${baseName} 중앙값 ${base.toFixed(1)}배${ratio < 0.85 ? `보다 ${Math.round((1 - ratio) * 100)}% 낮음` : ratio > 1.15 ? `보다 ${Math.round((ratio - 1) * 100)}% 높음` : "와 비슷"})`);
      r.perBase = Math.round(base * 10) / 10;
    }
    if (r.mkt === "KR" && r.f.roe != null){ v += r.f.roe >= 15 ? 1.5 : r.f.roe >= 8 ? 0.5 : r.f.roe < 0 ? -1.5 : 0; vt.push(`ROE ${r.f.roe.toFixed(1)}%`); }
    if (r.mkt === "US" && r.f.fper > 0 && r.f.per > 0){ const g = r.f.per / r.f.fper - 1; if (g > 0.1){ v += 0.75; vt.push(`예상 PER ${r.f.fper.toFixed(1)}배 → 이익이 ${Math.round(g * 100)}% 늘 거란 전망`); } else if (g < -0.1){ v -= 0.75; vt.push(`예상 PER ${r.f.fper.toFixed(1)}배 → 이익 감소 전망`); } }
    if (r.mkt === "US" && r.f.div > 2) vt.push(`배당수익률 ${(+r.f.div).toFixed(1)}%`);
  }
  r.V = r.etf ? null : Math.max(0, Math.min(100, Math.round(50 + v * 12)));
  r.vt = vt;
  r.pos52 = a.high52 > a.low52 ? Math.round((close - a.low52) / (a.high52 - a.low52) * 100) : 50;
  r.trendUp = a.ma[60] != null && a.ma[120] != null && close > a.ma[120] && a.ma[60] > a.ma[120];
  r.pre = r.etf ? r.T : Math.round(0.6 * r.T + 0.4 * r.V);
}

// ---- 성향별 후보 (각 시장 10개)
const groups = { KR: r => r.mkt === "KR", US: r => r.mkt === "US", ETF: r => r.etf };
const PRESETS = {
  best: { name: "종합", test: r => r.T >= 57, key: r => r.pre },
  value: { name: "저평가 + 상승 추세", test: r => !r.etf && r.V >= 62 && r.T >= 55, key: r => r.V + r.T },
  dip: { name: "눌림목", test: r => r.trendUp && r.an.rsi >= 33 && r.an.rsi <= 50 && r.an.chg20 <= 0 && r.an.chg20 >= -15, key: r => r.pre },
  momentum: { name: "모멘텀", test: r => r.T >= 65 && r.an.rsi < 75 && r.an.chg60 > 0, key: r => r.an.chg60 },
};
const picks = {};
const need = new Set();
for (const [pk, P] of Object.entries(PRESETS)){
  picks[pk] = {};
  for (const [gk, g] of Object.entries(groups)){
    const list = rows.filter(r => g(r) && P.test(r) && !(r.an.rsi >= 80)).sort((x, y) => P.key(y) - P.key(x)).slice(0, 12);
    picks[pk][gk] = list; list.forEach(r => need.add(r));
  }
}
log("뉴스 확인 대상", need.size);

// ---- 뉴스 (구글 뉴스, 최근 7일)
async function news(r){
  const q = r.mkt === "KR" || r.mkt === "ETF_KR" ? `"${r.name}"` : `${r.sym} stock`;
  const hl = r.mkt === "KR" || r.mkt === "ETF_KR" ? "hl=ko&gl=KR&ceid=KR:ko" : "hl=en-US&gl=US&ceid=US:en";
  try {
    const res = await fetch(`https://news.google.com/rss/search?q=${encodeURIComponent(q + " when:7d")}&${hl}`, { headers: { "User-Agent": "Mozilla/5.0" }, signal: AbortSignal.timeout(15000) });
    const x = await res.text();
    return [...x.matchAll(/<item>([\s\S]*?)<\/item>/g)].slice(0, 6).map(m => {
      const g = t => (m[1].match(new RegExp(`<${t}[^>]*>([\\s\\S]*?)</${t}>`)) || [])[1] || "";
      const title = g("title").replace(/<!\[CDATA\[|\]\]>/g, "").replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/\s+-\s+[^-]+$/, "").trim();
      return { title, link: g("link").trim(), time: new Date(g("pubDate")).toISOString(), src: g("source").replace(/<[^>]+>/g, "").trim(), ...tone(title) };
    }).filter(n => n.title);
  } catch { return []; }
}
const arr = [...need];
for (let i = 0; i < arr.length; i += 6){
  await Promise.all(arr.slice(i, i + 6).map(async r => { r.news = await news(r); }));
  await new Promise(ok => setTimeout(ok, 400));
}

// ---- 최종 점수와 근거
const fmt = (v, kr) => kr ? Math.round(v).toLocaleString("en-US") + "원" : "$" + (+v).toFixed(2);
function finalize(r){
  const a = r.an, kr = r.mkt === "KR" || r.mkt === "ETF_KR";
  const ns = r.news || [], g = ns.filter(n => n.tone === "good").length, b = ns.filter(n => n.tone === "bad").length;
  r.N = ns.length ? Math.max(0, Math.min(100, 50 + (g - b) * 10)) : 50;
  r.score = Math.round(r.etf ? 0.75 * r.T + 0.25 * r.N : 0.5 * r.T + 0.3 * r.V + 0.2 * r.N);
  const best = a.checks.filter(c => c.s > 0).sort((x, y) => y.s - x.s).slice(0, 2).map(c => `${c.k} ${c.v}`);
  const pat = a.patterns.find(p => p.bias === "bull");
  const why = [`기술 ${r.T}점(${a.signal}) — ${best.join(", ") || "뚜렷한 강세 신호 없음"}${pat ? ` · ${pat.ko}${pat.confirmed ? "(확정)" : "(진행 중)"}` : ""}`];
  if (!r.etf) why.push(`가치 ${r.V}점 — ${r.vt.join(", ")}`);
  why.push(ns.length ? `뉴스 ${r.N}점 — 최근 7일 ${ns.length}건 중 호재 ${g} · 악재 ${b}${ns.find(n => n.tone === "good") ? ` (예: ${ns.find(n => n.tone === "good").why})` : ""}` : "뉴스 — 최근 7일 관련 기사 없음");
  const risk = [];
  if (a.rsi >= 70) risk.push(`RSI ${a.rsi} 과열 → 쉬었다 가는 조정 가능`);
  if (r.pos52 >= 95) risk.push("1년 최고가 근처 → 추격 매수 주의");
  if (a.chg20 >= 30) risk.push(`20일 동안 ${a.chg20.toFixed(0)}% 급등`);
  if (b > g) risk.push("악재 뉴스가 더 많음");
  if (r.f?.per != null && r.f.per <= 0) risk.push("적자 기업");
  if (r.lev) risk.push("레버리지·인버스 → 단기 매매용");
  if (r.mkt === "KR" && r.amount < 5e9) risk.push("거래대금이 적어 사고팔 때 가격이 밀릴 수 있음");
  const L = a.levels;
  return { sym: r.sym, name: r.name, mkt: r.mkt, price: a.close, chgPct: QS[r.sym]?.[2] ?? null, score: r.score, T: r.T, V: r.V, N: r.N, signal: a.signal,
    per: r.f?.per ?? null, perBase: r.perBase ?? null, roe: r.f?.roe ?? null, rsi: a.rsi, chg20: a.chg20, chg60: a.chg60, pos52: r.pos52,
    why, risk, stop: L.mode === "bull" ? L.stop : null, target: L.mode === "bull" ? L.target1 : null, stopTxt: L.mode === "bull" && L.stop ? fmt(L.stop, kr) : null,
    news: ns.slice(0, 4).map(n => ({ t: n.title, l: n.link, s: n.src, d: n.time.slice(0, 10), tone: n.tone, why: n.why })), asOf: a.asOf };
}
const out = { updated: new Date().toISOString(), method: "기술 50% + 가치 30% + 뉴스 20% (ETF는 기술 75% + 뉴스 25%)", presets: {} };
for (const [pk, P] of Object.entries(PRESETS)){
  out.presets[pk] = { name: P.name };
  for (const gk of Object.keys(groups)) out.presets[pk][gk] = picks[pk][gk].map(finalize).sort((x, y) => (pk === "momentum" ? y.chg60 - x.chg60 : y.score - x.score)).slice(0, 10);
}
out.stats = { analyzed: rows.length, kr: rows.filter(r => r.mkt === "KR").length, us: rows.filter(r => r.mkt === "US").length, etf: rows.filter(r => r.etf).length, krMedPer: krMed, usMedPer: usMed };
fs.writeFileSync(D("picks.json"), JSON.stringify(out));
log("저장", JSON.stringify(out.stats));
