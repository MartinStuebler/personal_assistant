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

Server deployment (e.g. Railway): the local token.json / client_secret*.json files
won't exist there. As a fallback, the credential JSON can instead be supplied via env:
  - GOOGLE_TOKEN_JSON          = full contents of token.json
  - GOOGLE_CLIENT_SECRET_JSON  = full contents of client_secret*.json
Local files always take priority, so local dev is unchanged. The server path still
NEVER opens a browser and still raises NeedsAuth if no token is available.

Manual check (server-style, files absent):
    GOOGLE_TOKEN_JSON="$(cat token.json)" \
    GOOGLE_CLIENT_SECRET_JSON="$(cat client_secret*.json)" \
    python -c "import os,glob; \
        from connectors import google_auth as g; \
        g.TOKEN_PATH = g.ROOT / 'no-such-token.json'; \
        c = g.get_credentials(interactive=False); print('ok, valid:', c.valid)"
  (run from a directory where the real files are NOT globbed, or temporarily move them,
   to prove the env-only path loads and refreshes without error.)
"""

import glob
import json
import logging
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger(__name__)

# Env-var fallbacks for the credential files (used on servers without the files).
GOOGLE_TOKEN_ENV = "GOOGLE_TOKEN_JSON"
GOOGLE_CLIENT_SECRET_ENV = "GOOGLE_CLIENT_SECRET_JSON"

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


def _client_secret_dict() -> dict | None:
    """The OAuth client's {client_id, client_secret, token_uri, …}, from the
    client_secret*.json file if present, else the GOOGLE_CLIENT_SECRET_JSON env var.
    Returns the inner 'installed'/'web' object (where those fields live), or None if
    no source is available. File takes priority so local dev is unchanged."""
    matches = sorted(glob.glob(str(ROOT / "client_secret*.json")))
    if matches:
        data = json.loads(Path(matches[0]).read_text())
    else:
        raw = os.getenv(GOOGLE_CLIENT_SECRET_ENV)
        if not raw:
            return None
        data = json.loads(raw)
    # Desktop creds nest fields under "installed"; web creds under "web".
    return data.get("installed") or data.get("web") or data


def _load_token() -> Credentials | None:
    """Load cached credentials from token.json if present, else from the
    GOOGLE_TOKEN_JSON env var. File takes priority (local dev unchanged). Returns
    None if neither source is available."""
    if TOKEN_PATH.exists():
        return Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    raw = os.getenv(GOOGLE_TOKEN_ENV)
    if not raw:
        return None

    info = json.loads(raw)
    # A token.json written by to_json() already carries client_id/client_secret, so
    # refresh works from it alone. But if the env token lacks them, fill from the
    # client-secret source so the refresh path still has what it needs.
    if not (info.get("client_id") and info.get("client_secret")):
        secret = _client_secret_dict()
        if secret:
            info.setdefault("client_id", secret.get("client_id"))
            info.setdefault("client_secret", secret.get("client_secret"))
            info.setdefault(
                "token_uri",
                secret.get("token_uri", "https://oauth2.googleapis.com/token"),
            )
    return Credentials.from_authorized_user_info(info, SCOPES)


def _save_token(creds: Credentials) -> None:
    """Persist refreshed credentials to token.json. On a server with a read-only or
    ephemeral filesystem (env-var path) the write may fail — that's non-fatal: log and
    continue, since the refreshed credential is still valid in memory for this process."""
    try:
        TOKEN_PATH.write_text(creds.to_json())
    except OSError as e:
        logger.warning(
            "Could not persist refreshed token to %s (continuing with in-memory "
            "credentials): %s",
            TOKEN_PATH,
            e,
        )


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
