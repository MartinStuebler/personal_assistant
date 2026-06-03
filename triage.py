"""
Triage — derive front (needs me) vs background (handled meadow) and staleness,
then assemble the exact JSON shape the frontend renders from (PRD §6/§7).

Front/background (email & messages):
  FRONT       last message inbound (last_from_me == 0), not automated/muted, and
              — per bed — recent enough (front_window) and unread (require_unread).
  BACKGROUND  I replied (last_from_me == 1) within the window, OR it's muted/automated,
              OR it's an inbound thread that aged out / was already read.
              The meadow is intentionally generous — a record of the week's effort.

Staleness (front items only):
  age > 48h   -> 'alert'   (wilts, red bud)
  18h–48h     -> 'wilt'
  younger     -> 'normal'

Per-bed front gates (passed in by the caller, not hardcoded globally):
  front_window    inbound only counts as "needs you" within this many seconds
                  (messages: 7d — an old unanswered text is no longer actionable;
                  email: None — no recency cutoff).
  require_unread  only unread inbound stands front (email: True; messages: their
                  unread state isn't reliable per-chat, so False).

Background window: keep last 7 days so the meadow is a rolling weekly record.
Render cap is applied in the frontend (badge still shows the true front count).
"""

import time

ALERT_AGE = 48 * 3600
WILT_AGE = 18 * 3600
BACKGROUND_WINDOW = 7 * 24 * 3600

# Messages: a text you never replied to stops being "needs you" after a week.
MESSAGES_FRONT_WINDOW = 7 * 24 * 3600

# Escape hatch (PRD §6). Substring match against the sender/contact name, lowercase.
# Expected to be tuned over the first week of real use; empty is a fine default.
MUTE_SENDERS: set[str] = set()


def _age_label(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 3600:
        return f"{max(1, s // 60)}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _level(age: float) -> str:
    if age > ALERT_AGE:
        return "alert"
    if age > WILT_AGE:
        return "wilt"
    return "normal"


def _is_muted(item: dict) -> bool:
    if (item.get("extra") or {}).get("automated"):
        return True
    who = (item.get("who") or "").lower()
    return any(m in who for m in MUTE_SENDERS)


def triage_actionable(
    items: list[dict],
    now: int | None = None,
    front_window: int | None = None,
    require_unread: bool = False,
    apply_mute: bool = True,
    background_window: int | None = BACKGROUND_WINDOW,
) -> dict:
    """Split email/messages items into {front, background} per PRD §6.

    front_window / require_unread are per-bed gates (see module docstring): an
    inbound item that fails either still recedes into the background meadow if it's
    within BACKGROUND_WINDOW, rather than vanishing.

    apply_mute / background_window let a bed opt out of the extra filtering: the
    email bed runs a raw baseline (apply_mute=False, background_window=None) so the
    complete inbox shows with nothing hidden — every inbound thread is a front
    plant, every already-replied thread is meadow, regardless of category, read
    state, sender, or age.
    """
    now = now or int(time.time())
    front, background = [], []

    for it in items:
        age = now - (it.get("ts") or now)
        inbound = it.get("last_from_me") == 0
        muted = apply_mute and _is_muted(it)
        recent_enough = front_window is None or age <= front_window
        unread_ok = (not require_unread) or bool(it.get("unread"))

        if inbound and not muted and recent_enough and unread_ok:
            front.append(
                {
                    "id": it["id"],
                    "who": it.get("who") or "",
                    "preview": it.get("preview") or "",
                    "age": _age_label(age),
                    "level": _level(age),
                }
            )
        elif background_window is None or age <= background_window:
            background.append(
                {
                    "id": it["id"],
                    "who": it.get("who") or "",
                    "preview": it.get("preview") or "",
                }
            )

    # Front: most urgent (oldest inbound) first. Background: most recent first.
    order = {"alert": 0, "wilt": 1, "normal": 2}
    front.sort(key=lambda f: order.get(f["level"], 3))
    return {"front": front, "background": background}


def calendar_events(items: list[dict]) -> list[dict]:
    """Today's events as the frontend's {time, title} list, sorted by start."""
    evs = sorted(items, key=lambda it: it.get("ts") or 0)
    return [
        {"time": (it.get("extra") or {}).get("time", ""), "title": it.get("preview") or ""}
        for it in evs
    ]


def news_items(items: list[dict], distilled: dict[str, str] | None = None) -> list[dict]:
    """News as the frontend's {source, title, url} list, newest first.
    title is the distilled line when available, else the original headline (PRD §4.4;
    informational only — no front/background mechanic)."""
    distilled = distilled or {}
    ordered = sorted(items, key=lambda it: it.get("ts") or 0, reverse=True)
    return [
        {
            "source": it.get("who") or "",
            "title": distilled.get(it["id"]) or it.get("preview") or "",
            "url": (it.get("extra") or {}).get("url", ""),
        }
        for it in ordered
    ]
