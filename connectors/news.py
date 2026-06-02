"""
News connector — RSS pull for my four tracks (PRD §4.4).

Informational only: no front/background mechanic. Pull last 24h, dedupe by title,
cap per-track and overall. Headlines + source. The 'source' label shown in the UI
is the track name (matches the mockup: "Industrial AI", "Fintech", …).

Distillation of headlines is handled separately (distill.py), on a schedule — this
module only fetches raw articles. Article ids are a stable hash of the link so the
distill cache can key to them across pulls (PRD: summarize each article once, ever).

FEEDS is the tunable knob (PRD §12 open question). Edit freely as tracks are refined.
"""

import calendar
import hashlib
import html
import re
import socket
import time

import feedparser

_TAG_RE = re.compile(r"<[^>]+>")


def _clean_title(raw: str) -> str:
    """Some feeds (e.g. Fierce Healthcare) wrap the title in an <a> tag.
    Strip markup and unescape entities so we store plain text."""
    return html.unescape(_TAG_RE.sub("", raw or "")).strip()

# track -> list of RSS feed URLs. Kept small and free; tune over the first week.
FEEDS: dict[str, list[str]] = {
    "Industrial AI": [
        "https://venturebeat.com/category/ai/feed/",
        "https://www.therobotreport.com/feed/",
    ],
    "Fintech": [
        "https://techcrunch.com/category/fintech/feed/",
        "https://www.finextra.com/rss/headlines.aspx",
    ],
    "Healthcare": [
        "https://www.fiercehealthcare.com/rss/xml",
        "https://medcitynews.com/feed/",
    ],
    "Materials": [
        "https://www.sciencedaily.com/rss/matter_energy/materials_science.xml",
        "https://phys.org/rss-feed/chemistry-news/materials-science/",
    ],
}

FRESH_WINDOW = 24 * 3600   # PRD §4.4: keep last 24h
PER_TRACK_CAP = 3          # balance across tracks before the overall cap
OVERALL_CAP = 8            # "a handful of headlines per refresh"
FEED_TIMEOUT = 10          # seconds per feed; a slow feed must not hang the pull


def _article_id(link: str, title: str) -> str:
    """Stable id from the link (fallback to title) so the distill cache persists
    across pulls even though the news rows are replaced each refresh."""
    basis = (link or title or "").strip().lower()
    return "news_" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def _entry_ts(entry) -> float | None:
    pub = entry.get("published_parsed") or entry.get("updated_parsed")
    return calendar.timegm(pub) if pub else None  # feed times are UTC struct_time


def fetch_articles(now: int | None = None) -> list[dict]:
    now = now or int(time.time())
    socket.setdefaulttimeout(FEED_TIMEOUT)

    collected: list[dict] = []
    seen_titles: set[str] = set()

    for track, urls in FEEDS.items():
        track_count = 0
        for url in urls:
            if track_count >= PER_TRACK_CAP:
                break
            try:
                parsed = feedparser.parse(url)
            except Exception:
                continue  # a broken feed is skipped, not fatal

            for entry in parsed.entries:
                if track_count >= PER_TRACK_CAP:
                    break
                title = _clean_title(entry.get("title"))
                link = (entry.get("link") or "").strip()
                if not title:
                    continue

                ts = _entry_ts(entry)
                if ts is not None and now - ts > FRESH_WINDOW:
                    continue  # older than 24h
                ts = ts or now  # undated entry: treat as just-seen

                key = title.lower()
                if key in seen_titles:
                    continue
                seen_titles.add(key)

                collected.append(
                    {
                        "id": _article_id(link, title),
                        "who": track,          # shown as the source label
                        "preview": title,      # original headline (distill overrides at render)
                        "ts": int(ts),
                        "last_from_me": None,
                        "unread": None,
                        "extra": {"url": link, "track": track},
                    }
                )
                track_count += 1

    collected.sort(key=lambda a: a["ts"], reverse=True)
    return collected[:OVERALL_CAP]
