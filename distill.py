"""
News headline distillation — Claude Haiku 4.5, on a schedule, cached forever.

Per the build spec:
  - Cheapest capable model (claude-haiku-4-5); fine for one-line headline rewriting.
  - Runs on the background scheduler (2–3×/day), NEVER on page load.
  - Each article is summarized ONCE, EVER: a successful line is cached in SQLite
    (store.news_distill) keyed by the stable article id and never recomputed.
  - Graceful fallback: if the key is missing, the call errors, or the result is junk
    (empty or > 20 words), we cache NOTHING and the page falls back to the original
    headline. Junk is never persisted, so the article is retried next cycle.

Key comes from ANTHROPIC_API_KEY (.env, git-ignored), loaded by app.py at startup.
"""

import logging
import os

import store

log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5"
MAX_WORDS = 20

# Cached so the system block is a prompt-cache hit across the many small per-article
# calls in a single distillation run (claude-api best practice).
_SYSTEM = [
    {
        "type": "text",
        "text": (
            "You rewrite a news headline into one calm, plain-language line for a "
            "personal morning dashboard. Strictly under 20 words. No clickbait, no "
            "trailing punctuation, no quotation marks, no source/byline, no emoji. "
            "Preserve the concrete fact. Output only the rewritten line."
        ),
        "cache_control": {"type": "ephemeral"},
    }
]


def _client():
    """Return an Anthropic client, or None if no key / SDK unavailable.
    Returning None (rather than raising) keeps distillation a no-op so the news bed
    simply falls back to raw headlines."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        from anthropic import Anthropic
    except ImportError:
        return None
    return Anthropic()


def _valid(line: str) -> bool:
    return bool(line) and 0 < len(line.split()) <= MAX_WORDS


def _distill_one(client, title: str, source: str) -> str | None:
    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=64,
            system=_SYSTEM,
            messages=[{"role": "user", "content": f"Track: {source}\nHeadline: {title}"}],
        )
        line = "".join(b.text for b in msg.content if b.type == "text").strip().strip('"')
    except Exception as e:
        log.warning("distill call failed for %r: %s", title[:50], e)
        return None
    return line if _valid(line) else None


def distill_pending() -> dict:
    """Summarize any cached news article that has no distilled line yet.
    Returns a small stats dict for logging. Safe to call repeatedly."""
    client = _client()
    if client is None:
        log.info("distill: no ANTHROPIC_API_KEY — skipping (news falls back to headlines)")
        return {"skipped": True, "distilled": 0, "pending": 0}

    articles = store.get_items("news")
    done = store.get_distilled_map()
    pending = [a for a in articles if a["id"] not in done]

    distilled = 0
    for a in pending:
        line = _distill_one(client, a.get("preview") or "", a.get("who") or "")
        if line:
            store.set_distilled(a["id"], line)
            distilled += 1
        # junk/failure: cache nothing -> retried next cycle, headline shown meanwhile

    log.info("distill: %d/%d newly summarized", distilled, len(pending))
    return {"skipped": False, "distilled": distilled, "pending": len(pending)}


if __name__ == "__main__":
    # Manual one-off run (after a news pull). Mostly handled by the scheduler.
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    store.init_db()
    print(distill_pending())
