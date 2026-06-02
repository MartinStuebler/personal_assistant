"""
iMessage connector — read-only, local (PRD §4.3).

Safety first: the live Messages db is NEVER opened in place. We copy chat.db (plus its
-wal/-shm sidecars, so the snapshot is consistent) into a temp dir and open the COPY
read-only. The original under ~/Library/Messages is never written to.

Per conversation (1:1 and group chats alike) we derive: contact/group name, last
message snippet, direction (last_from_me), and timestamp — exactly what triage needs
to place a conversation front (last inbound) or in the meadow (I replied).

Requires Full Disk Access for the running process (System Settings → Privacy &
Security → Full Disk Access). Without it, opening the db raises an OperationalError,
which we wrap as MessagesUnavailable so the app can show an empty bed + a hint instead
of crashing.
"""

import os
import re
import shutil
import sqlite3
import tempfile

from . import contacts

CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")

# Apple Cocoa epoch (2001-01-01 UTC) → Unix epoch offset, in seconds.
APPLE_EPOCH = 978307200

MAX_CHATS = 40  # most-recent conversations to consider

# --- Automated-SMS filtering (PRD §6 escape hatch, applied at the source) ---
# These aren't conversations that need you, so they're dropped from the bed entirely
# (not just demoted) the way OTPs and tapbacks clutter the front otherwise.

# Trailing service tag iMessage appends to filtered SMS handles, e.g. "35213(smsft)",
# "+1646…(smsfp)", "69368(smsft_rm)". Stripped for both display and shortcode detection.
_SMS_SUFFIX_RE = re.compile(r"\s*\(sms[^)]*\)\s*$", re.I)

# OTP / verification / transactional one-way SMS.
_OTP_RE = re.compile(
    r"(one[\-\s]?time|verification code|verify|\bOTP\b|\bcode\b.{0,12}\bis\b"
    r"|do not share|2fa|security code|passcode|payment required)",
    re.I,
)

# Tapback reactions surface as their own "message" whose text is the localized
# "Loved/Liked/…" wrapper. macOS also sets associated_message_type != 0 for these;
# we check both since the type isn't always populated on older rows.
_TAPBACK_RE = re.compile(
    r"^(Loved|Liked|Disliked|Laughed at|Emphasized|Questioned|Reacted"
    r"|A réagi|Le encantó|Le gustó|A aimé|A adoré|Adoró)\b"
)


def _strip_sms_suffix(s: str) -> str:
    return _SMS_SUFFIX_RE.sub("", s or "").strip()


def _is_shortcode(handle: str) -> bool:
    """A numeric SMS short code (5–6 digits) rather than a real phone (10–11)."""
    core = _strip_sms_suffix(handle)
    if not re.fullmatch(r"\+?\d+", core or ""):
        return False
    return len(re.sub(r"\D", "", core)) <= 6


def _is_automated(handle: str, identifier: str, text: str, assoc_type, is_group: bool) -> bool:
    if assoc_type:  # nonzero == a tapback/reaction add or remove
        return True
    t = (text or "").strip()
    if _TAPBACK_RE.match(t):
        return True
    if _OTP_RE.search(t):
        return True
    # Shortcode senders only make sense for 1:1 chats (groups have real members).
    if not is_group and (_is_shortcode(handle or "") or _is_shortcode(identifier or "")):
        return True
    return False


class MessagesUnavailable(RuntimeError):
    """chat.db could not be opened — almost always missing Full Disk Access."""


# Last message per chat. The subquery picks the max-date message id per chat, then we
# join back for that message's text/sender/direction.
_QUERY = """
SELECT
    c.ROWID            AS chat_id,
    c.chat_identifier  AS chat_identifier,
    c.display_name     AS display_name,
    c.style            AS style,
    m.text             AS text,
    m.attributedBody   AS attributed_body,
    m.is_from_me       AS is_from_me,
    m.date             AS date,
    m.associated_message_type AS assoc_type,
    h.id               AS handle
FROM chat c
JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
JOIN message m            ON m.ROWID = cmj.message_id
LEFT JOIN handle h        ON h.ROWID = m.handle_id
JOIN (
    SELECT cmj2.chat_id AS chat_id, MAX(m2.date) AS maxdate
    FROM message m2
    JOIN chat_message_join cmj2 ON cmj2.message_id = m2.ROWID
    GROUP BY cmj2.chat_id
) last ON last.chat_id = c.ROWID AND last.maxdate = m.date
ORDER BY m.date DESC
LIMIT ?
"""


