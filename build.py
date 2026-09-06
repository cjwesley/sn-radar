#!/usr/bin/env python3
"""SN Radar: pull ServiceNow ecosystem content into one static dashboard.

Reads sources.json, fetches every feed, normalizes items, merges with the
previously saved items.json so history accumulates past what each feed
exposes, then renders docs/index.html from template.html.

Usage:
    python3 build.py            # full build
    python3 build.py --dry-run  # fetch and report, write nothing
"""

import json
import re
import sys
import time
import html as htmlmod
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

import feedparser

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
CONFIG_PATH = ROOT / "sources.json"
TEMPLATE_PATH = ROOT / "template.html"
ITEMS_PATH = DOCS / "items.json"
STATUS_PATH = DOCS / "status.json"
INDEX_PATH = DOCS / "index.html"

TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                   "utm_content", "fbclid", "gclid", "mc_cid", "mc_eid", "ref"}

STOPWORDS = set("""
a an the and or of to in on for with from by at as is are was were be been being
inc corp co ltd llc says say said report reports announces announced announce today
first best top ways way big giant small step next show shows expects expect right
official officially customers customer company companies business enterprise
this that these those it its into over under about after before how what why when
where who which your you we our us they their them he she his her i my me new
now via vs than then also just more most less can will would should could may might
do does did done get got make made use using used how-to tips tip guide part episode
ep week weekly update updates news blog post video series live session sessions
servicenow service now platform ai s t re ll d 2024 2025 2026 v1 v2 one two three
""".split())


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def fetch_bytes(url, ua, timeout):
    req = Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.status


def resolve_community_key(cfg):
    """Khoros embeds a rotating cache key in its RSS paths. Discover it live."""
    settings = cfg["settings"]
    fallback = settings.get("community_cache_key_fallback", "")
    try:
        body, _ = fetch_bytes("https://www.servicenow.com/community/",
                              settings["user_agent"], settings["request_timeout_seconds"])
        m = re.search(rb"/community/s/([A-Za-z0-9]+)/rss/", body)
        if m:
            key = m.group(1).decode()
            log(f"community cache key: {key}")
            return key
    except Exception as exc:  # noqa: BLE001
        log(f"community key discovery failed: {exc}")
    log(f"community cache key: using fallback {fallback}")
    return fallback


def clean_url(url):
    if not url:
        return ""
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if k.lower() not in TRACKING_PARAMS]
    return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/") or "/",
                       p.params, urlencode(q), ""))


