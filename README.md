# Garden — Personal Command Center (v1)

A calm, local-only morning dashboard. Pulls your real accounts (read-only) and shows
what needs attention as a small garden: things needing you stand upright in front;
things you've handled this week recede into a paler background **meadow**.

See [`PRD.md`](./PRD.md) for the full spec.

## Status

- [x] **Milestone 1 — Skeleton + meadow.** Flask serves the page at `/` and hardcoded
  sample state at `/api/state`. Frontend renders the front/background meadow from the API.
- [ ] Milestone 2 — Calendar + Gmail connectors (real data via OAuth).
- [ ] Milestone 3 — News (RSS).
- [ ] Milestone 4 — iMessage (read-only `chat.db` copy + contacts join).
- [ ] Milestone 5 — Triage threshold tuning.
- [ ] Milestone 6 — Background refresh loop + morning pull.

## Run it (local, macOS)

```bash
cd persona_assistant
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
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
