"""
Garden — Personal Command Center (v1)
Flask app: serves the single-page frontend and the /api/state JSON it renders from.

Milestone 2: Gmail + Calendar are LIVE — pulled read-only via OAuth, normalized into
SQLite, triaged into the front/background meadow, and returned at /api/state. Messages
and News remain labeled placeholders until Milestones 3–4.

Flow per request (PRD §3): connectors pull -> store.replace_source -> triage -> beds.
Resilience (PRD §2): the last good /api/state is cached in SQLite; if a connector is
down or unauthorized, we serve stored items and flag the payload stale rather than
blocking the page on one failing source.
"""

import threading
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, send_from_directory

import distill
import store
import triage
from connectors import gcal, gmail, imessage, news
from connectors.google_auth import NeedsAuth, get_credentials
from connectors.imessage import MessagesUnavailable

load_dotenv()  # ANTHROPIC_API_KEY etc. from .env (git-ignored)

STATIC_DIR = Path(__file__).parent / "static"

# News refresh cadence (PRD §4.4 + build spec: scheduled, NOT on page load).
NEWS_REFRESH_HOURS = 4

app = Flask(__name__, static_folder=None)


EMAIL_CHROME = {"icon": "✉️", "iconBg": "#f7ecd2", "title": "Email"}
MESSAGES_CHROME = {"icon": "\U0001f4ac", "iconBg": "#fae3de", "title": "Messages"}
NEWS_CHROME = {"icon": "\U0001f4f0", "iconBg": "#e2f0e4", "title": "News"}
TODAY_CHROME = {"icon": "\U0001f4c5", "iconBg": "#e8eefb", "title": "Today"}


_news_lock = threading.Lock()  # serialize pulls (scheduler vs. on-load trigger)
_news_inflight = False
_news_inflight_lock = threading.Lock()


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def news_pipeline() -> None:
    """Pull RSS, replace the stored news rows, then distill any new headlines.
    Records the local date of the pull so the on-load trigger can tell a new day.
    Never runs on the request path itself — the scheduler and the on-load trigger
    both invoke it on background threads."""
    with _news_lock:
        store.init_db()
        store.replace_source("news", news.fetch_articles())
        distill.distill_pending()
        store.set_kv("news_fetched_on", _today_str())


def _scheduler_loop() -> None:
    while True:
        try:
            news_pipeline()
        except Exception:
            app.logger.exception("news_pipeline failed")
        time.sleep(NEWS_REFRESH_HOURS * 3600)


def _onload_refresh() -> None:
    global _news_inflight
    try:
        news_pipeline()
    except Exception:
        app.logger.exception("on-load news refresh failed")
    finally:
        with _news_inflight_lock:
            _news_inflight = False


def maybe_refresh_news_on_load() -> None:
    """If the last pull was on an earlier day, kick a background refresh.

    The 4h timer sleeps relative to process start and is paused while the Mac
    sleeps, so an overnight-suspended laptop can wake with stale news. This makes
    the first page-load of a new day trigger a fresh pull. It's fire-and-forget:
    the current response still serves the cache; the new data lands on the next poll.
    """
    global _news_inflight
    if store.get_kv("news_fetched_on") == _today_str():
        return
    with _news_inflight_lock:
        if _news_inflight:
            return
        _news_inflight = True
    threading.Thread(target=_onload_refresh, name="news-onload", daemon=True).start()


_scheduler_started = False


def start_background_jobs() -> None:
    """Start the news scheduler once. Idempotent so repeated calls (or an accidental
    double-import) don't spawn duplicate threads."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    threading.Thread(target=_scheduler_loop, name="news-scheduler", daemon=True).start()


def _meta(stale: bool) -> dict:
    now = datetime.now()
    hour = now.hour
    part = "Morning" if hour < 12 else "Afternoon" if hour < 18 else "Evening"
    # %-d/%-H are platform-specific; build the clock manually to stay portable.
    clock = f"{now.strftime('%a')} · {now.hour}:{now.strftime('%M')}"
    return {
        "clock": clock,
        "greeting": f"{part}, <em>Martin.</em>",
        "weather": "\U0001f331",  # decorative; no weather connector in v1
        "stale": stale,
    }


def build_state() -> dict:
    """Pull live sources, persist, triage, and assemble the frontend payload.

    Each connector is attempted independently. A failure (including not-yet-authorized)
    leaves that source's last-stored items in place and marks the payload stale, so a
    single down source never blanks the whole page.
    """
    store.init_db()

    fresh = True
    try:
        creds = get_credentials(interactive=False)
    except NeedsAuth as e:
        app.logger.warning("Google not authorized: %s", e)
        creds = None
        fresh = False

    if creds is not None:
        for source, fetch in (("email", lambda: gmail.fetch(creds)),
                              ("calendar", lambda: gcal.fetch(creds))):
            try:
                store.replace_source(source, fetch())
            except Exception as e:  # network/API hiccup -> keep last stored items
                app.logger.warning("Pull failed for %s: %s", source, e)
                fresh = False

    # Messages: local read-only pull (PRD §4.3). FDA denial is a persistent config
    # state, not transient staleness, so we surface it via logs and an empty bed
    # rather than flipping the whole page to "stale".
    try:
        store.replace_source("messages", imessage.fetch())
    except MessagesUnavailable as e:
        app.logger.warning("Messages unavailable: %s", e)
    except Exception as e:
        app.logger.warning("Pull failed for messages: %s", e)
        fresh = False

    # Email: only unread Primary-tab threads stand front (read = handled).
    email = triage.triage_actionable(store.get_items("email"), require_unread=True)
    # Messages: an unanswered text older than a week is no longer "needs you".
    messages = triage.triage_actionable(
        store.get_items("messages"), front_window=triage.MESSAGES_FRONT_WINDOW
    )
    events = triage.calendar_events(store.get_items("calendar"))
    # News is read from the cache ONLY — the scheduler pulls + distills it out of band.
    news_list = triage.news_items(store.get_items("news"), store.get_distilled_map())

    state = {
        "meta": _meta(stale=not fresh),
        "beds": {
            "email": {**EMAIL_CHROME, **email},
            "messages": {**MESSAGES_CHROME, **messages},
            "news": {**NEWS_CHROME, "items": news_list},
            "today": {**TODAY_CHROME, "events": events},
        },
    }

    if fresh:
        store.set_kv("last_state", state)  # cache the last good payload
    return state


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "garden-mockup.html")


@app.route("/api/state")
def api_state():
    maybe_refresh_news_on_load()  # first load of a new day → background refresh
    try:
        return jsonify(build_state())
    except Exception as e:  # last-resort fallback to the cached good payload
        app.logger.exception("build_state failed")
        cached = store.get_kv("last_state")
        if cached:
            cached["meta"]["stale"] = True
            return jsonify(cached)
        return jsonify({"meta": _meta(stale=True), "beds": {}, "error": str(e)}), 200


if __name__ == "__main__":
    # Local only. Debug on for development; this never gets deployed (PRD §2, §10).
    # use_reloader=False: the reloader would fork a second process and a duplicate
    # news scheduler. start_background_jobs is idempotent, but one process is cleaner.
    start_background_jobs()
    app.run(host="127.0.0.1", port=5001, debug=True, use_reloader=False)
