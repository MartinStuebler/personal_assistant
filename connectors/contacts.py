"""
Contacts name resolution (PRD §4.3) — join phone numbers / emails to names so the
Messages bed shows "Lena" not "+1 415…".

Reads the macOS AddressBook SQLite db(s) read-only and builds a lookup map. Phones
are normalized to their last 10 digits (storage formats vary: +1 415…, (415) …, etc.).
Everything degrades gracefully to an empty map — if Contacts access is denied or the
db is missing, the iMessage connector simply falls back to showing the raw handle.
"""

import glob
import os
import re
import sqlite3

ADDRESSBOOK_GLOBS = [
    "~/Library/Application Support/AddressBook/AddressBook-v22.abcddb",
    "~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb",
]


def _norm_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw or "")
    return digits[-10:] if len(digits) >= 10 else None


def _full_name(first: str, last: str, org: str) -> str | None:
    name = " ".join(p for p in (first, last) if p).strip()
    return name or (org or None)


def _abcddb_paths() -> list[str]:
    paths = []
    for pat in ADDRESSBOOK_GLOBS:
        paths.extend(glob.glob(os.path.expanduser(pat)))
    return paths


def build_name_map() -> dict[str, str]:
    """Map of {normalized_phone | lowercased_email -> display name}. Empty on failure."""
    name_map: dict[str, str] = {}
    for path in _abcddb_paths():
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            records = {}
            for row in conn.execute(
                "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION FROM ZABCDRECORD"
            ):
                nm = _full_name(row[1] or "", row[2] or "", row[3] or "")
                if nm:
                    records[row[0]] = nm

            for owner, number in conn.execute(
                "SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"
            ):
                nm = records.get(owner)
                key = _norm_phone(number or "")
                if nm and key:
                    name_map.setdefault(key, nm)

            for owner, address in conn.execute(
                "SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"
            ):
                nm = records.get(owner)
                if nm and address:
                    name_map.setdefault(address.strip().lower(), nm)
        except sqlite3.Error:
            continue  # schema variations across macOS versions: skip this db
        finally:
            conn.close()

    return name_map


def resolve(handle: str, name_map: dict[str, str]) -> str | None:
    """Resolve a Messages handle (phone or email) to a contact name, if known."""
    if not handle:
        return None
    if "@" in handle:
        return name_map.get(handle.strip().lower())
    phone = _norm_phone(handle)
    return name_map.get(phone) if phone else None
