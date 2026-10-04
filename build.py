#!/usr/bin/env python3
"""
Now — construye los datos de la app de noticias.

En cada ejecución:
  1. Descarga el histórico de 30 días ya publicado en la web (data/archive.json).
     El histórico vive en la propia web: no hace falta base de datos.
  2. Lee las fuentes de cada tema de topics.json: feeds RSS y búsquedas de
     Google News.
  3. Añade cada artículo nuevo a la historia que ya cuenta lo mismo (titular
     parecido en los últimos días) o crea una historia nueva.
  4. Poda: borra lo que tiene más de 30 días, lo de temas eliminados y se
     queda con las historias más cubiertas de cada tema y día.
  5. Calcula el índice de importancia de cada historia y envía una notificación
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
import logging
import os
import re
import sys
import time
from collections import defaultdict
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


# ─── Agrupación ─────────────────────────────────────────────────────────────

def merge(stories: list[dict], fetched: dict[str, list[dict]], topics: list[dict], now: int) -> tuple[int, int]:
    thr, window = CFG["cluster_threshold"], CFG["match_window_days"] * DAY
    max_age, max_links = CFG["max_article_age_hours"] * 3600, CFG["max_links_per_story"]
    by_topic: dict[str, list[dict]] = defaultdict(list)
    for s in stories:
        s["_kw"] = set(s.get("kw", []))
        by_topic[s["c"]].append(s)

    added = created = 0
    for t in topics:
        pool = by_topic[t["id"]]
        arts = sorted(fetched.get(t["id"], []), key=lambda a: to_epoch(a.get("pub_date")) or now)
        for art in arts:
            title, src = (art.get("title") or "").strip(), (art.get("source") or "").strip()
            if not title or not src:
                continue
            ts = min(to_epoch(art.get("pub_date")) or now, now)
            if now - ts > max_age:
                continue
            kw = keywords(title)
            if len(kw) < 2:
                continue
            best, best_sim = None, 0.0
            for s in pool:
                if now - s["last"] <= window:
                    sim = jaccard(kw, s["_kw"])
                    if sim > thr and sim > best_sim:
                        best, best_sim = s, sim
            link = art.get("link") or ""
            if best is None:
                best = {"id": hashlib.sha1((link or title).encode()).hexdigest()[:10], "c": t["id"], "t": title,
                        "lang": detect_lang(title + " " + art.get("summary", "")), "aff": affinity(title),
                        "img": art.get("image") or "", "a": [], "_kw": set(kw), "first": ts, "last": ts}
                pool.append(best); stories.append(best); created += 1
            sm = tidy_summary(art.get("summary", ""), title)
            if better_summary(best.get("sm", ""), sm):
                best["sm"] = sm                            # resumen más completo de entre los medios
            if any(a[0] == src for a in best["a"]):
                continue                                   # cada medio cuenta una vez
            best["a"].append([src, ts, link if sum(1 for a in best["a"] if a[2]) < max_links else ""])
            best["a"].sort(key=lambda a: a[1])
            best["first"], best["last"] = best["a"][0][1], best["a"][-1][1]
            if not best["img"] and art.get("image"):
                best["img"] = art["image"]
            if len(best["_kw"]) < 40:
                best["_kw"] |= kw
            added += 1
    return added, created


def prune(stories: list[dict], topics: list[dict], now: int) -> list[dict]:
    ids = {t["id"] for t in topics}
    keep_from = now - CFG["keep_days"] * DAY
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for s in stories:
        if s["a"] and s["c"] in ids and s["last"] >= keep_from:
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
        "notify_threshold": CFG.get("notify_threshold", 75),
    }

    def public(s: dict, with_kw: bool) -> dict:
        d = {k: s[k] for k in ("id", "c", "t", "lang", "aff", "img", "a")}
        if s.get("sm"):
            d["sm"] = s["sm"]
        if with_kw:
            d["kw"] = sorted(s["_kw"])
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

# ─── Índice de importancia (mismo cálculo que la app) ───────────────────────

def importance(stories: list[dict], now: int, window_days: int) -> list[dict]:
    """Índice 0–100 de cada historia dentro de la ventana (por defecto, 7 días):
    40 % cobertura ponderada + 15 % velocidad + 15 % diversidad + 15 % frescura + 15 % afinidad."""
    start = now - window_days * DAY
    authority = [a.lower() for a in CFG["authority_sources"]]
    is_auth = lambda name: any(a in name.lower() for a in authority)
    rows = []
    for s in stories:
        m = sorted((x for x in s["a"] if start <= x[1] <= now), key=lambda x: x[1])
        if not m:
            continue
        names = list(dict.fromkeys(x[0] for x in m))
        auth = sum(1 for n in names if is_auth(n))
        first, last = m[0][1], m[-1][1]
        rows.append({
            "s": s, "last": last, "cov": len(names), "wcov": len(names) + 0.5 * auth,
            "vel": sum(1 for x in m if x[1] - first <= DAY) / len(m),
            "div": 0.5 * min(1, (len(names) - 1) / 4) + (0.5 if auth else 0),
            "fresh": max(0.0, 1 - (now - last) / (7 * DAY)),
            "afi": min(1.0, (s.get("aff") or 0) / 2),
        })
    max_w = max([1.0] + [r["wcov"] for r in rows])
    for r in rows:
        r["idx"] = int(100 * (.40 * r["wcov"] / max_w + .15 * r["vel"] + .15 * r["div"] + .15 * r["fresh"] + .15 * r["afi"]) + .5)
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


def notify(stories: list[dict], topics: list[dict], previous: dict, now: int) -> dict:
    """Avisa de las historias que superan el umbral y no se habían avisado. Devuelve el registro actualizado."""
    keys, subs = load_json_env("NOW_PUSH_KEYS", {}), load_json_env("NOW_PUSH_SUBS", [])
    if os.environ.get("NOW_PUSH_TEST") == "1":
        n = send_pushes(subs, keys, [{"title": "Now · prueba", "body": "Las notificaciones funcionan ✅", "url": "./", "tag": "now-test"}])
        log.info("Prueba de notificación enviada a %d dispositivo(s).", n)
    thr = CFG.get("notify_threshold", 75)
    seen = previous.get("notified")
    first_run = seen is None                                # primera vez con esta función: se registra sin avisar
    seen = {k: v for k, v in (seen or {}).items() if v >= now - CFG["keep_days"] * DAY}
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
    stories = previous.get("stories", [])
    for s in stories:
        s["first"], s["last"] = s["a"][0][1], s["a"][-1][1]
    tasks = url_tasks(topics)
    log.info("%d temas · leyendo %d fuentes…", len(topics), len(tasks))
    fetched = fetch_all(tasks, CFG["max_article_age_hours"])
    log.info("Artículos descargados: %d", sum(len(v) for v in fetched.values()))
    added, created = merge(stories, fetched, topics, now)
    stories = prune(stories, topics, now)
    log.info("Nuevos: %d artículos · %d historias · guardadas: %d", added, created, len(stories))
    notified = notify(stories, topics, previous, now)
    write_outputs(stories, topics, now, notified)
    log.info("Listo en %.0f s", time.time() - t0)


if __name__ == "__main__":
    main()