def _apple_to_unix(date: int) -> int:
    # Modern chat.db stores nanoseconds; older versions stored seconds.
    seconds = date / 1_000_000_000 if date > 1_000_000_000_000 else date
    return int(seconds) + APPLE_EPOCH


def _decode_attributed_body(blob: bytes) -> str:
    """Best-effort text extraction from a message's attributedBody (NSAttributedString
    archive) for rows where the plain `text` column is NULL (newer macOS). Returns ''
    if it can't confidently recover the string — the snippet is non-critical."""
    if not blob:
        return ""
    try:
        data = blob
        if b"NSString" in data:
            data = data.split(b"NSString", 1)[1]
            data = data[5:]  # skip class/version header bytes
            if not data:
                return ""
            if data[0] == 0x81:  # 2-byte little-endian length prefix
                length = int.from_bytes(data[1:3], "little")
                start = 3
            else:
                length = data[0]
                start = 1
            return data[start : start + length].decode("utf-8", "ignore").strip()
    except Exception:
        return ""
    return ""


def _snapshot_db() -> str:
    """Copy chat.db (+ sidecars) to a fresh temp dir; return the copy's path."""
    tmpdir = tempfile.mkdtemp(prefix="garden_msg_")
    dst = os.path.join(tmpdir, "chat.db")
    shutil.copy2(CHAT_DB, dst)
    for ext in ("-wal", "-shm"):
        side = CHAT_DB + ext
        if os.path.exists(side):
            shutil.copy2(side, dst + ext)
    return dst


def _chat_name(row, name_map) -> str:
    """Group: display_name, else the resolved/raw handle for 1:1."""
    if row["display_name"]:
        return row["display_name"]
    handle = _strip_sms_suffix(row["handle"] or row["chat_identifier"] or "")
    return contacts.resolve(handle, name_map) or handle or "Unknown"


def fetch(max_chats: int = MAX_CHATS) -> list[dict]:
    if not os.path.exists(CHAT_DB):
        raise MessagesUnavailable(f"{CHAT_DB} not found (is Messages set up on this Mac?)")

    try:
        snapshot = _snapshot_db()
    except (PermissionError, OSError) as e:
        raise MessagesUnavailable(
            "Could not read chat.db — grant Full Disk Access to your terminal "
            "(System Settings → Privacy & Security → Full Disk Access)."
        ) from e

    name_map = contacts.build_name_map()
    items: list[dict] = []
    tmpdir = os.path.dirname(snapshot)
    try:
        conn = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(_QUERY, (max_chats,)).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        raise MessagesUnavailable(
            "Could not open chat.db — grant Full Disk Access to your terminal "
            "(System Settings → Privacy & Security → Full Disk Access)."
        ) from e
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    for r in rows:
        snippet = (r["text"] or "").strip() or _decode_attributed_body(r["attributed_body"])
        is_group = (r["style"] == 43)

        # Drop automated SMS (shortcodes, OTP/verification, tapback reactions) outright:
        # they aren't conversations, so they shouldn't crowd the bed at all.
        if _is_automated(r["handle"], r["chat_identifier"], snippet, r["assoc_type"], is_group):
            continue

        items.append(
            {
                "id": str(r["chat_id"]),
                "who": _chat_name(r, name_map),
                "preview": snippet or "(message)",
                "ts": _apple_to_unix(r["date"]),
                "last_from_me": 1 if r["is_from_me"] else 0,
                "unread": None,  # not reliably available per-chat without extra joins
                "extra": {"group": is_group, "handle": r["handle"]},
            }
        )

    return items
