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

import os
import threading
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

import distill
import store
import triage
from connectors import gcal, gmail, heartbeat, imessage, news
from connectors.google_auth import NeedsAuth, get_credentials
from connectors.heartbeat import HeartbeatUnavailable
from connectors.imessage import MessagesUnavailable

load_dotenv()  # ANTHROPIC_API_KEY etc. from .env (git-ignored)

STATIC_DIR = Path(__file__).parent / "static"

# News refresh cadence (PRD §4.4 + build spec: scheduled, NOT on page load).
NEWS_REFRESH_HOURS = 4

app = Flask(__name__, static_folder=None)

# Ensure the SQLite schema exists at startup. garden.db is git-ignored, so a fresh
# server (e.g. Railway) starts with no database; the first request would otherwise hit
# `kv`/`items` before any init_db() and crash with "no such table". init_db() is
# idempotent (CREATE TABLE IF NOT EXISTS), so locally — where the tables already
# exist — this is a no-op and behavior is unchanged. Runs under gunicorn too, since
# the __main__ block below doesn't execute there.
store.init_db()


# ---------------------------------------------------------------------------
# Auth gate (security-critical). Single-user password login in front of every
# route. The app is intended to go to a public URL later, so an unauthenticated
# visitor must reach NOTHING but the login page. Two secrets come from the env
# (.env via load_dotenv): FLASK_SECRET_KEY signs the session cookie; GARDEN_PASSWORD
# is the login password. Both are mandatory — if either is missing the app refuses
# to start rather than falling open to no-auth. We never log/print either value;
# the password is held only as a Werkzeug hash and compared in constant time.
# ---------------------------------------------------------------------------

# how a failed login is slowed / locked, in-memory (single-user app; per-process).
FAILED_LOGIN_DELAY = 0.75   # seconds added to every failed attempt
LOCKOUT_THRESHOLD = 5       # consecutive failures before a cooldown kicks in
LOCKOUT_SECONDS = 60        # length of that cooldown
_login_failures = 0
_lockout_until = 0.0
_login_lock = threading.Lock()


def _configure_auth() -> str:
    """Read the two required secrets from the env, wire up the session signing key,
    and return the password HASH. Raises (refusing startup) if either is unset, so
    the app never runs without auth. Neither secret is logged or returned in plaintext."""
    secret = os.getenv("FLASK_SECRET_KEY")
    if not secret:
        raise RuntimeError(
            "FLASK_SECRET_KEY is not set — refusing to start. Add it to .env "
            "(see .env.example). The login gate will not run without it."
        )
    password = os.getenv("GARDEN_PASSWORD")
    if not password:
        raise RuntimeError(
            "GARDEN_PASSWORD is not set — refusing to start. Add it to .env "
            "(see .env.example). The app will not fall open to no-auth."
        )
    app.secret_key = secret
    # HttpOnly + SameSite=Lax always. SESSION_COOKIE_SECURE (cookie only sent over
    # HTTPS) is enabled in production and left off for local http dev, decided by the
    # PRODUCTION env flag. Set PRODUCTION=1 in deployment (Railway serves over HTTPS);
    # unset locally so the session cookie still works over http://127.0.0.1.
    production = os.getenv("PRODUCTION", "").lower() in ("1", "true", "yes")
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=production,
    )
    return generate_password_hash(password)


PASSWORD_HASH = _configure_auth()

# Endpoints reachable while logged out. Deny-by-default: only the login view is
# public, so every other route — current and future — is gated automatically.
PUBLIC_ENDPOINTS = {"login"}


