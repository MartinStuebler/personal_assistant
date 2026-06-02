# Garden — Personal Command Center (v1)

A calm, local-only morning dashboard. Pulls your real accounts (read-only) and shows
what needs attention as a small garden: things needing you stand upright in front;
things you've handled this week recede into a paler background **meadow**.

See [`PRD.md`](./PRD.md) for the full spec.

## Status

- [x] **Milestone 1 — Skeleton + meadow.** Flask serves the page at `/` and hardcoded
  sample state at `/api/state`. Frontend renders the front/background meadow from the API.
- [x] **Milestone 2 — Calendar + Gmail connectors.** OAuth desktop flow (read-only
  scopes), real data pulled into SQLite, triaged into front/background, served at
  `/api/state`. One-time authorize: `python -m connectors.google_auth`.
- [x] **Milestone 3 — News (RSS) + scheduled LLM distillation.** RSS pull for four
  tracks (industrial AI, fintech, healthcare, materials), last 24h, deduped, capped.
  Headlines are distilled to one calm line by Claude Haiku 4.5 on a background
  scheduler (every 4h, never on page load); each article summarized once and cached
  in SQLite, with graceful fallback to the raw headline. Set `ANTHROPIC_API_KEY` in
  `.env` (see `.env.example`) to enable distillation.
- [x] **Milestone 4 — iMessage.** Read-only snapshot of `chat.db` (copied with its
  `-wal`/`-shm` sidecars, opened read-only) + Contacts name join. Automated SMS
  (shortcodes, OTP/verification, tapback reactions) are filtered out. Needs Full
  Disk Access for the terminal.
- [x] **Milestone 5 — Triage threshold tuning.** Messages: inbound counts as "needs
  you" only within the last 7 days (per-bed window). Email: pull restricted to the
  Primary tab (`category:primary`) and only unread threads stand front. Calendar:
  events merged across all readable calendars, each tagged with its source.
- [ ] Milestone 6 — Background refresh loop + morning pull. *(Partial: news refreshes
  on the first page-load of a new day so an overnight-slept Mac still updates on wake.)*

## Run it (local, macOS)

```bash
cd personal_assistant
python3.12 -m venv venv          # PRD §2 needs 3.11+; macOS system python3 is 3.9
source venv/bin/activate
pip install -r requirements.txt
python -m connectors.google_auth  # one-time: browser consent, writes token.json
python app.py
# open http://127.0.0.1:5001
```

## Privacy (non-negotiable, PRD §9)

- **Read-only scopes only** — never sends, replies, deletes, or modifies anything.
- Everything stays **local**. No cloud, no telemetry, no deployment.
- Secrets (`client_secret.json`, `token.json`, `*.db`, `.env`) are git-ignored — verify
  before every commit.
- iMessage `chat.db` is opened from a **read-only copy**; the original is never touched.

## Manual prerequisites (you do these — Claude Code can't)

1. Create an empty GitHub repo (no README) and copy its URL.
2. Create a Google Cloud project + OAuth desktop credentials (Gmail + Calendar
   read-only scopes); download `client_secret.json` into this folder.
3. Grant **Full Disk Access** to your terminal app so iMessage `chat.db` is readable.
