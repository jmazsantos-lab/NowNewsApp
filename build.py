#!/usr/bin/env python3
"""
Now — construye los datos de la app de noticias.

En cada ejecución:
  1. Descarga el histórico de 30 días ya publicado en la web (data/archive.json).
     El histórico vive en la propia web: no hace falta base de datos.
  2. Lee las fuentes de cada tema de topics.json: feeds RSS y búsquedas de
     Google News.
  3. Agrupa los artículos que cuentan lo mismo (TF-IDF sobre titular y
     entradilla, entre todas las secciones) y fusiona historias duplicadas.
  4. Poda: borra lo que tiene más de 30 días, lo de temas eliminados y se
     queda con las historias más cubiertas de cada tema y día.
  5. Calcula el índice de importancia v2 de cada historia y envía una notificación
     push de las que superan el umbral (75) y no se habían avisado antes.
  6. Escribe site/data/latest.json (7 días) y site/data/archive.json (30 días).

Variables de entorno:
  NOW_SITE_URL      URL pública de la app (la pone el workflow automáticamente)
  NOW_API_URL       URL de la función de Supabase (opcional, para los ajustes)
  NOW_SKIP_DOWNLOAD =1 para empezar sin histórico (pruebas)
  NOW_PUSH_KEYS     JSON {"pub","priv"} con las claves VAPID (variable del repositorio)
  NOW_PUSH_SUBS     JSON con los dispositivos suscritos a las notificaciones
  NOW_PUSH_TEST     =1 para enviar una notificación de prueba y nada más
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import logging
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import feedparser
import requests

logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("now")

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "now_config.json").read_text(encoding="utf-8"))
DATA_DIR = ROOT / "site" / "data"
DAY = 86400
TIMEOUT = 15
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NowNews/1.0)"}


# ─── Lectura de fuentes ─────────────────────────────────────────────────────

def google_news_url(query: str, lang: str = "es", country: str = "ES") -> str:
    return (f"https://news.google.com/rss/search?q={quote_plus(query)}"
            f"&hl={lang}-{country}&gl={country}&ceid={country}:{lang}")


def parse_date(entry):
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val:
            try:
                return datetime(*val[:6])
            except Exception:
                pass
    return None


def clean_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


_BOILER = [
    re.compile(r"\s*The post .{0,240}? appeared first on .*$", re.I),
    re.compile(r"\s*(Continue reading|Read more|Read the full (story|article)|Leer m[aá]s|Lee m[aá]s|Seguir leyendo|Sigue leyendo|Lire la suite)\b.*$", re.I),
    re.compile(r"\s*(Sign up|Subscribe|Suscr[ií]bete|Follow us)\b.{0,120}$", re.I),
    re.compile(r"\s*\[?(…|\.\.\.)\]?\s*$"),
]
SUMMARY_MAX = 240


def tidy_summary(raw: str, title: str) -> str:
    """Resumen breve a partir de la descripción del feed: limpia restos de
    maquetación, descarta lo que solo repite el titular y recorta a ~240 caracteres."""
    text = clean_html(html.unescape(raw or ""))
    for pat in _BOILER:
        text = pat.sub("", text).strip()
    t = (title or "").strip()
    if t and text.lower().startswith(t.lower()):
        text = text[len(t):].lstrip(" -–—:|.·").strip()
    if len(text) < 60:
        return ""
    if jaccard(keywords(text), keywords(t)) > 0.8 and len(text) < len(t) + 40:
        return ""
    if len(text) > SUMMARY_MAX:
        cut = text[:SUMMARY_MAX]
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        text = cut[:end + 1] if end >= 110 else cut[:cut.rfind(" ")].rstrip(" ,;:-–—") + "…"
    return text


def better_summary(old: str, new: str) -> bool:
    """Se queda con el resumen más completo; la diferencia debe ser clara para no ir cambiándolo."""
    return bool(new) and (not old or len(new) > len(old) + 40)


def extract_image(entry):
    for field in ("media_thumbnail", "media_content"):
        items = getattr(entry, field, None) or []
        for it in items:
            url = it.get("url") if hasattr(it, "get") else None
            if url and (field == "media_thumbnail" or it.get("medium") == "image"
                        or re.search(r"\.(jpe?g|png|webp|gif)(\?|$)", url, re.I)):
                return url
    for enc in getattr(entry, "enclosures", []) or []:
        url = enc.get("href") or enc.get("url")
        if url and (enc.get("type") or "").lower().startswith("image/"):
            return url
    for field in (getattr(entry, "summary", ""), getattr(entry, "description", "")):
        m = re.search(r'<img[^>]+src=[\'"]([^\'"]+)[\'"]', field or "")
        if m:
            return m.group(1)
    return None


def fetch_feed(url: str, max_age_hours: int) -> list[dict]:
    out = []
    try:
        resp = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
        feed_title = clean_html(feed.feed.get("title", "")) or url.split("/")[2]
        for e in feed.entries:
            pub = parse_date(e)
            if pub and pub < cutoff:
                continue
            title = clean_html(getattr(e, "title", ""))
            source = ""
            if "news.google.com" in url and " - " in title:     # «Titular - Medio»
                title, source = (p.strip() for p in title.rsplit(" - ", 1))
            out.append({
                "title": title, "link": getattr(e, "link", ""),
                "summary": "" if "news.google.com" in url else clean_html(html.unescape(getattr(e, "summary", "") or getattr(e, "description", "")))[:900],
                "source": source or short_source(feed_title, url),
                "image": extract_image(e), "pub_date": pub,
            })
    except requests.RequestException as exc:
        log.debug("Sin respuesta de %s: %s", url, exc)
    except Exception as exc:
        log.warning("Error leyendo %s: %s", url, exc)
    return out


def short_source(title: str, url: str) -> str:
    """«Latest News | Brewbound» → «Brewbound». Si no hay título, el dominio."""
    if " | " in title:
        parts = [p.strip() for p in title.split(" | ") if len(p.strip()) >= 4]
        if parts:
            title = min(parts, key=len)
    title = re.sub(r"\s*[-–:]\s*(rss|feed|news feed|latest news|all news|noticias|portada).*$", "", title, flags=re.I)
    title = re.sub(r"\s+(rss|feed)$", "", title, flags=re.I).strip()
    if not title:
        title = re.sub(r"^www\.", "", url.split("/")[2])
    return title[:40]


def fetch_all(tasks: list[tuple[str, str]], max_age_hours: int) -> dict[str, list[dict]]:
    results: dict[str, list[dict]] = defaultdict(list)
    with ThreadPoolExecutor(max_workers=40) as pool:
        futures = {pool.submit(fetch_feed, url, max_age_hours): tid for tid, url in tasks}
        for f in as_completed(futures):
            results[futures[f]].extend(f.result())
    return results


# ─── Texto ──────────────────────────────────────────────────────────────────

STOP_WORDS = {
    "the", "a", "an", "in", "on", "at", "for", "of", "to", "is", "are", "was", "were", "and", "or",
    "but", "with", "from", "by", "as", "its", "this", "that", "have", "has", "had", "will", "would",
    "could", "said", "new", "says", "more", "over", "after", "their", "been",
    "el", "la", "los", "las", "un", "una", "de", "en", "por", "para", "con", "y", "e", "del", "al",
    "que", "su", "se", "es", "son", "ha", "lo", "le", "les", "sino", "pero", "como", "más", "ya", "si",
    "le", "les", "des", "du", "et", "pour", "dans", "sur", "une", "est", "der", "die", "das", "und",
}
_ES = frozenset("de la el en que los las una del con por para como este esta son más pero también "
                "porque cuando donde hay fue ser han tras según sobre entre hasta desde nuevo nueva".split())


def keywords(title: str) -> set:
    return set(re.findall(r"\b[a-záéíóúüñàèçâêîôûëïœ]{4,}\b", title.lower())) - STOP_WORDS


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def detect_lang(text: str) -> str:
    words = set(re.findall(r"\b[a-záéíóúüñ]{2,}\b", text.lower()))
    return "es" if len(words & _ES) >= 3 else "en"


_W = "a-záéíóúüñ0-9"
_PROFILE = [re.compile(rf"(?<![{_W}]){re.escape(k.lower())}" + (rf"(?![{_W}])" if len(k) < 4 else ""))
            for k in CFG.get("profile_keywords", [])]


def affinity(title: str) -> int:
    low = title.lower()
    return sum(1 for p in _PROFILE if p.search(low))


def to_epoch(pub) -> int | None:
    return int(pub.replace(tzinfo=timezone.utc).timestamp()) if pub else None


# ─── Temas e histórico ──────────────────────────────────────────────────────

def load_topics() -> list[dict]:
    topics = json.loads((ROOT / "topics.json").read_text(encoding="utf-8"))["topics"]
    for t in topics:
        t.setdefault("feeds", []); t.setdefault("queries", [])
    return topics


def url_tasks(topics: list[dict]) -> list[tuple[str, str]]:
    tasks = []
    for t in topics:
        for url in dict.fromkeys(t["feeds"]):
            tasks.append((t["id"], url))
        for q in t["queries"]:
            if isinstance(q, str):
                q = {"q": q}
            tasks.append((t["id"], google_news_url(q["q"], q.get("lang", "es"), q.get("country", "ES"))))
    return tasks


def site_url() -> str:
    return (os.environ.get("NOW_SITE_URL") or CFG.get("site_url", "")).rstrip("/")


def load_previous() -> dict:
    if os.environ.get("NOW_SKIP_DOWNLOAD") == "1" or not site_url():
        log.info("Se empieza sin histórico.")
        return {"stories": []}
    url = site_url() + "/data/archive.json"
    try:
        resp = requests.get(url, timeout=40, headers=HEADERS)
    except requests.RequestException as exc:
        raise SystemExit(f"No se pudo descargar el histórico ({exc}). Se cancela para no perderlo.")
    if resp.status_code == 404:
        log.info("Primera ejecución: todavía no hay histórico en %s", url)
        return {"stories": []}
    resp.raise_for_status()
    data = resp.json()
    log.info("Histórico cargado: %d historias", len(data.get("stories", [])))
    return data


# ─── Agrupación de artículos en historias ──────────────────────────────────
# Cada artículo se convierte en un vector de términos (titular ×2 + entradilla),
# con raíces simples (elecciones/electoral → «eleccion»), alias entre idiomas
# (Brazil → brasil) y pesos TF-IDF: los nombres propios y términos raros pesan
# mucho, las palabras comunes casi nada. Un artículo se une a la historia con la
# que comparte al menos dos términos distintivos y cuyo coseno supera el umbral.
# Después, las historias que hablan de lo mismo se fusionan. Se agrupa entre
# todas las secciones: una misma noticia suma los medios de todas ellas.

_CL_STOP = set("""a al algo ante antes asi aun cada como con contra cual cuando de del desde donde dos e el ella ellas ellos en entre era es esa ese eso esta este esto estos estas fue ha han hasta hay la las le les lo los mas me mi muy no nos o otra otro para pero poco por porque que quien se sea segun ser si sin sobre son su sus tambien tan te tiene tras tu un una uno unos unas y ya
enero febrero marzo abril mayo junio julio agosto septiembre setiembre octubre noviembre diciembre lunes martes miercoles jueves viernes sabado domingo
january february march april june july august september october november december monday tuesday wednesday thursday friday saturday sunday
hoy ayer manana directo vivo ultima ultimas hora horas minuto video fotos noticias noticia sigue claves ahora nuevo nueva nuevos nuevas mientras durante
the an and or but of to in on at for from by with as is are was were be been has have had will would could should may might can its it this that these those his her their our your you they we he she not no over after before about into than then also just more most new says said say week day days today live update updates latest news what how why who when where which there here up out off amid via
des du le et pour dans sur une est au aux par do da das em na nas um uma com""".split())
_ALIAS = {
    "brazil": "brasil", "brazilian": "brasil", "brazilians": "brasil", "brasileno": "brasil", "brasilena": "brasil",
    "election": "eleccion", "elections": "eleccion", "electoral": "eleccion", "electorales": "eleccion", "elecciones": "eleccion",
    "eleicao": "eleccion", "eleicoes": "eleccion", "runoff": "balotaje", "run-off": "balotaje",
    "vote": "voto", "votes": "voto", "voting": "voto", "votos": "voto", "votacion": "voto", "votaciones": "voto",
    "spain": "espana", "spanish": "espana", "mexican": "mexico", "mexicano": "mexico", "mexicana": "mexico",
    "president": "presidente", "presidential": "presidente", "presidencial": "presidente", "presidencia": "presidente",
    "government": "gobierno", "snap": "adelanto", "calls": "convoca", "call": "convoca",
    "usa": "eeuu", "us": "eeuu", "u.s.": "eeuu", "ee.uu.": "eeuu",
}
MIN_IDF = 2.0           # término distintivo: sale en menos de ~1 de cada 3 artículos (las noticias grandes siguen agrupándose)


def norm_text(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").lower()) if unicodedata.category(c) != "Mn")


def _stem(w: str) -> str:
    if len(w) > 5 and w.endswith("es"):
        w = w[:-2]
    elif len(w) > 4 and w.endswith("s"):
        w = w[:-1]
    return w[:7]


def tokens(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9ñ][a-z0-9ñ'\-\.]*[a-z0-9ñ]|[a-z]", norm_text(text)):
        w = w.strip("'.-")
        if w in _ALIAS:
            out.append(_ALIAS[w]); continue
        if w in _CL_STOP or len(w) < 3 or w.replace(".", "").isdigit():
            continue
        out.append(_stem(w))
    return out


def text_vector(title: str, summary: str = "") -> Counter:
    v = Counter()
    for t in tokens(title):
        v[t] += 2
    for t in tokens(summary)[:30]:
        v[t] += 1
    return v


class Idf:
    def __init__(self, docs):
        self.df, self.n = Counter(), 0
        for d in docs:
            self.df.update(set(d)); self.n += 1
        self.cache = {}

    def __call__(self, t: str) -> float:
        if t not in self.cache:
            self.cache[t] = math.log((self.n + 1) / (self.df[t] + 1)) + 1
        return self.cache[t]


def cosine(a: Counter, b: Counter, idf: Idf) -> float:
    common = a.keys() & b.keys()
    if sum(1 for t in common if idf(t) >= MIN_IDF) < 2:
        return 0.0
    num = sum(a[t] * b[t] * idf(t) ** 2 for t in common)
    na = math.sqrt(sum((x * idf(t)) ** 2 for t, x in a.items()))
    nb = math.sqrt(sum((x * idf(t)) ** 2 for t, x in b.items()))
    return num / (na * nb) if na and nb else 0.0


def trim(v: Counter, k: int = 60) -> Counter:
    return Counter(dict(v.most_common(k))) if len(v) > k else v


_AUTH = [re.compile(rf"(?<![a-z0-9]){re.escape(norm_text(a).replace(' ', ''))}(?![a-z0-9])") for a in CFG["authority_sources"]]
_EXCLUDE = [re.compile(norm_text(p)) for p in CFG.get("exclude_patterns", [])]


def is_authority(source: str) -> bool:
    n = norm_text(source).replace(" ", "")
    return any(p.search(n) for p in _AUTH)


def excluded(title: str) -> bool:
    n = norm_text(title)
    return any(p.search(n) for p in _EXCLUDE)


def _prepare(s: dict) -> None:
    kw = s.get("kw") or {}
    s["_v"] = Counter(kw) if isinstance(kw, dict) else Counter({k: 2 for k in kw})
    for a in s["a"]:
        if len(a) < 4:
            a.append(s["c"])                               # histórico antiguo: sección de la historia
    s["_cs"] = set(s.get("cs") or []) | {a[3] for a in s["a"]} | {s["c"]}
    s.setdefault("tt", s["a"][0][1])
    s["first"], s["last"] = s["a"][0][1], s["a"][-1][1]


def _add(s: dict, art: dict, thr: float, idf: Idf, max_links: int) -> bool:
    """Añade el artículo a la historia. Devuelve True si suma un medio nuevo."""
    s["_cs"].add(art["topic"])
    sm = tidy_summary(art.get("summary", ""), art["title"])
    newer = art["ts"] >= s["tt"] + 6 * 3600
    if newer and cosine(art["v"], s["_v"], idf) >= thr:   # titular más reciente: la historia ha avanzado
        s["t"], s["tt"], s["lang"] = art["title"], art["ts"], detect_lang(art["title"] + " " + art.get("summary", ""))
        if sm:
            s["sm"] = sm
    elif better_summary(s.get("sm", ""), sm):
        s["sm"] = sm
    s["_v"] = trim(s["_v"] + art["v"])
    s["aff"] = max(s.get("aff", 0), affinity(art["title"]))
    if not s["img"] and art.get("image"):
        s["img"] = art["image"]
    if any(a[0] == art["source"] for a in s["a"]):
        return False                                       # cada medio cuenta una vez
    link = art["link"] if sum(1 for a in s["a"] if a[2]) < max_links else ""
    s["a"].append([art["source"], art["ts"], link, art["topic"]])
    s["a"].sort(key=lambda a: a[1])
    s["first"], s["last"] = s["a"][0][1], s["a"][-1][1]
    return True


def _absorb(keep: dict, other: dict) -> None:
    seen = {a[0]: a for a in keep["a"]}
    for a in other["a"]:
        if a[0] not in seen or a[1] < seen[a[0]][1]:
            seen[a[0]] = a
    keep["a"] = sorted(seen.values(), key=lambda a: a[1])
    keep["first"], keep["last"] = keep["a"][0][1], keep["a"][-1][1]
    keep["_v"] = trim(keep["_v"] + other["_v"])
    keep["_cs"] |= other["_cs"]
    keep["aff"] = max(keep.get("aff", 0), other.get("aff", 0))
    keep["img"] = keep["img"] or other["img"]
    if other["tt"] >= keep["tt"] + 6 * 3600:               # el titular más reciente manda
        keep["t"], keep["tt"], keep["lang"] = other["t"], other["tt"], other.get("lang", keep.get("lang"))
        if other.get("sm"):
            keep["sm"] = other["sm"]
    elif better_summary(keep.get("sm", ""), other.get("sm", "")):
        keep["sm"] = other["sm"]


def merge(stories: list[dict], fetched: dict[str, list[dict]], topics: list[dict], now: int) -> tuple[int, int, dict]:
    """Agrupa los artículos nuevos en historias. Devuelve (medios añadidos, historias nuevas, fusiones {id: id})."""
    thr = CFG["cluster_threshold"]
    window = CFG.get("match_window_hours", 48) * 3600
    max_age, max_links = CFG["max_article_age_hours"] * 3600, CFG["max_links_per_story"]
    for s in stories:
        _prepare(s)
    by_link = {a[2]: s for s in stories for a in s["a"] if a[2]}

    arts, seen_links = [], set()
    for tid, items in fetched.items():
        for art in items:
            title, src = (art.get("title") or "").strip(), (art.get("source") or "").strip()
            if not title or not src or excluded(title):
                continue
            ts = min(to_epoch(art.get("pub_date")) or now, now)
            if now - ts > max_age:
                continue
            v = text_vector(title, art.get("summary", ""))
            if len(v) < 2:
                continue
            link = art.get("link") or ""
            arts.append({**art, "title": title, "source": src, "ts": ts, "topic": tid, "v": v, "link": link,
                         "dup": bool(link) and link in seen_links})
            seen_links.add(link)
    arts.sort(key=lambda a: a["ts"])

    active = [s for s in stories if now - s["last"] <= window]
    idf = Idf([a["v"] for a in arts if not a["dup"]] + [s["_v"] for s in active])
    inv: dict[str, list[dict]] = defaultdict(list)

    def index(s):
        for t in s["_v"]:
            if idf(t) >= MIN_IDF and s not in inv[t]:
                inv[t].append(s)
    for s in active:
        index(s)

    added = created = 0
    for art in arts:
        if art["link"] and art["link"] in by_link:        # mismo artículo ya leído (otra sección u otra ejecución)
            by_link[art["link"]]["_cs"].add(art["topic"])
            continue
        best, best_sim = None, 0.0
        cands = {id(s): s for t in art["v"] if idf(t) >= MIN_IDF for s in inv.get(t, [])}
        for s in cands.values():
            sim = cosine(art["v"], s["_v"], idf)
            if sim > best_sim:
                best, best_sim = s, sim
        if best is None or best_sim < thr:
            best = {"id": hashlib.sha1((art["link"] or art["title"]).encode()).hexdigest()[:10], "c": art["topic"],
                    "t": art["title"], "tt": art["ts"], "lang": detect_lang(art["title"] + " " + art.get("summary", "")),
                    "aff": 0, "img": "", "a": [], "_v": Counter(), "_cs": {art["topic"]}, "first": art["ts"], "last": art["ts"]}
            stories.append(best); created += 1
        added += _add(best, art, thr, idf, max_links)
        if art["link"]:
            by_link[art["link"]] = best
        index(best)

    # Fusión: historias recientes que cuentan lo mismo (también corrige el histórico)
    merged: dict[str, str] = {}
    pool = sorted((s for s in stories if now - s["last"] <= window), key=lambda s: -len(s["a"]))
    alive, dead = {id(s) for s in pool}, set()
    for s in pool:
        if id(s) not in alive:
            continue
        for t in list(s["_v"]):
            if idf(t) < MIN_IDF:
                continue
            for o in inv.get(t, []):
                if o is s or id(o) not in alive:
                    continue
                if cosine(s["_v"], o["_v"], idf) >= thr:
                    _absorb(s, o); alive.discard(id(o)); dead.add(id(o)); merged[o["id"]] = s["id"]
    if merged:
        stories[:] = [s for s in stories if id(s) not in dead]
        log.info("Historias fusionadas por tratar lo mismo: %d", len(merged))

    for s in stories:                                      # sección principal = la que más medios aporta
        counts = Counter(a[3] for a in s["a"])
        s["c"] = counts.most_common(1)[0][0] if counts else s["c"]
    return added, created, merged


def prune(stories: list[dict], topics: list[dict], now: int) -> list[dict]:
    ids = {t["id"] for t in topics}
    keep_from = now - CFG["keep_days"] * DAY
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for s in stories:
        s["a"] = [a for a in s["a"] if a[3] in ids]        # menciones de secciones borradas
        s["_cs"] = {c for c in s["_cs"] if c in ids}
        if not s["a"] or s["last"] < keep_from:
            continue
        s["first"], s["last"] = s["a"][0][1], s["a"][-1][1]
        if s["c"] not in ids:
            s["c"] = Counter(a[3] for a in s["a"]).most_common(1)[0][0]
        groups[(s["c"], datetime.fromtimestamp(s["first"], timezone.utc).date())].append(s)
    out = []
    for g in groups.values():
        g.sort(key=lambda s: (len(s["a"]), bool(s["img"]), s["last"]), reverse=True)
        out.extend(g[:CFG["max_stories_per_topic_day"]])
    return sorted(out, key=lambda s: s["last"], reverse=True)


def write_outputs(stories: list[dict], topics: list[dict], now: int, notified: dict | None = None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    meta = {
        "updated": now,
        "api_url": os.environ.get("NOW_API_URL", "").strip() or CFG.get("api_url", ""),
        "categories": [{"id": t["id"], "name": t["name"], "short": t.get("short") or t["name"].split()[0],
                        "icon": t.get("icon") or "📰", "color": t.get("color"),
                        "sources": len(t["feeds"]), "queries": len(t["queries"])} for t in topics],
        "authority": CFG["authority_sources"],
        "index": index_config(),
        "notify_threshold": CFG.get("notify_threshold", 75),
    }

    def public(s: dict, archive: bool) -> dict:
        d = {k: s[k] for k in ("id", "c", "t", "lang", "aff", "img", "a")}
        d["cs"] = sorted(s.get("_cs") or {s["c"]})
        if s.get("sm"):
            d["sm"] = s["sm"]
        if archive:
            d["kw"] = dict(s["_v"].most_common(40))
            d["tt"] = s.get("tt", s["first"])
        return d

    latest_from = now - CFG["latest_days"] * DAY
    files = {
        "latest.json": {**meta, "days": CFG["latest_days"], "stories": [public(s, False) for s in stories if s["last"] >= latest_from]},
        "archive.json": {**meta, "days": CFG["keep_days"], "stories": [public(s, True) for s in stories],
                         "notified": notified or {}},
    }
    for name, payload in files.items():
        path = DATA_DIR / name
        path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        log.info("%s: %d historias · %.0f KB", name, len(payload["stories"]), path.stat().st_size / 1024)


# ─── Índice de importancia v2 (mismo cálculo en la app y en el conector) ─────
#
#   Índice = 100 × (wC·C + wI·I + wA·A + wD·D + wR·R + wP·P)       (pesos que suman 1)
#
#   C  Cobertura     ln(1 + medios + 0,5·agencias) / ln(1 + 20)        tope 1
#   I  Impulso       ln(1 + medios en las últimas 12 h) / ln(1 + 10)   tope 1
#   A  Alcance       ½·min(1, (secciones − 1)/2) + ½·min(1, agencias/2)
#   D  Duración      min(1, (días con medios nuevos − 1)/2)
#   R  Recencia      0,5 ^ (horas desde la última mención / vida media)   vida media = periodo/7, entre 12 y 72 h
#   P  Preferencias  min(1, palabras de tus intereses en el titular / 2)

INDEX_DEFAULT = {
    "weights": {"cobertura": 0.40, "impulso": 0.20, "alcance": 0.15, "duracion": 0.10, "recencia": 0.10, "preferencias": 0.05},
    "ref_outlets": 20, "ref_recent": 10, "recent_hours": 12,
}


def index_config() -> dict:
    ix = CFG.get("index") or {}
    w = {**INDEX_DEFAULT["weights"], **(ix.get("weights") or {})}
    w = {k: max(0.0, float(v)) for k, v in w.items() if k in INDEX_DEFAULT["weights"]}
    total = sum(w.values()) or 1.0
    return {**INDEX_DEFAULT, **{k: v for k, v in ix.items() if k != "weights"},
            "weights": {k: round(v / total, 4) for k, v in w.items()}}


def factors(s: dict, start: int, end: int, now: int, ix: dict) -> dict | None:
    m = [x for x in s["a"] if start <= x[1] <= end]
    if not m:
        return None
    names = list(dict.fromkeys(x[0] for x in m))
    auth = sum(1 for n in names if is_authority(n))
    ref = min(now, end)
    last = max(x[1] for x in m)
    recent = len({x[0] for x in m if x[1] >= ref - ix["recent_hours"] * 3600})
    sections = len({x[3] if len(x) > 3 else s["c"] for x in m})
    days = len({x[1] // DAY for x in m})
    half = min(72 * 3600, max(12 * 3600, (end - start) / 7))
    f = {
        "cobertura": min(1.0, math.log(1 + len(names) + 0.5 * auth) / math.log(1 + ix["ref_outlets"])),
        "impulso": min(1.0, math.log(1 + recent) / math.log(1 + ix["ref_recent"])),
        "alcance": 0.5 * min(1.0, (sections - 1) / 2) + 0.5 * min(1.0, auth / 2),
        "duracion": min(1.0, (days - 1) / 2),
        "recencia": 0.5 ** (max(0, ref - last) / half),
        "preferencias": min(1.0, (s.get("aff") or 0) / 2),
    }
    idx = int(100 * sum(ix["weights"][k] * f[k] for k in f) + 0.5)
    return {"s": s, "f": f, "idx": idx, "last": last, "outlets": len(names), "auth": auth,
            "recent": recent, "sections": sections, "days": days}


def importance(stories: list[dict], now: int, window_days: int) -> list[dict]:
    ix = index_config()
    rows = [r for r in (factors(s, now - window_days * DAY, now, now, ix) for s in stories) if r]
    return sorted(rows, key=lambda r: (r["idx"], r["last"]), reverse=True)


# ─── Notificaciones push ────────────────────────────────────────────────────

def load_json_env(name: str, default):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        log.warning("%s no contiene un JSON válido; se ignora.", name)
        return default


def send_pushes(subs: list[dict], keys: dict, messages: list[dict]) -> int:
    """Envía cada mensaje a cada dispositivo. Devuelve cuántos envíos llegaron al servicio push."""
    if not subs or not messages:
        return 0
    if not keys.get("priv"):
        log.warning("Hay dispositivos suscritos pero faltan las claves VAPID (NOW_PUSH_KEYS).")
        return 0
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        log.warning("Falta pywebpush (pip install pywebpush): no se envían notificaciones.")
        return 0
    # El identificador VAPID («sub») solo admite un dominio https o un mailto:, sin ruta.
    u = urlparse(site_url())
    subject = os.environ.get("NOW_VAPID_SUBJECT") or (f"https://{u.netloc}" if u.scheme == "https" and u.netloc else "mailto:now@example.com")
    delivered = 0
    for sub in subs:
        info = sub.get("sub") or sub
        for msg in messages:
            try:
                webpush(subscription_info=info, data=json.dumps(msg, ensure_ascii=False),
                        vapid_private_key=keys["priv"], vapid_claims={"sub": subject},
                        ttl=6 * 3600, timeout=15)
                delivered += 1
            except WebPushException as exc:
                code = getattr(getattr(exc, "response", None), "status_code", None)
                if code in (404, 410):
                    log.warning("Dispositivo caducado (borra y vuelve a activar las notificaciones en la app): %s…", info.get("endpoint", "")[:60])
                    break
                log.warning("Fallo al enviar la notificación (%s): %s", code, str(exc)[:160])
            except Exception as exc:                        # red, claves mal formadas…
                log.warning("Fallo al enviar la notificación: %s", str(exc)[:160])
    return delivered


def notify(stories: list[dict], topics: list[dict], previous: dict, now: int, merged: dict | None = None) -> dict:
    """Avisa de las historias que superan el umbral y no se habían avisado. Devuelve el registro actualizado."""
    keys, subs = load_json_env("NOW_PUSH_KEYS", {}), load_json_env("NOW_PUSH_SUBS", [])
    if os.environ.get("NOW_PUSH_TEST") == "1":
        n = send_pushes(subs, keys, [{"title": "Now · prueba", "body": "Las notificaciones funcionan ✅", "url": "./", "tag": "now-test"}])
        log.info("Prueba de notificación enviada a %d dispositivo(s).", n)
    thr = CFG.get("notify_threshold", 75)
    seen = previous.get("notified")
    first_run = seen is None                                # primera vez con esta función: se registra sin avisar
    seen = {k: v for k, v in (seen or {}).items() if v >= now - CFG["keep_days"] * DAY}
    for old, new in (merged or {}).items():                 # si una historia avisada se fusiona, no se repite el aviso
        if old in seen:
            seen.setdefault(new, seen[old])
    cats = {t["id"]: t for t in topics}
    fresh_enough = now - CFG.get("notify_max_age_hours", 36) * 3600
    pending = [r for r in importance(stories, now, CFG["latest_days"])
               if r["idx"] >= thr and r["s"]["id"] not in seen and r["s"]["c"] in cats]
    if not pending:
        return seen
    if first_run:
        log.info("Primera ejecución con notificaciones: %d historias ≥ %d registradas sin avisar.", len(pending), thr)
        seen.update({r["s"]["id"]: now for r in pending})
        return seen
    to_send = [r for r in pending if r["last"] >= fresh_enough][:CFG.get("notify_max_per_run", 3)]
    messages = []
    for r in to_send:
        c = cats[r["s"]["c"]]
        messages.append({"title": f"{c.get('icon') or '📰'} {c.get('short') or c['name']} · importancia {r['idx']}",
                         "body": r["s"]["t"][:160], "url": f"./#s={r['s']['id']}", "tag": r["s"]["id"], "idx": r["idx"]})
    delivered = send_pushes(subs, keys, messages)
    log.info("Notificaciones: %d historia(s) ≥ %d · %d envío(s) · %d dispositivo(s).", len(messages), thr, delivered, len(subs))
    if delivered or not subs:                               # si había dispositivos y todo falló, se reintenta en la próxima
        seen.update({r["s"]["id"]: now for r in pending})   # también las que no cupieron: no se avisa tarde
    return seen


def main() -> None:
    t0, now = time.time(), int(time.time())
    topics = load_topics()
    previous = load_previous()
    stories = [s for s in previous.get("stories", []) if s.get("a")]
    tasks = url_tasks(topics)
    log.info("%d temas · leyendo %d fuentes…", len(topics), len(tasks))
    fetched = fetch_all(tasks, CFG["max_article_age_hours"])
    log.info("Artículos descargados: %d", sum(len(v) for v in fetched.values()))
    added, created, merged = merge(stories, fetched, topics, now)
    stories = prune(stories, topics, now)
    log.info("Nuevos: %d medios en historias · %d historias nuevas · guardadas: %d", added, created, len(stories))
    notified = notify(stories, topics, previous, now, merged)
    write_outputs(stories, topics, now, notified)
    log.info("Listo en %.0f s", time.time() - t0)


if __name__ == "__main__":
    main()