def strip_html(text, limit=320):
    if not text:
        return ""
    text = re.sub(r"<(script|style).*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = htmlmod.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        cut = text[:limit].rsplit(" ", 1)[0]
        text = cut + "..."
    return text


def entry_datetime(entry):
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        val = entry.get(key)
        if val:
            try:
                return datetime.fromtimestamp(time.mktime(val), tz=timezone.utc)
            except (OverflowError, ValueError):
                continue
    return None


def entry_thumbnail(entry):
    thumbs = entry.get("media_thumbnail") or []
    if thumbs and thumbs[0].get("url"):
        return thumbs[0]["url"]
    img = entry.get("image")
    if isinstance(img, dict) and img.get("href"):
        return img["href"]
    return ""


def entry_summary(entry):
    if entry.get("media_description"):
        return strip_html(entry["media_description"])
    if entry.get("summary"):
        return strip_html(entry["summary"])
    content = entry.get("content") or []
    if content and content[0].get("value"):
        return strip_html(content[0]["value"])
    return ""


def entry_duration(entry):
    d = entry.get("itunes_duration")
    if not d:
        return ""
    d = str(d).strip()
    if d.isdigit():
        secs = int(d)
        return f"{secs // 3600}h {(secs % 3600) // 60}m" if secs >= 3600 else f"{secs // 60}m"
    return d


def normalize_news_title(title):
    """Google News appends ' - Publisher' to titles. Split it out."""
    if " - " in title:
        head, tail = title.rsplit(" - ", 1)
        if 0 < len(tail) <= 60:
            return head.strip(), tail.strip()
    return title.strip(), ""


def fetch_source(src, cfg, community_key):
    settings = cfg["settings"]
    url = src["url"].replace("{community_key}", community_key)
    status = {"id": src["id"], "name": src["name"], "url": url, "ok": False,
              "items": 0, "error": "", "fetched_at": datetime.now(timezone.utc).isoformat()}
    try:
        body, code = fetch_bytes(url, settings["user_agent"], settings["request_timeout_seconds"])
    except HTTPError as exc:
        status["error"] = f"HTTP {exc.code}"
        return [], status
    except (URLError, TimeoutError, OSError) as exc:
        status["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return [], status

    parsed = feedparser.parse(body)
    if parsed.bozo and not parsed.entries:
        status["error"] = f"parse error: {getattr(parsed, 'bozo_exception', 'unknown')}"[:200]
        return [], status

    items = []
    for entry in parsed.entries:
        link = entry.get("link") or ""
        if not link:
            # Some podcast hosts publish only an enclosure, no page link.
            for l in entry.get("links") or []:
                if l.get("href"):
                    link = l["href"]
                    break
        if not link and str(entry.get("id", "")).startswith("http"):
            link = entry["id"]
        link = clean_url(link)
        title = (entry.get("title") or "").strip()
        if not link or not title:
            continue
        via = ""
        if src["type"] == "news":
            title, via = normalize_news_title(title)
        dt = entry_datetime(entry)
        summary = entry_summary(entry)
        if src["type"] == "news" and summary.lower().startswith(title.lower()[:40]):
            summary = ""
        items.append({
            "id": link,
            "title": title,
            "url": link,
            "source_id": src["id"],
            "source": src["name"],
            "type": src["type"],
            "group": src.get("group", ""),
            "via": via,
            "author": (entry.get("author") or "").strip(),
            "published": dt.isoformat() if dt else "",
            "summary": summary,
            "thumbnail": entry_thumbnail(entry),
            "duration": entry_duration(entry),
            "first_seen": datetime.now(timezone.utc).isoformat(),
        })
    status["ok"] = True
    status["items"] = len(items)
    return items, status


def load_previous():
    if not ITEMS_PATH.exists():
        return []
    try:
        with open(ITEMS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("items", [])
    except (json.JSONDecodeError, OSError):
        return []


def title_key(title):
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def apply_tags(items, cfg):
    patterns = {name: re.compile(rx, re.I) for name, rx in cfg["settings"].get("tag_patterns", {}).items()}
    for it in items:
        hay = " ".join((it.get("title", ""), it.get("author", ""), it.get("summary", "")))
        it["tags"] = [name for name, rx in patterns.items() if rx.search(hay)]
    return items


def merge_items(new_items, old_items, cfg):
    settings = cfg["settings"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings["max_age_days"])
    excludes = [re.compile(p, re.I) for p in settings.get("exclude_title_patterns", [])]

    by_url = {}
    for it in old_items:
        by_url[it["id"]] = it
    for it in new_items:
        prev = by_url.get(it["id"])
        if prev:
            it["first_seen"] = prev.get("first_seen") or it["first_seen"]
            if not it["published"] and prev.get("published"):
                it["published"] = prev["published"]
        by_url[it["id"]] = it

    seen_titles = {}
    merged = []
    for it in sorted(by_url.values(), key=lambda x: x.get("published") or x.get("first_seen"), reverse=True):
        stamp = it.get("published") or it.get("first_seen")
        try:
            when = datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            when = datetime.now(timezone.utc)
        if when < cutoff:
            continue
        if any(rx.search(it["title"]) for rx in excludes):
            continue
        tk = (it["type"], title_key(it["title"]))
        # Same headline reached through two feeds (a board feed and the all-boards
        # feed, or two outlets syndicating one wire story): keep the newest copy.
        if tk in seen_titles:
            continue
        seen_titles[tk] = True
        merged.append(it)
    return merged[: settings["max_items"]]


def topic_counts(items, days=14, top=24):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    uni, bi = Counter(), Counter()
    uni_src, bi_src = {}, {}
    seen = set()
    for it in items:
        tk = title_key(it["title"])
        if tk in seen:
            continue
        seen.add(tk)
        stamp = it.get("published") or it.get("first_seen")
        try:
            if datetime.fromisoformat(stamp) < cutoff:
                continue
        except (TypeError, ValueError):
            continue
        words = [w for w in re.findall(r"[a-z0-9][a-z0-9+#.-]*", it["title"].lower())
                 if w not in STOPWORDS and len(w) > 2]
        ws = set(words)
        bs = {f"{a} {b}" for a, b in zip(words, words[1:])}
        uni.update(ws)
        bi.update(bs)
        for w in ws:
            uni_src.setdefault(w, set()).add(it["source_id"])
        for b in bs:
            bi_src.setdefault(b, set()).add(it["source_id"])
    # A topic has to show up in several distinct items from more than one source,
    # otherwise one syndicated headline dominates the strip.
    topics = [{"term": t, "count": c} for t, c in bi.most_common(top * 3)
              if c >= 3 and len(bi_src[t]) >= 2][:top // 2]
    have = {t["term"] for t in topics}
    for t, c in uni.most_common(top * 4):
        if len(topics) >= top:
            break
        if c >= 4 and len(uni_src[t]) >= 2 and not any(t in h for h in have):
            topics.append({"term": t, "count": c})
    return topics


def render(items, statuses, cfg):
    tpl = TEMPLATE_PATH.read_text(encoding="utf-8")
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "settings": {"max_age_days": cfg["settings"]["max_age_days"]},
        "sources": [{"id": s["id"], "name": s["name"], "type": s["type"],
                     "group": s.get("group", ""), "home": s.get("home", "")}
                    for s in cfg["sources"] if s.get("enabled", True)],
        "status": statuses,
        "tags": list(cfg["settings"].get("tag_patterns", {}).keys()),
        "topics": topic_counts(items),
        "items": items,
    }
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    out = tpl.replace("/*__DATA__*/null", blob)
    return out, payload


def main():
    dry = "--dry-run" in sys.argv
    cfg = load_config()
    community_key = resolve_community_key(cfg)

    new_items, statuses = [], []
    with ThreadPoolExecutor(max_workers=8) as pool:
        active = [s for s in cfg["sources"] if s.get("enabled", True)]
        futures = {pool.submit(fetch_source, src, cfg, community_key): src for src in active}
        for fut in as_completed(futures):
            src = futures[fut]
            try:
                items, status = fut.result()
            except Exception as exc:  # noqa: BLE001
                items, status = [], {"id": src["id"], "name": src["name"], "url": src["url"],
                                     "ok": False, "items": 0, "error": str(exc)[:200],
                                     "fetched_at": datetime.now(timezone.utc).isoformat()}
            flag = "ok " if status["ok"] else "ERR"
            log(f"[{flag}] {src['name']}: {status['items']} items {status['error']}")
            new_items.extend(items)
            statuses.append(status)

    statuses.sort(key=lambda s: s["name"].lower())
    merged = apply_tags(merge_items(new_items, load_previous(), cfg), cfg)
    log(f"merged: {len(merged)} items ({len(new_items)} fetched this run)")

    if dry:
        return

    DOCS.mkdir(parents=True, exist_ok=True)
    html_out, payload = render(merged, statuses, cfg)
    INDEX_PATH.write_text(html_out, encoding="utf-8")
    ITEMS_PATH.write_text(json.dumps({"generated_at": payload["generated_at"], "items": merged},
                                     ensure_ascii=False, indent=0), encoding="utf-8")
    STATUS_PATH.write_text(json.dumps({"generated_at": payload["generated_at"], "status": statuses},
                                      ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"wrote {INDEX_PATH} ({INDEX_PATH.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
