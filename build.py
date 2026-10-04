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
  5. Escribe site/data/latest.json (7 días) y site/data/archive.json (30 días).

Variables de entorno:
  NOW_SITE_URL      URL pública de la app (la pone el workflow automáticamente)
  NOW_API_URL       URL de la función de Supabase (opcional, para los ajustes)
  NOW_SKIP_DOWNLOAD =1 para empezar sin histórico (pruebas)
"""
from __future__ import annotations

import hashlib
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
from urllib.parse import quote_plus

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
                "summary": clean_html(getattr(e, "summary", ""))[:400],
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


def write_outputs(stories: list[dict], topics: list[dict], now: int) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    meta = {
        "updated": now,
        "api_url": os.environ.get("NOW_API_URL", "").strip() or CFG.get("api_url", ""),
        "categories": [{"id": t["id"], "name": t["name"], "short": t.get("short") or t["name"].split()[0],
                        "icon": t.get("icon") or "📰", "color": t.get("color"),
                        "sources": len(t["feeds"]), "queries": len(t["queries"])} for t in topics],
        "authority": CFG["authority_sources"],
    }

    def public(s: dict, with_kw: bool) -> dict:
        d = {k: s[k] for k in ("id", "c", "t", "lang", "aff", "img", "a")}
        if with_kw:
            d["kw"] = sorted(s["_kw"])
        return d

    latest_from = now - CFG["latest_days"] * DAY
    files = {
        "latest.json": {**meta, "days": CFG["latest_days"], "stories": [public(s, False) for s in stories if s["last"] >= latest_from]},
        "archive.json": {**meta, "days": CFG["keep_days"], "stories": [public(s, True) for s in stories]},
    }
    for name, payload in files.items():
        path = DATA_DIR / name
        path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        log.info("%s: %d historias · %.0f KB", name, len(payload["stories"]), path.stat().st_size / 1024)


def main() -> None:
    t0, now = time.time(), int(time.time())
    topics = load_topics()
    stories = load_previous().get("stories", [])
    for s in stories:
        s["first"], s["last"] = s["a"][0][1], s["a"][-1][1]
    tasks = url_tasks(topics)
    log.info("%d temas · leyendo %d fuentes…", len(topics), len(tasks))
    fetched = fetch_all(tasks, CFG["max_article_age_hours"])
    log.info("Artículos descargados: %d", sum(len(v) for v in fetched.values()))
    added, created = merge(stories, fetched, topics, now)
    stories = prune(stories, topics, now)
    log.info("Nuevos: %d artículos · %d historias · guardadas: %d", added, created, len(stories))
    write_outputs(stories, topics, now)
    log.info("Listo en %.0f s", time.time() - t0)


if __name__ == "__main__":
    main()
