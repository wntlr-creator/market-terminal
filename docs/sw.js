/* YH 마켓 터미널 서비스 워커: 화면은 오프라인에서도 열리고, 시세는 항상 서버 최신값 우선 */
const VERSION = "yh-terminal-v2";
const SHELL = ["./", "index.html", "manifest.webmanifest", "icons/icon-192.png", "icons/icon-512.png", "data.json", "portfolios.json"];
const CDN = "https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js";

self.addEventListener("install", e => {
  e.waitUntil((async () => {
    const c = await caches.open(VERSION);
    await c.addAll(SHELL).catch(() => {});
    try { await c.put(CDN, await fetch(CDN, { mode: "cors" })); } catch {}
    self.skipWaiting();
  })());
});

self.addEventListener("activate", e => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (k !== VERSION) await caches.delete(k);
    await self.clients.claim();
  })());
});

async function networkFirst(req){
  const c = await caches.open(VERSION);
  try {
    const res = await fetch(req);
    if (res.ok && req.method === "GET") c.put(req, res.clone());
    return res;
  } catch {
    const hit = await c.match(req, { ignoreSearch: true });
    if (hit) return hit;
    throw new Error("offline");
  }
}

async function staleWhileRevalidate(req){
  const c = await caches.open(VERSION);
  const hit = await c.match(req);
  const net = fetch(req).then(res => { if (res.ok) c.put(req, res.clone()); return res; }).catch(() => null);
  return hit || (await net) || new Response("offline", { status: 503 });
}

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.href === CDN) return e.respondWith(staleWhileRevalidate(req));
  if (url.origin !== location.origin) return;
  if (url.pathname.includes("/api/") || /\.json$/.test(url.pathname) || req.mode === "navigate" || url.pathname.endsWith("/") || url.pathname.endsWith("index.html"))
    return e.respondWith(networkFirst(req));
  e.respondWith(staleWhileRevalidate(req));
});
