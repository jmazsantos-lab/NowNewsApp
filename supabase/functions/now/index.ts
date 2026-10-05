// Now · función de servidor (Supabase Edge Function, sin base de datos)
//
//   POST …/now/topics   Ajustes de la app: buscar fuentes con IA, añadir y borrar temas
//                       y registrar dispositivos para las notificaciones.
//                       Requiere la clave de administración (NOW_ADMIN_KEY).
//   POST …/now/mcp      Conector MCP para Claude (solo lectura de noticias).
//
// Secrets (Supabase → Edge Functions → Secrets):
//   NOW_SITE_URL       URL pública de la app, p. ej. https://usuario.github.io/now   (obligatorio)
//   NOW_GITHUB_REPO    Repositorio con topics.json, p. ej. usuario/now                (para los ajustes)
//   NOW_GITHUB_TOKEN   Token de GitHub sobre ese repositorio con permisos de lectura y escritura en
//                      Contents (temas y pesos), Variables (notificaciones) y Actions (botón ↻)
//   NOW_ADMIN_KEY      Clave que pedirá la app para cambiar temas (elígela tú)
//   ANTHROPIC_API_KEY  Clave de la API de Claude (para buscar fuentes)
//   NOW_TIMEZONE       Opcional. Zona horaria de las fechas. Por defecto America/Havana
//   NOW_AI_MODEL       Opcional. Por defecto claude-sonnet-5-5
//   NOW_GITHUB_BRANCH  Opcional. Por defecto main
//   NOW_WORKFLOW_FILE  Opcional. Workflow que actualiza las noticias. Por defecto update.yml
//
// Despliegue:  supabase functions deploy now --no-verify-jwt

const env = (k: string, d = "") => (Deno.env.get(k) ?? d).trim();
const SITE = env("NOW_SITE_URL").replace(/\/$/, "");
const REPO = env("NOW_GITHUB_REPO");
const BRANCH = env("NOW_GITHUB_BRANCH", "main");
const GH_TOKEN = env("NOW_GITHUB_TOKEN");
const ADMIN_KEY = env("NOW_ADMIN_KEY");
const AI_KEY = env("ANTHROPIC_API_KEY");
const AI_MODEL = env("NOW_AI_MODEL", "claude-sonnet-5-5");
const TZ = env("NOW_TIMEZONE", "America/Havana");
const H = 3600e3, D = 24 * H;
const AI_URL = env("NOW_ANTHROPIC_URL", "https://api.anthropic.com");   // solo para pruebas
const GH_API = env("NOW_GITHUB_API", "https://api.github.com");
const WORKFLOW = env("NOW_WORKFLOW_FILE", "update.yml");
const PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"];
const UA = "Mozilla/5.0 (compatible; NowNews/1.0; +https://github.com)";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "content-type, authorization, x-now-key, mcp-session-id, mcp-protocol-version, accept",
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { ...CORS, "Content-Type": "application/json; charset=utf-8" } });
const norm = (s: string) => s.toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").trim();
const slug = (s: string) => norm(s).replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 48) || "tema";

// ════════════════════════════════════════════════════════════════════════════
//  1 · TEMAS (ajustes de la app)
// ════════════════════════════════════════════════════════════════════════════

type Query = { q: string; lang: string; country: string };
type Topic = { id: string; name: string; short: string; icon: string; color: [string, string]; feeds: string[]; queries: (Query | string)[] };

const PALETTE: [string, string][] = [
  ["#e7b45f", "#7c4d12"], ["#6ba3d0", "#274d6e"], ["#a882d8", "#563a86"], ["#7fb277", "#2f5a2c"], ["#5cc2b5", "#1c6b62"],
  ["#e0a05a", "#8a4a15"], ["#8f8be6", "#2d2b8a"], ["#d07a5f", "#7a2415"], ["#5fc6bc", "#0d5b53"], ["#bd8ae8", "#5a1f8f"],
  ["#66b6e0", "#0a5a86"], ["#e8a06a", "#8a3a12"], ["#e0ad63", "#7a3a08"], ["#aebf5f", "#3f4a08"], ["#8b87e0", "#2b2585"],
  ["#5cbfb4", "#0c534d"], ["#e089b0", "#7a1240"], ["#d0905f", "#6a2a15"], ["#7aa6c2", "#2c4a5e"], ["#c9a24d", "#5e4612"],
  ["#9fc27a", "#3c5a1f"], ["#d27fa0", "#6a1f40"], ["#8fb3d9", "#2f4f7a"], ["#c48a6a", "#5a2f1a"],
];

