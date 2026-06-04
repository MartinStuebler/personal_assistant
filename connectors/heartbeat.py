"""
Heartbeat connector — price history from a private GitHub repo (dashboard chart).

Reads prices.log from MartinStuebler/github-heartbeat@main via the GitHub REST contents
endpoint. Each line is `UTC_timestamp,ticker,price` (e.g. 2026-06-04T14:12Z,SPY,753.32).
We keep the SPY series for the dashboard's price-history chart.

Auth: a fine-grained token read from the environment as HEARTBEAT_GH_TOKEN (loaded by
app.py's load_dotenv()). The token is NEVER logged, printed, or returned — it only ever
travels in the request Authorization header, server-side. The browser hits /api/prices
and never sees the token.

Resilience (matches the connectors' house style): a missing token or a failed request
raises; the route degrades to a small "price data unavailable" note rather than crashing.
A short in-memory cache keeps repeated dashboard loads from hammering the GitHub API.
"""

import os
import time

import requests

REPO = "MartinStuebler/github-heartbeat"
PATH = "prices.log"
REF = "main"
TICKER = "SPY"

GITHUB_TIMEOUT = 10          # seconds; a slow API must not hang the page
CACHE_TTL = 3 * 60           # serve cached points for a few minutes between pulls

# module-level cache: (fetched_at_epoch, points). Process-local, fine for a single-user
# local app — mirrors the rest of the app's "good enough, keep it simple" resilience.
_cache: tuple[float, list[dict]] | None = None


class HeartbeatUnavailable(RuntimeError):
    """Token missing or the GitHub fetch failed — caller should degrade gracefully."""


def _parse(text: str) -> list[dict]:
    """Parse `ts,ticker,price` lines, keeping only TICKER, in file order.
    Bad/short lines and unparseable prices are skipped, not fatal."""
    points: list[dict] = []
    for line in text.splitlines():
        parts = line.strip().split(",")
        if len(parts) != 3:
            continue
        ts, ticker, price = (p.strip() for p in parts)
        if ticker != TICKER:
            continue
        try:
            value = float(price)
        except ValueError:
            continue
        points.append({"t": ts, "price": value})
    return points


def fetch_prices(now: float | None = None) -> list[dict]:
    """Return the SPY price series as [{"t","price"}], newest-cache-aware.

    Raises HeartbeatUnavailable if the token is absent or the request fails. On success
    the result is cached for CACHE_TTL seconds and reused for subsequent calls.
    """
    global _cache
    now = now if now is not None else time.time()

    if _cache is not None and now - _cache[0] < CACHE_TTL:
        return _cache[1]

    token = os.getenv("HEARTBEAT_GH_TOKEN")
    if not token:
        raise HeartbeatUnavailable("HEARTBEAT_GH_TOKEN not set")

    url = f"https://api.github.com/repos/{REPO}/contents/{PATH}"
    try:
        resp = requests.get(
            url,
            params={"ref": REF},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.raw+json",  # raw file body, not base64 JSON
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=GITHUB_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        # Note: never include the token in the message; requests doesn't echo headers here.
        raise HeartbeatUnavailable(f"GitHub fetch failed: {e}") from e

    points = _parse(resp.text)
    _cache = (now, points)
    return points
