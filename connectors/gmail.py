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
from email.utils import parseaddr

from googleapiclient.discovery import build

# Gmail's own categorization → "automated/no-reply" hint for triage's mute logic.
AUTOMATED_LABELS = {
    "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES",
    "CATEGORY_FORUMS",
    "CATEGORY_SOCIAL",
}


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _display_name(from_header: str) -> str:
    name, email = parseaddr(from_header)
    return name or email or from_header


def fetch(creds, max_threads: int = 30) -> list[dict]:
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    me_email = service.users().getProfile(userId="me").execute().get("emailAddress", "")

    # Restrict to the Primary tab so the bed matches what the user actually sees
    # there — job-application auto-replies, newsletters, etc. live under Gmail's
    # Updates/Promotions tabs and shouldn't surface as "needs you".
    listed = (
        service.users()
        .threads()
        .list(userId="me", q="in:inbox category:primary", maxResults=max_threads)
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
                metadataHeaders=["From", "Subject", "Date"],
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
                },
            }
        )

    return items