@app.before_request
def _require_login():
    """Gate every request. Allow the login view and already-authenticated sessions
    through; redirect everything else to /login with no body, so an unauthenticated
    request leaks nothing (no partial JSON, no internal error)."""
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if session.get("authed"):
        return None
    return redirect(url_for("login"))


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

    # Messages: local read-only pull (PRD §4.3). If the pull fails (FDA denied, or
    # running on a host with no chat.db at all, e.g. Railway), we keep the last-stored
    # rows — but those can be days old. An empty front over frozen data must NOT read
    # as the calm "nothing needs you"; we flag the bed stale so the UI shows a warning
    # instead. A failed pull doesn't flip the whole page stale (it's a per-bed state).
    messages_stale = False
    try:
        store.replace_source("messages", imessage.fetch())
    except MessagesUnavailable as e:
        app.logger.warning("Messages unavailable: %s", e)
        messages_stale = True
    except Exception as e:
        app.logger.warning("Pull failed for messages: %s", e)
        messages_stale = True

    # Email: "real people only" — suppress no-reply/notifications addresses, Gmail
    # Promotions/Updates categories, and bulk-list senders (List-Unsubscribe /
    # Precedence:bulk) so only person-to-person mail reaches the bed. The surviving
    # humans keep the raw front/meadow split (inbound = front, replied = meadow).
    human_email, suppressed_email = gmail.partition_humans(store.get_items("email"))
    if suppressed_email:
        app.logger.info("Email filter: hid %d non-human sender(s)", len(suppressed_email))
    email = triage.triage_actionable(
        human_email,
        require_unread=False,
        apply_mute=False,
        background_window=None,
    )
    # Messages: an unanswered text older than a week is no longer "needs you".
    messages = triage.triage_actionable(
        store.get_items("messages"), front_window=triage.MESSAGES_FRONT_WINDOW
    )
    if messages_stale:
        last = store.source_updated_at("messages")
        if last:
            d = datetime.fromtimestamp(last)
            # %-d/%-I are platform-specific (see _meta); build it portably.
            hour12 = d.hour % 12 or 12
            synced = f"{d.strftime('%b')} {d.day}, {hour12}:{d.strftime('%M %p')}"
        else:
            synced = "never"
        messages["stale"] = True
        messages["hint"] = f"Can’t read Messages right now — last synced {synced}."
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


def _render_login(error: str = "", status: int = 200):
    template = (STATIC_DIR / "login.html").read_text(encoding="utf-8")
    return render_template_string(template, error=error), status


@app.route("/login", methods=["GET", "POST"])
def login():
    """Single-field password login. On success, set the session flag and send the
    user to the dashboard. Failed attempts are slowed by a fixed delay and locked out
    after several in a row (in-memory) to blunt brute-forcing. The submitted password
    and the stored hash are never logged."""
    global _login_failures, _lockout_until

    if request.method == "GET":
        if session.get("authed"):
            return redirect(url_for("index"))
        return _render_login()

    # POST — under cooldown? reject without even checking the password.
    now = time.time()
    with _login_lock:
        locked = now < _lockout_until
    if locked:
        return _render_login("Too many attempts — wait a minute and try again.", 429)

    password = request.form.get("password", "")
    if check_password_hash(PASSWORD_HASH, password):
        with _login_lock:
            _login_failures = 0
            _lockout_until = 0.0
        session.clear()
        session["authed"] = True
        return redirect(url_for("index"))

    # wrong password: slow it down, count it, maybe start a cooldown.
    time.sleep(FAILED_LOGIN_DELAY)
    with _login_lock:
        _login_failures += 1
        if _login_failures >= LOCKOUT_THRESHOLD:
            _lockout_until = time.time() + LOCKOUT_SECONDS
            _login_failures = 0
    app.logger.warning("Failed login attempt")  # note: never logs the password
    return _render_login("Incorrect password.", 401)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "garden-mockup.html")


@app.route("/api/prices")
def api_prices():
    """SPY price history for the dashboard chart, pulled server-side from the private
    heartbeat repo so the GitHub token never reaches the browser. Degrades gracefully:
    a missing token or failed fetch returns an 'unavailable' flag (HTTP 200), not a 500,
    so the chart area shows a small note instead of breaking the page."""
    try:
        return jsonify({"ticker": heartbeat.TICKER, "points": heartbeat.fetch_prices()})
    except HeartbeatUnavailable as e:
        app.logger.warning("Price history unavailable: %s", e)
        return jsonify({"ticker": heartbeat.TICKER, "points": [], "unavailable": True})
    except Exception:
        app.logger.exception("api_prices failed")
        return jsonify({"ticker": heartbeat.TICKER, "points": [], "unavailable": True})


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
