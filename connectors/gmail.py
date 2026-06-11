"""
Gmail connector — read-only (PRD §4.1).

Pulls INBOX threads and normalizes each into the shared item schema:
  who           sender display name of the thread's last message
  preview       subject (snippet kept in extra for the tooltip)
  ts            unix seconds of the last message
  last_from_me  1 if the last message was sent by me, else 0  (drives triage front/bg)
  unread        1 if the thread has any UNREAD message
  extra         {snippet, labels, automated}  (automated = promotional/updates/etc.)

Uses thread metadata only (no bodies) — cheap and enough for the garden view.
"""

import base64  # noqa: F401  (reserved for future body parsing; not used in v1 read-only metadata pull)
import re
import time
from email.utils import parseaddr

from googleapiclient.discovery import build

# Gmail's own categorization → "automated/no-reply" hint for triage's mute logic.
AUTOMATED_LABELS = {
    "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES",
    "CATEGORY_FORUMS",
    "CATEGORY_SOCIAL",
}

# --- "Real people only" filter (PRD §6 escape hatch, email-specific) ----------
# Goal: keep person-to-person mail in front; drop machine senders and bulk lists.
# Three independent signals, strongest first — any one suppresses:
#   1. Gmail category labels (Promotions/Updates) — Gmail already sorted these.
#   2. List-Unsubscribe / Precedence:bulk headers — the RFC markers every mailing
#      list and bulk sender sets; the single highest-precision "not a human" tell.
#   3. The From address local-part — no-reply@, notifications@, mailer@, newsletter@…

# Categories that are never person-to-person (per spec: Promotions + Updates only;
# Forums/Social are left to land in front unless another signal catches them).
SUPPRESS_CATEGORIES = {"CATEGORY_PROMOTIONS", "CATEGORY_UPDATES"}

# Local-parts that are unambiguously a machine / no-reply / bulk sender — these
# suppress on the address alone (e.g. no-reply@, notifications@, mailer-daemon@).
_NONHUMAN_LOCALPART_RE = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply"
    r"|notifications?|notify|alerts?"
    r"|mailer([-_.]?daemon)?|bounces?|postmaster"
    r"|news(letter)?|marketing|promo(tions?)?|offers?|deals?|digest"
    r"|automated|auto[-_.]?(reply|confirm)?)([-_.+].*)?$",
    re.I,
)


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _display_name(from_header: str) -> str:
    name, email = parseaddr(from_header)
    return name or email or from_header


def is_human(item: dict) -> tuple[bool, str]:
    """Decide whether an email item looks person-to-person.

    Returns (human, reason): reason names the suppression cause when human is
    False (for the before/after audit), or '' when the sender looks human.
    Reads only fields already on the item's `extra`, so it is pure and testable.
    """
    extra = item.get("extra") or {}
    labels = set(extra.get("labels") or [])
    addr = (extra.get("from_email") or "").lower()
    local = addr.split("@", 1)[0] if "@" in addr else addr

    cat = labels & SUPPRESS_CATEGORIES
    if cat:
        pretty = sorted(c.split("_")[-1].title() for c in cat)[0]
        return False, f"category:{pretty}"
    if extra.get("list_unsub"):
        return False, "bulk:List-Unsubscribe"
    if (extra.get("precedence") or "").lower() in ("bulk", "list", "junk"):
        return False, f"bulk:Precedence={extra.get('precedence')}"
    if local and _NONHUMAN_LOCALPART_RE.match(local):
        return False, f"address:{local}@"
    return True, ""


def partition_humans(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split email items into (humans, suppressed). Suppressed items get
    extra['suppressed_reason'] set so the audit view can show what was filtered."""
    humans, suppressed = [], []
    for it in items:
        human, reason = is_human(it)
        if human:
            humans.append(it)
        else:
            ex = {**(it.get("extra") or {}), "suppressed_reason": reason}
            suppressed.append({**it, "extra": ex})
    return humans, suppressed


def fetch(creds, window_seconds: int = 24 * 3600, max_threads: int = 500) -> list[dict]:
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    me_email = service.users().getProfile(userId="me").execute().get("emailAddress", "")

    # Raw baseline: pull EVERYTHING in the inbox for the recent window — every
    # category, read or unread, any sender. Nothing hidden by content. The only
    # triage applied downstream is the simple front/meadow split (inbound vs.
    # already-replied). Category-/read-based filters were deliberately removed so
    # the true inbox is visible first; we'll reintroduce filters later once the
    # real noise is known.
    #
    # The 24h window is the only bound: the full inbox is thousands of threads and
    # one metadata call per thread is too slow to fetch all at once. A time window
    # (vs. a count cap) keeps the bed to recent activity — what's actually live.
    after = int(time.time()) - window_seconds
    listed = (
        service.users()
        .threads()
        .list(userId="me", q=f"in:inbox after:{after}", maxResults=max_threads)
        .execute()
        .get("threads", [])
    )

    items: list[dict] = []
    for t in listed:
        thread = (
            service.users()
            .threads()
            .get(
                userId="me",
                id=t["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date", "List-Unsubscribe", "Precedence"],
            )
            .execute()
        )
        msgs = thread.get("messages", [])
        if not msgs:
            continue

        last = msgs[-1]
        last_headers = last.get("payload", {}).get("headers", [])
        from_hdr = _header(last_headers, "From")
        subject = _header(last_headers, "Subject") or "(no subject)"
        # Fields the "real people only" filter (is_human) reads off `extra`.
        from_email = (parseaddr(from_hdr)[1] or "").lower()
        list_unsub = bool(_header(last_headers, "List-Unsubscribe"))
        precedence = _header(last_headers, "Precedence")

        last_from_me = 1 if me_email and me_email.lower() in from_hdr.lower() else 0

        all_labels = set()
        unread = 0
        for m in msgs:
            labels = m.get("labelIds", [])
            all_labels.update(labels)
            if "UNREAD" in labels:
                unread = 1

        ts = int(last.get("internalDate", "0")) // 1000  # ms → s
        automated = bool(all_labels & AUTOMATED_LABELS)

        items.append(
            {
                "id": t["id"],
                "who": _display_name(from_hdr),
                "preview": subject,
                "ts": ts,
                "last_from_me": last_from_me,
                "unread": unread,
                "extra": {
                    "snippet": last.get("snippet", ""),
                    "labels": sorted(all_labels),
                    "automated": automated,
                    "from_email": from_email,
                    "list_unsub": list_unsub,
                    "precedence": precedence,
                },
            }
        )

    return items