function safeEqual(a: string, b: string): boolean {
  if (!a || !b || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// ─── GitHub: leer y guardar topics.json ─────────────────────────────────────
function b64decode(s: string): string {
  const bin = atob(s.replace(/\s/g, ""));
  return new TextDecoder().decode(Uint8Array.from(bin, c => c.charCodeAt(0)));
}
function b64encode(s: string): string {
  const bytes = new TextEncoder().encode(s);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}
const ghHeaders = () => ({
  Authorization: `Bearer ${GH_TOKEN}`, Accept: "application/vnd.github+json",
  "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "now-app",
});

async function readFile(path: string): Promise<{ text: string; sha: string }> {
  const res = await fetch(`${GH_API}/repos/${REPO}/contents/${path}?ref=${encodeURIComponent(BRANCH)}`, { headers: ghHeaders() });
  if (!res.ok) throw new Error(`GitHub no devolvió ${path} (${res.status}). Revisa NOW_GITHUB_REPO y NOW_GITHUB_TOKEN.`);
  const file = await res.json();
  return { text: b64decode(file.content), sha: file.sha };
}
async function writeFile(path: string, text: string, sha: string, message: string): Promise<void> {
  const res = await fetch(`${GH_API}/repos/${REPO}/contents/${path}`, {
    method: "PUT", headers: { ...ghHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify({ message, content: b64encode(text), sha, branch: BRANCH }),
  });
  if (res.status === 409) throw new Error("Otro cambio se guardó a la vez. Vuelve a intentarlo.");
  if (!res.ok) throw new Error(`GitHub no aceptó el cambio (${res.status}). El token necesita «Contents: Read and write».`);
}
async function readTopics(): Promise<{ topics: Topic[]; sha: string }> {
  const f = await readFile("topics.json");
  return { topics: JSON.parse(f.text).topics as Topic[], sha: f.sha };
}
const writeTopics = (topics: Topic[], sha: string, message: string) =>
  writeFile("topics.json", JSON.stringify({ topics }, null, 1) + "\n", sha, message);

// ─── Actualizar ahora (botón ↻ de la app) ───────────────────────────────────
const NEEDS_ACTIONS = "El token de GitHub necesita el permiso «Actions: Read and write» sobre el repositorio.";
async function runsWith(status: string): Promise<number> {
  const res = await fetch(`${GH_API}/repos/${REPO}/actions/workflows/${WORKFLOW}/runs?status=${status}&per_page=1`, { headers: ghHeaders() });
  if (!res.ok) throw new Error(`GitHub no deja consultar las actualizaciones (${res.status}). ${NEEDS_ACTIONS}`);
  return Number((await res.json()).total_count ?? 0);
}
async function refreshNow(): Promise<{ started: boolean; running: boolean }> {
  if ((await runsWith("in_progress")) + (await runsWith("queued")) > 0) return { started: false, running: true };
  const res = await fetch(`${GH_API}/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
    method: "POST", headers: { ...ghHeaders(), "Content-Type": "application/json" }, body: JSON.stringify({ ref: BRANCH }),
  });
  if (!res.ok) throw new Error(`GitHub no aceptó la actualización (${res.status}). ${NEEDS_ACTIONS}`);
  return { started: true, running: false };
}

// ─── Pesos del índice (pantalla «Cómo se calcula») ──────────────────────────
const WEIGHT_KEYS = ["cobertura", "impulso", "alcance", "duracion", "recencia", "preferencias"];
async function setWeights(raw: Record<string, unknown>): Promise<Record<string, number>> {
  const w = Object.fromEntries(WEIGHT_KEYS.map(k => [k, Math.max(0, Math.min(1, Number(raw?.[k]) || 0))]));
  const total = Object.values(w).reduce((a, b) => a + b, 0);
  if (!(total > 0)) throw new Error("Los pesos no pueden ser todos cero.");
  for (const k of WEIGHT_KEYS) w[k] = Math.round((w[k] / total) * 1000) / 1000;
  const f = await readFile("now_config.json");
  const cfg = JSON.parse(f.text);
  cfg.index = { ...(cfg.index ?? {}), weights: w };
  await writeFile("now_config.json", JSON.stringify(cfg, null, 2) + "\n", f.sha, "Ajustar pesos del índice de importancia");
  return w;
}

// ─── Notificaciones push ────────────────────────────────────────────────────
// Los dispositivos suscritos y las claves VAPID viven en variables del repositorio
// (NOW_PUSH_SUBS, NOW_PUSH_KEYS); el workflow las lee y envía los avisos.
const VAR_KEYS = "NOW_PUSH_KEYS", VAR_SUBS = "NOW_PUSH_SUBS", MAX_DEVICES = 20;
const u64 = (b: ArrayBuffer | Uint8Array) => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const fromU64 = (t: string) => Uint8Array.from(atob(t.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(t.length / 4) * 4, "=")), c => c.charCodeAt(0));
const VARS = () => `${GH_API}/repos/${REPO}/actions/variables`;
const NEEDS_VARS = "El token de GitHub necesita el permiso «Variables: Read and write» sobre el repositorio.";

async function getVar(name: string): Promise<string | null> {
  const res = await fetch(`${VARS()}/${name}`, { headers: ghHeaders() });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`No se pudo leer la variable ${name} (${res.status}). ${NEEDS_VARS}`);
  return String((await res.json()).value ?? "");
}
async function setVar(name: string, value: string): Promise<void> {
  const h = { ...ghHeaders(), "Content-Type": "application/json" };
  let res = await fetch(`${VARS()}/${name}`, { method: "PATCH", headers: h, body: JSON.stringify({ name, value }) });
  if (res.status === 404) res = await fetch(VARS(), { method: "POST", headers: h, body: JSON.stringify({ name, value }) });
  if (!res.ok) throw new Error(`No se pudo guardar la variable ${name} (${res.status}). ${NEEDS_VARS}`);
}
async function pushKeys(): Promise<{ pub: string; priv: string }> {
  const cur = await getVar(VAR_KEYS);
  if (cur) { try { const k = JSON.parse(cur); if (k.pub && k.priv) return k; } catch { /* se vuelve a crear */ } }
  const kp = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, ["sign", "verify"]);
  const jwk = await crypto.subtle.exportKey("jwk", kp.privateKey);
  const raw = new Uint8Array(65); raw[0] = 4; raw.set(fromU64(jwk.x!), 1); raw.set(fromU64(jwk.y!), 33);
  const keys = { pub: u64(raw), priv: jwk.d! };
  await setVar(VAR_KEYS, JSON.stringify(keys));
  await setVar(VAR_SUBS, "[]").catch(() => {});
  return keys;
}
type Device = { endpoint: string; keys: { p256dh: string; auth: string }; label: string; at: number };
async function getDevices(): Promise<Device[]> {
  try { const v = JSON.parse((await getVar(VAR_SUBS)) || "[]"); return Array.isArray(v) ? v : []; } catch { return []; }
}

const summary = (t: Topic) => ({ id: t.id, name: t.name, short: t.short, icon: t.icon, color: t.color, sources: t.feeds.length, queries: t.queries.length });

// ─── IA: proponer fuentes para un tema ──────────────────────────────────────
function prompt(topic: string): string {
  return `Quiero seguir las noticias del tema «${topic}» en un lector de noticias que funciona con feeds RSS.

Encuentra entre 12 y 20 fuentes de noticias, las más relevantes y fiables para este tema, que publiquen con frecuencia:
- medios de referencia del ámbito, publicaciones especializadas y agencias;
- incluye las fuentes más importantes en el idioma propio del tema y, si existen y son buenas, también en español y en inglés;
- de cada fuente necesito la URL exacta de su feed RSS o Atom. Si no la sabes con seguridad, búscala (muchos medios la tienen en /feed, /rss, /rss.xml o en una página de «RSS»). Si un medio tiene feeds por secciones, elige el de la sección que mejor encaja con el tema;
- si no encuentras el feed de una fuente muy importante, inclúyela igualmente con feed vacío y la web principal: intentaré descubrirlo.

Propón además entre 6 y 10 búsquedas cortas para Google News que cubran el tema, cada una con su idioma (lang, ISO 639-1) y país (country, ISO 3166-1 alfa-2).

Responde SOLO con un bloque JSON, sin texto antes ni después, con esta forma exacta:
{"name":"nombre del tema en español, corto y claro","short":"máx. 10 caracteres para la barra","icon":"un solo emoji",
 "sources":[{"name":"Nombre del medio","site":"https://…","feed":"https://…"}],
 "queries":[{"q":"búsqueda","lang":"fr","country":"FR"}]}`;
}

type AiProposal = { name?: string; short?: string; icon?: string; sources?: { name?: string; site?: string; feed?: string }[]; queries?: Query[] };

async function askClaude(topic: string): Promise<AiProposal> {
  if (!AI_KEY) throw new Error("Falta ANTHROPIC_API_KEY en los secrets de la función.");
  // deno-lint-ignore no-explicit-any
  const messages: any[] = [{ role: "user", content: prompt(topic) }];
  let text = "";
  for (let turn = 0; turn < 4; turn++) {
    const res = await fetch(`${AI_URL}/v1/messages`, {
      method: "POST",
      headers: { "x-api-key": AI_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json" },
      body: JSON.stringify({
        model: AI_MODEL, max_tokens: 6000, messages,
        tools: [{ type: "web_search_20250305", name: "web_search", max_uses: 6 }],
      }),
    });
    if (!res.ok) {
      const err = await res.text();
      throw new Error(`La IA no respondió (${res.status}): ${err.slice(0, 200)}`);
    }
    const msg = await res.json();
    // deno-lint-ignore no-explicit-any
    text += (msg.content ?? []).filter((b: any) => b.type === "text").map((b: any) => b.text).join("\n");
    if (msg.stop_reason !== "pause_turn") break;
    messages.push({ role: "assistant", content: msg.content });       // búsqueda larga: continuar
  }
  const fenced = [...text.matchAll(/```(?:json)?\s*([\s\S]*?)```/g)].map(m => m[1]);
  const candidates = fenced.length ? fenced.reverse() : [text.slice(text.indexOf("{"), text.lastIndexOf("}") + 1)];
  for (const c of candidates) {
    try { return JSON.parse(c) as AiProposal; } catch { /* siguiente */ }
  }
  throw new Error("La IA no devolvió una lista de fuentes legible. Vuelve a intentarlo.");
}

// ─── Comprobar feeds (y descubrirlos si hace falta) ─────────────────────────
type FeedCheck = { ok: boolean; url: string; items: number; latest: number | null; title: string };

async function fetchText(url: string, ms: number): Promise<{ status: number; text: string; url: string } | null> {
  try {
    const res = await fetch(url, { headers: { "User-Agent": UA, Accept: "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.8, */*;q=0.5" }, redirect: "follow", signal: AbortSignal.timeout(ms) });
    const reader = res.body?.getReader();
    if (!reader) return { status: res.status, text: "", url: res.url || url };
    const chunks: Uint8Array[] = []; let size = 0;
    while (size < 400_000) {
      const { value, done } = await reader.read();
      if (done) break;
      chunks.push(value); size += value.length;
    }
    reader.cancel().catch(() => {});
    const all = new Uint8Array(size); let o = 0;
    for (const c of chunks) { all.set(c, o); o += c.length; }
    return { status: res.status, text: new TextDecoder().decode(all), url: res.url || url };
  } catch { return null; }
}

function inspectFeed(text: string): { items: number; latest: number | null; title: string } | null {
  if (!/<(rss|feed|rdf:RDF)[\s>]/i.test(text.slice(0, 3000))) return null;
  const items = (text.match(/<(item|entry)[\s>]/gi) ?? []).length;
  if (!items) return null;
  const dates = [...text.matchAll(/<(pubDate|updated|published|dc:date)>([^<]+)</gi)].map(m => Date.parse(m[2].trim())).filter(n => !isNaN(n));
  const title = (text.match(/<title[^>]*>(?:<!\[CDATA\[)?([^<\]]+)/i)?.[1] ?? "").trim();
  return { items, latest: dates.length ? Math.max(...dates) : null, title };
}

async function checkFeed(url: string): Promise<FeedCheck | null> {
  if (!/^https?:\/\//i.test(url)) return null;
  const r = await fetchText(url, 8000);
  if (!r || r.status >= 400) return null;
  const info = inspectFeed(r.text);
  return info ? { ok: true, url: r.url, ...info } : null;
}

async function discover(site: string): Promise<FeedCheck | null> {
  if (!/^https?:\/\//i.test(site)) return null;
  const page = await fetchText(site, 8000);
  if (page && page.status < 400) {
    const direct = inspectFeed(page.text);
    if (direct) return { ok: true, url: page.url, ...direct };
    const links = [...page.text.matchAll(/<link[^>]+>/gi)].map(m => m[0])
      .filter(tag => /rel=["']?alternate/i.test(tag) && /(rss|atom)\+xml/i.test(tag))
      .map(tag => tag.match(/href=["']([^"']+)["']/i)?.[1]).filter((h): h is string => !!h)
      .map(h => new URL(h.replace(/&amp;/g, "&"), page.url).toString());
    for (const href of links.slice(0, 3)) {
      const c = await checkFeed(href);
      if (c) return c;
    }
  }
  const base = new URL(site).origin;
  for (const path of ["/feed", "/rss", "/rss.xml", "/feed.xml", "/feeds/all.rss"]) {
    const c = await checkFeed(base + path);
    if (c) return c;
  }
  return null;
}

async function pool<T, R>(items: T[], size: number, fn: (x: T) => Promise<R>): Promise<R[]> {
  const out: R[] = new Array(items.length); let i = 0;
  await Promise.all(Array.from({ length: Math.min(size, items.length) }, async () => {
    while (i < items.length) { const k = i++; out[k] = await fn(items[k]); }
  }));
  return out;
}

async function suggest(topicName: string) {
  const ai = await askClaude(topicName);
  const seen = new Set<string>();
  const sources = (ai.sources ?? []).filter(s => s && (s.feed || s.site)).slice(0, 24);
  const checked = await pool(sources, 8, async s => {
    let c = s.feed ? await checkFeed(s.feed) : null;
    if (!c && s.site) c = await discover(s.site);
    const stale = c?.latest ? Date.now() - c.latest > 45 * D : false;
    return {
      name: (s.name || c?.title || s.site || s.feed || "").slice(0, 60),
      site: s.site || "", feed: c?.url || s.feed || "",
      ok: !!c && !stale, items: c?.items ?? 0,
      note: !c ? "No se encontró un feed que funcione" : stale ? "Sin publicaciones recientes" : "",
    };
  });
  const unique = checked.filter(s => { const k = s.feed || s.site; if (!k || seen.has(k)) return false; seen.add(k); return true; });
  unique.sort((a, b) => Number(b.ok) - Number(a.ok));
  const queries = (ai.queries ?? []).filter(q => q && q.q).slice(0, 10)
    .map(q => ({ q: String(q.q).slice(0, 120), lang: String(q.lang || "es").slice(0, 5), country: String(q.country || "ES").slice(0, 2).toUpperCase() }));
  const name = (ai.name || topicName).trim().slice(0, 60);
  return {
    name, short: (ai.short || name.split(/\s+/)[0]).slice(0, 12), icon: (ai.icon || "📰").slice(0, 8),
    sources: unique, queries,
  };
}

async function topicsApi(req: Request): Promise<Response> {
  // deno-lint-ignore no-explicit-any
  let body: any;
  try { body = await req.json(); } catch { return json({ error: "JSON no válido" }, 400); }
  const key = req.headers.get("x-now-key") ?? body?.key ?? "";
  if (!ADMIN_KEY) return json({ error: "La función no tiene NOW_ADMIN_KEY configurada." }, 500);
  if (!safeEqual(String(key), ADMIN_KEY)) return json({ error: "Clave de administración incorrecta." }, 401);
  if (!REPO || !GH_TOKEN) return json({ error: "Faltan NOW_GITHUB_REPO o NOW_GITHUB_TOKEN en los secrets de la función." }, 500);

  try {
    switch (body.action) {
      case "check":
      case "list": {
        const { topics } = await readTopics();
        return json({ ok: true, ai: !!AI_KEY, topics: topics.map(summary) });
      }
      case "suggest": {
        const name = String(body.name ?? "").trim();
        if (name.length < 2) return json({ error: "Escribe el tema que quieres seguir." }, 400);
        return json({ ok: true, proposal: await suggest(name.slice(0, 80)) });
      }
      case "add": {
        const t = body.topic ?? {};
        const name = String(t.name ?? "").trim().slice(0, 60);
        const feeds = [...new Set((t.feeds ?? []).map(String).filter((u: string) => /^https?:\/\//i.test(u)))].slice(0, 30) as string[];
        const queries = (t.queries ?? []).filter((q: Query) => q && q.q).slice(0, 12)
          .map((q: Query) => ({ q: String(q.q).slice(0, 120), lang: String(q.lang || "es").slice(0, 5), country: String(q.country || "ES").slice(0, 2).toUpperCase() }));
        if (!name) return json({ error: "Falta el nombre del tema." }, 400);
        if (!feeds.length && !queries.length) return json({ error: "El tema necesita al menos una fuente o una búsqueda." }, 400);
        const { topics, sha } = await readTopics();
        let id = slug(name), n = 2;
        while (topics.some(x => x.id === id)) id = `${slug(name)}-${n++}`;
        const used = new Set(topics.map(x => (x.color ?? [])[0]));
        const color = PALETTE.find(p => !used.has(p[0])) ?? PALETTE[topics.length % PALETTE.length];
        const topic: Topic = { id, name, short: String(t.short || name.split(/\s+/)[0]).slice(0, 12), icon: String(t.icon || "📰").slice(0, 8), color, feeds, queries };
        topics.push(topic);
        await writeTopics(topics, sha, `Añadir tema: ${name}`);
        return json({ ok: true, topic: summary(topic), topics: topics.map(summary) });
      }
      case "delete": {
        const { topics, sha } = await readTopics();
        const t = topics.find(x => x.id === body.id);
        if (!t) return json({ error: "Ese tema ya no existe." }, 404);
        const rest = topics.filter(x => x.id !== body.id);
        await writeTopics(rest, sha, `Borrar tema: ${t.name}`);
        return json({ ok: true, topics: rest.map(summary) });
      }
      case "refresh":
        return json({ ok: true, ...(await refreshNow()) });
      case "set_weights":
        return json({ ok: true, weights: await setWeights(body.weights) });
      case "push_key": {
        const k = await pushKeys();
        return json({ ok: true, publicKey: k.pub, devices: (await getDevices()).length });
      }
      case "push_subscribe": {
        const sub = body.subscription ?? {};
        if (!/^https?:\/\//.test(String(sub.endpoint ?? "")) || !sub.keys?.p256dh || !sub.keys?.auth) return json({ error: "La suscripción no es válida." }, 400);
        await pushKeys();
        const list = (await getDevices()).filter(d => d.endpoint !== sub.endpoint);
        list.push({ endpoint: String(sub.endpoint), keys: { p256dh: String(sub.keys.p256dh), auth: String(sub.keys.auth) }, label: String(body.label ?? "").slice(0, 30), at: Date.now() });
        const kept = list.slice(-MAX_DEVICES);
        await setVar(VAR_SUBS, JSON.stringify(kept));
        return json({ ok: true, devices: kept.length });
      }
      case "push_unsubscribe": {
        const list = (await getDevices()).filter(d => d.endpoint !== body.endpoint);
        await setVar(VAR_SUBS, JSON.stringify(list));
        return json({ ok: true, devices: list.length });
      }
      default:
        return json({ error: "Acción no reconocida." }, 400);
    }
  } catch (e) {
    return json({ error: (e as Error).message || "Error interno" }, 502);
  }
}

// ════════════════════════════════════════════════════════════════════════════
//  2 · CONECTOR MCP PARA CLAUDE (lectura de noticias)
// ════════════════════════════════════════════════════════════════════════════

type Article = { src: string; t: number; url: string; c: string };
type Story = { id: string; c: string; cs?: string[]; t: string; sm?: string; lang: string; aff: number; img: string; a: [string, number, string, string?][]; arts?: Article[] };
type Category = { id: string; name: string; short?: string; icon: string };
type IndexCfg = { weights: Record<string, number>; ref_outlets: number; ref_recent: number; recent_hours: number };
type Data = { updated: number; categories: Category[]; authority: string[]; index?: IndexCfg; stories: Story[] };

const cache: Record<string, { at: number; data: Data }> = {};
async function getData(name: "latest.json" | "archive.json"): Promise<Data> {
  if (!SITE) throw new Error("La función no tiene NOW_SITE_URL configurada.");
  const hit = cache[name];
  if (hit && Date.now() - hit.at < 10 * 60e3) return hit.data;
  const res = await fetch(`${SITE}/data/${name}?t=${Date.now()}`);
  if (!res.ok) throw new Error(`No se pudieron leer las noticias (${res.status}).`);
  const data = (await res.json()) as Data;
  for (const s of data.stories) {
    s.cs = s.cs?.length ? s.cs : [s.c];
    s.arts = s.a.map(([src, ts, url, c]) => ({ src, t: ts * 1000, url, c: c || s.c })).sort((x, y) => x.t - y.t);
  }
  cache[name] = { at: Date.now(), data };
  return data;
}

const fmt = (ms: number) => new Date(ms).toLocaleString("es-ES", { timeZone: TZ, dateStyle: "medium", timeStyle: "short" });

function tzOffset(dateStr: string): string {
  const probe = new Date(`${dateStr}T12:00:00Z`);
  const local = new Date(probe.toLocaleString("en-US", { timeZone: TZ }));
  const diffH = Math.round((local.getTime() - probe.getTime()) / H);
  return `${diffH < 0 ? "-" : "+"}${String(Math.abs(diffH)).padStart(2, "0")}:00`;
}

function windowFrom(args: Record<string, unknown>): { a: number; b: number; label: string } {
  const now = Date.now();
  const desde = typeof args.desde === "string" ? args.desde : "";
  const hasta = typeof args.hasta === "string" ? args.hasta : "";
  if (/^\d{4}-\d{2}-\d{2}$/.test(desde)) {
    const end = /^\d{4}-\d{2}-\d{2}$/.test(hasta) ? hasta : new Date(now).toLocaleDateString("en-CA", { timeZone: TZ });
    return { a: Date.parse(`${desde}T00:00:00${tzOffset(desde)}`), b: Date.parse(`${end}T23:59:59${tzOffset(end)}`), label: `${desde} → ${end}` };
  }
  const p = String(args.periodo ?? "7d");
  const days = p === "hoy" ? 1 : p === "30d" ? 30 : 7;
  return { a: now - days * D, b: now, label: p === "hoy" ? "últimas 24 horas" : `últimos ${days} días` };
}
const dataFor = (a: number) => (Date.now() - a > 7 * D + H) ? getData("archive.json") : getData("latest.json");

function findCategory(data: Data, q: unknown) {
  if (typeof q !== "string" || !q.trim()) return null;
  const n = norm(q);
  return data.categories.find(c => c.id === n || norm(c.name) === n || norm(c.short ?? "") === n) ??
    data.categories.find(c => norm(c.name).includes(n) || norm(c.short ?? "").includes(n) || c.id.includes(slug(n))) ?? null;
}

// Índice v2 (igual que la app y build.py):
//   Índice = 100 × (wC·C + wI·I + wA·A + wD·D + wR·R + wP·P)
//   C = ln(1 + medios + 0,5·referencia)/ln(1 + 20) · I = ln(1 + medios en 12 h)/ln(1 + 10)
//   A = ½·mín(1,(secciones−1)/2) + ½·mín(1, referencia/2) · D = mín(1,(días con medios nuevos−1)/2)
//   R = 0,5^(horas desde la última mención / vida media) · P = mín(1, palabras de interés/2)
const DEF_IX: IndexCfg = { weights: { cobertura: .40, impulso: .20, alcance: .15, duracion: .10, recencia: .10, preferencias: .05 }, ref_outlets: 20, ref_recent: 10, recent_hours: 12 };
const nrmTxt = (t: string) => t.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
function ranked(data: Data, a: number, b: number) {
  const now = Date.now();
  const ix = { ...DEF_IX, ...(data.index ?? {}) };
  const tw = Object.values(ix.weights).reduce((x, y) => x + y, 0) || 1;
  const w = Object.fromEntries(Object.entries(ix.weights).map(([k, v]) => [k, v / tw]));
  const res = data.authority.map(x => new RegExp(`(^|[^a-z0-9])${nrmTxt(x).replace(/ /g, "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}([^a-z0-9]|$)`));
  const isAuth = (src: string) => { const n = nrmTxt(src).replace(/ /g, ""); return res.some(r => r.test(n)); };
  const valid = new Set(data.categories.map(c => c.id));
  const rows = data.stories.filter(s => (s.cs ?? [s.c]).some(c => valid.has(c))).map(s => {
    const m = (s.arts ?? []).filter(x => x.t >= a && x.t <= b);
    if (!m.length) return null;
    const names = [...new Set(m.map(x => x.src))];
    const auth = names.filter(isAuth).length;
    const ref = Math.min(now, b), first = m[0].t, last = m[m.length - 1].t;
    const recent = new Set(m.filter(x => x.t >= ref - ix.recent_hours * H).map(x => x.src)).size;
    const secs = new Set(m.map(x => x.c)).size, days = new Set(m.map(x => Math.floor(x.t / D))).size;
    const half = Math.min(72 * H, Math.max(12 * H, (b - a) / 7));
    const f: Record<string, number> = {
      cobertura: Math.min(1, Math.log(1 + names.length + 0.5 * auth) / Math.log(1 + ix.ref_outlets)),
      impulso: Math.min(1, Math.log(1 + recent) / Math.log(1 + ix.ref_recent)),
      alcance: 0.5 * Math.min(1, (secs - 1) / 2) + 0.5 * Math.min(1, auth / 2),
      duracion: Math.min(1, (days - 1) / 2),
      recencia: Math.pow(0.5, Math.max(0, ref - last) / half),
      preferencias: Math.min(1, (s.aff || 0) / 2),
    };
    const idx = Math.round(100 * Object.keys(f).reduce((t, k) => t + (w[k] ?? 0) * f[k], 0));
    return { s, m, names, auth, recent, secs, days, first, last, f, idx };
  }).filter((r): r is NonNullable<typeof r> => r !== null);
  return rows.sort((x, y) => y.idx - x.idx || y.last - x.last);
}

type Row = ReturnType<typeof ranked>[number];
const catName = (data: Data, id: string) => { const c = data.categories.find(x => x.id === id); return c ? `${c.icon} ${c.name}` : id; };
const brief = (data: Data, r: Row, pos: number) => ({
  posicion: pos, id: r.s.id, titular: r.s.t, seccion: catName(data, r.s.c), indice: r.idx,
  medios: r.names.length, ultima_aparicion: fmt(r.last), enlace: r.m.find(x => x.url)?.url ?? "",
});

const periodProps = {
  periodo: { type: "string", enum: ["hoy", "7d", "30d"], description: "Periodo relativo. Por defecto 7d." },
  desde: { type: "string", description: "Fecha inicial AAAA-MM-DD. Sustituye a periodo. Máximo 30 días atrás." },
  hasta: { type: "string", description: "Fecha final AAAA-MM-DD. Por defecto hoy." },
};
const TOOLS = [
  { name: "resumen", description: "Panorama de Now: última actualización, noticias por sección y las 10 más importantes de las últimas 24 horas (o del periodo indicado).",
    inputSchema: { type: "object", properties: periodProps } },
  { name: "formula_indice", description: "Explica la ecuación del índice de importancia de Now (factores, fórmulas y pesos actuales).",
    inputSchema: { type: "object", properties: {} } },
  { name: "secciones", description: "Lista las secciones (temas) de Now: identificador, nombre, nombre corto e icono.",
    inputSchema: { type: "object", properties: {} } },
  { name: "noticias", description: "Ranking de noticias por índice de importancia (0–100), opcionalmente de una sección y en un periodo.",
    inputSchema: { type: "object", properties: {
      seccion: { type: "string", description: "Identificador, nombre o nombre corto de la sección. Vacío = todas." },
      ...periodProps,
      limite: { type: "integer", minimum: 1, maximum: 50, description: "Número de noticias. Por defecto 10." } } } },
  { name: "noticia", description: "Detalle de una noticia: resumen breve, índice y su desglose, medios que la publicaron con fecha y enlace, e imagen.",
    inputSchema: { type: "object", required: ["id"], properties: { id: { type: "string", description: "Identificador devuelto por noticias, buscar o resumen." } } } },
  { name: "buscar", description: "Busca noticias cuyo titular contenga un texto (sin distinguir mayúsculas ni acentos), ordenadas por importancia.",
    inputSchema: { type: "object", required: ["texto"], properties: {
      texto: { type: "string" }, seccion: { type: "string" }, ...periodProps,
      limite: { type: "integer", minimum: 1, maximum: 50 } } } },
];

async function callTool(name: string, args: Record<string, unknown>): Promise<unknown> {
  const limit = Math.min(50, Math.max(1, Number(args.limite) || 10));
  switch (name) {
    case "formula_indice":
      return {
        formula: "Índice = 100 × (wC·C + wI·I + wA·A + wD·D + wR·R + wP·P), pesos que suman 1",
        factores: {
          C_cobertura: "ln(1 + medios + 0,5·medios de referencia) / ln(1 + 20), tope 1",
          I_impulso: "ln(1 + medios en las últimas 12 h) / ln(1 + 10), tope 1",
          A_alcance: "½·mín(1, (secciones − 1)/2) + ½·mín(1, medios de referencia/2)",
          D_duracion: "mín(1, (días con medios nuevos − 1)/2)",
          R_recencia: "0,5^(horas desde la última mención / vida media); vida media = periodo/7 entre 12 y 72 h",
          P_preferencias: "mín(1, palabras de interés en el titular / 2)",
        },
        pesos_actuales: (await getData("latest.json")).index?.weights ?? DEF_IX.weights,
      };
    case "secciones": {
      const d = await getData("latest.json");
      return d.categories.map(c => ({ id: c.id, nombre: c.name, corto: c.short, icono: c.icon }));
    }
    case "resumen": {
      const w = args.periodo || args.desde ? windowFrom(args) : { a: Date.now() - D, b: Date.now(), label: "últimas 24 horas" };
      const d = await dataFor(w.a);
      const rows = ranked(d, w.a, w.b);
      const counts: Record<string, number> = {};
      for (const r of rows) counts[r.s.c] = (counts[r.s.c] ?? 0) + 1;
      return {
        actualizado: fmt(d.updated * 1000), periodo: w.label, total_noticias: rows.length,
        por_seccion: d.categories.map(c => ({ seccion: `${c.icon} ${c.name}`, noticias: counts[c.id] ?? 0 })),
        mas_importantes: rows.slice(0, 10).map((r, i) => brief(d, r, i + 1)), app: SITE,
      };
    }
    case "noticias":
    case "buscar": {
      const w = windowFrom(args);
      const d = await dataFor(w.a);
      let rows = ranked(d, w.a, w.b);
      if (args.seccion) {
        const c = findCategory(d, args.seccion);
        if (!c) return { error: `No existe la sección «${args.seccion}». Usa la herramienta secciones para ver las disponibles.` };
        rows = rows.filter(r => (r.s.cs ?? [r.s.c]).includes(c.id));
      }
      if (name === "buscar") {
        const q = norm(String(args.texto ?? ""));
        if (!q) return { error: "Indica el texto a buscar." };
        rows = rows.filter(r => norm(r.s.t).includes(q));
      }
      return { actualizado: fmt(d.updated * 1000), periodo: w.label, encontradas: rows.length, noticias: rows.slice(0, limit).map((r, i) => brief(d, r, i + 1)) };
    }
    case "noticia": {
      const id = String(args.id ?? "");
      for (const file of ["latest.json", "archive.json"] as const) {
        const d = await getData(file);
        const s = d.stories.find(x => x.id === id);
        if (!s) continue;
        const rows = ranked(d, Date.now() - 30 * D, Date.now());
        const pos = rows.findIndex(r => r.s.id === id);
        const r = rows[pos];
        return {
          id, titular: s.t, resumen: s.sm || null, seccion: catName(d, s.c), idioma: s.lang, imagen: s.img || null,
          indice: r?.idx ?? null, posicion_en_30_dias: pos >= 0 ? pos + 1 : null,
          desglose: r ? {
            formula: "Índice = 100 × (wC·C + wI·I + wA·A + wD·D + wR·R + wP·P)",
            medios: r.names.length, medios_de_referencia: r.auth, medios_ultimas_12h: r.recent, secciones: r.secs, dias_con_medios_nuevos: r.days,
            factores_0_a_1: Object.fromEntries(Object.entries(r.f).map(([k, v]) => [k, Math.round(v * 100) / 100])),
            pesos: d.index?.weights ?? DEF_IX.weights,
          } : null,
          fuentes: (s.arts ?? []).map(x => ({ medio: x.src, fecha: fmt(x.t), enlace: x.url || null })),
        };
      }
      return { error: `No encuentro la noticia «${id}» en los últimos 30 días.` };
    }
  }
  throw Object.assign(new Error(`Herramienta desconocida: ${name}`), { code: -32602 });
}

async function handleRpc(msg: { id?: string | number; method?: string; params?: Record<string, unknown> }) {
  const { id, method, params = {} } = msg;
  const ok = (result: unknown) => ({ jsonrpc: "2.0", id, result });
  const fail = (code: number, message: string) => ({ jsonrpc: "2.0", id, error: { code, message } });
  try {
    switch (method) {
      case "initialize": {
        const asked = String(params.protocolVersion ?? "");
        return ok({
          protocolVersion: PROTOCOLS.includes(asked) ? asked : PROTOCOLS[0],
          capabilities: { tools: { listChanged: false } },
          serverInfo: { name: "now", title: "Now · noticias relevantes", version: "2.0.0" },
          instructions: "Now es un lector de las noticias más importantes de los temas que sigue el usuario. Empieza con «resumen» para situarte. " +
            "El índice de importancia (0–100) combina cuántos medios cubren la noticia (las agencias pesan más), la velocidad de difusión, " +
            "la diversidad, la frescura y la afinidad con los intereses del usuario. Las fechas van en AAAA-MM-DD. " +
            "Hay datos de los últimos 30 días y se actualizan cada 2 horas. Los temas se gestionan desde los ajustes de la app.",
        });
      }
      case "ping": return ok({});
      case "tools/list": return ok({ tools: TOOLS });
      case "tools/call": {
        const result = await callTool(String(params.name), (params.arguments ?? {}) as Record<string, unknown>);
        const isError = typeof result === "object" && result !== null && "error" in result;
        return ok({ content: [{ type: "text", text: JSON.stringify(result, null, 1) }], isError });
      }
      case "resources/list": return ok({ resources: [] });
      case "prompts/list": return ok({ prompts: [] });
      default: return fail(-32601, `Método no soportado: ${method}`);
    }
  } catch (e) {
    const err = e as { code?: number; message?: string };
    if (method === "tools/call" && err.code === undefined) return ok({ content: [{ type: "text", text: err.message ?? "Error" }], isError: true });
    return fail(err.code ?? -32603, err.message ?? "Error interno");
  }
}

async function mcp(req: Request): Promise<Response> {
  let body: unknown;
  try { body = await req.json(); } catch { return json({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "JSON no válido" } }, 400); }
  const batch = Array.isArray(body) ? body : [body];
  const replies = [];
  for (const msg of batch) {
    if (msg && typeof msg === "object" && "id" in msg && msg.id !== undefined && msg.id !== null) replies.push(await handleRpc(msg));
  }
  if (!replies.length) return new Response(null, { status: 202, headers: CORS });       // solo notificaciones
  return json(Array.isArray(body) ? replies : replies[0]);
}

// ════════════════════════════════════════════════════════════════════════════
Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
  if (req.method !== "POST") return new Response(null, { status: 405, headers: { ...CORS, Allow: "POST, OPTIONS" } });
  const path = new URL(req.url).pathname.replace(/\/+$/, "");
  return path.endsWith("/topics") ? topicsApi(req) : mcp(req);
});
