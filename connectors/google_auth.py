"""
Shared Google OAuth — desktop ("installed app") flow, read-only scopes only.

PRD §4.1/§4.2/§9: gmail.readonly + calendar.readonly, one cached token, local-only.
The same credential serves both Gmail and Calendar.

Two entry points:
  - get_credentials(interactive=False)  -> used by the Flask server. NEVER opens a
    browser; raises NeedsAuth if there's no usable token so /api/state can fall back
    gracefully instead of blocking the request on a consent screen.
  - python -m connectors.google_auth    -> the one-time authorize step you run by
    hand. Opens the browser consent screen and writes token.json.
"""

import glob
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Read-only only. No send/modify/delete scope is ever requested (PRD §9).
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]

ROOT = Path(__file__).resolve().parent.parent
TOKEN_PATH = ROOT / "token.json"


class NeedsAuth(RuntimeError):
    """No usable cached credential. Run `python -m connectors.google_auth`."""


def _client_secret_path() -> str:
    """The OAuth client downloads as client_secret_<id>.apps.googleusercontent.com.json,
    not literally client_secret.json — so glob for it in the project root."""
    matches = sorted(glob.glob(str(ROOT / "client_secret*.json")))
    if not matches:
        raise NeedsAuth(
            "No client_secret*.json in the project root. Download OAuth desktop "
            "credentials from Google Cloud into this folder (PRD §8 step 2)."
        )
    return matches[0]


def _load_token() -> Credentials | None:
    if not TOKEN_PATH.exists():
        return None
    return Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)


def _save_token(creds: Credentials) -> None:
    TOKEN_PATH.write_text(creds.to_json())


def get_credentials(interactive: bool = False) -> Credentials:
    """Return valid credentials, refreshing or (only if interactive) authorizing.

    interactive=False (the server path): refresh silently if possible, else raise
    NeedsAuth. Never opens a browser.
    interactive=True (the manual authorize path): run the desktop consent flow.
    """
    creds = _load_token()

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_token(creds)
        return creds

    if not interactive:
        raise NeedsAuth(
            "Gmail/Calendar not authorized yet. Run:  python -m connectors.google_auth"
        )

    flow = InstalledAppFlow.from_client_secrets_file(_client_secret_path(), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    _save_token(creds)
    return creds


if __name__ == "__main__":
    print("Opening browser for Google consent (read-only Gmail + Calendar)…")
    get_credentials(interactive=True)
    print(f"Authorized. Token cached at {TOKEN_PATH}")
