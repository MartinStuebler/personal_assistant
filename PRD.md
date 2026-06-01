# PRD — "Garden" Personal Command Center (v1)

## 1. What this is

A single-page personal dashboard that pulls from my real accounts and shows everything needing my attention as a small garden. Four planter cards sit in a row (stacked on mobile). Each has a garden bed on top and an inbox list below. The number of plants in each bed reflects how much is in that source; items needing attention stand in the front, upright and green (wilting/yellow + red bud if overdue). Items already handled recede into the background as smaller, paler plants that accumulate into a meadow — a visible record of effort. When a bed's front row is empty, it shows "Nothing needs you" with a full background meadow behind it, and its sky brightens.

v1 is **read-only**: it displays state pulled from my accounts. It does not send, reply, delete, or modify anything. The front-vs-background split is **derived from the data**, not from manual clicking (see §6).

## 2. Stack

- **Backend:** Python 3.11+, Flask. (Consistent with my food-LCA project so I keep learning the same stack.)
- **Store:** SQLite (single local file). One user, local-only.
- **Frontend:** the existing single-file `garden-mockup.html` (vanilla HTML/CSS/JS, no framework). The backend serves it and feeds it JSON. Keep the visual identical to the mockup; only swap fake data for the live `/api/state` response.
- **Freshness model:** **live pull on open.** When the page loads, `/api/state` fetches all four sources fresh and the frontend shows a brief loading state while they return. The most recent successful pull is cached in SQLite and used as a fallback if a source is slow or down, so the page never blocks on one failing connector. A light background refresh every ~15 min keeps the cache warm.
- **Everything runs locally on my Mac.** No deployment, no cloud, no third-party hosting in v1.

## 3. Architecture

```
connectors/        one module per source, each normalizes into a shared item schema
  gmail.py
  gcal.py
  imessage.py
  news.py
store.py           SQLite read/write, the shared item schema
triage.py          derives front-vs-background + staleness from items
app.py             Flask: serves the page + /api/state
static/garden-mockup.html
```

Flow: each connector pulls → normalizes → writes to SQLite → triage derives state → `/api/state` returns the JSON the frontend already expects.

## 4. Data sources (and the honest hard parts)

### 4.1 Gmail — read-only
- Use the Gmail API with the **`gmail.readonly`** scope only.
- Pull: inbox threads, sender, subject, snippet, timestamps, whether the last message is from me, read/unread, labels.
- **Manual prerequisite (I do this, not Claude Code):** create a Google Cloud project, enable the Gmail + Calendar APIs, create OAuth desktop credentials, download `client_secret.json` into the project. First run opens a browser consent screen; token is cached locally. Claude Code cannot create the Cloud project or credentials for me.

### 4.2 Google Calendar — read-only
- Calendar API, **`calendar.readonly`** scope.
- Pull today's events: start time, title. (Same OAuth credential as Gmail; just add the scope.)

### 4.3 iMessage — read-only, local
- Read the local Messages database at `~/Library/Messages/chat.db` (SQLite). **Copy it to a temp path and open the copy read-only** — never write to the original.
- Derive per-conversation: contact handle, last message, direction (from me / to me), timestamp. **Group chats are included** and treated like 1:1 conversations for front/background logic (see §6), with the mute list as the noise escape hatch.
- Join phone numbers/emails to names via the macOS Contacts (AddressBook) database, or a small local contacts export, so it shows "Lena" not "+1 415…".
- **Manual prerequisites (I do these):**
  - Grant **Full Disk Access** to the terminal app (and/or the Python binary) in System Settings → Privacy & Security → Full Disk Access. Without this, reading `chat.db` fails silently/with permission errors.
  - Have **Messages in iCloud** enabled and synced so iPhone texts appear in the Mac database.
- **Known limitation to document in the README:** the database only reflects what the Mac has synced. If the Mac was asleep/off overnight, the morning view can lag the phone. Acceptable for v1.

### 4.4 News — last 24h, tuned to my tracks
- Pull from RSS feeds (and/or a news API if one's free/easy). Topics: industrial AI, fintech infrastructure, healthcare commercial, materials.
- Keep last 24h, dedupe, cap at a handful of headlines per refresh. Headlines + source only. News items are informational — they do **not** use the front/background mechanic; they're just shown as plants/rows.

## 5. Shared item schema (SQLite)

One `items` table, normalized across sources:

```
id            TEXT  (stable per source item, e.g. gmail thread id)
source        TEXT  ('email' | 'messages' | 'news' | 'calendar')
who           TEXT  (sender / contact name / news source / event title)
preview       TEXT  (subject/snippet/headline — used for hover tooltip)
ts            INTEGER (unix, last activity)
last_from_me  INTEGER (1/0; null for news/calendar)
unread        INTEGER (1/0; null where N/A)
extra         TEXT  (json: event time, news url, label, etc.)
updated_at    INTEGER
```

## 6. Triage: deriving front vs background (the core logic)

This is what makes the garden meaningful without manual tending. For the two actionable sources:

**Email & Messages — an item is FRONT (needs me) when:**
- the last message in the thread/conversation is **inbound** (`last_from_me = 0`), AND
- it isn't an obvious no-reply item (newsletter/automated/promotional label, or sender on a small ignore list).

**It is BACKGROUND (handled) when:**
- I have sent a message in the thread/conversation within the **last 7 days** (`last_from_me = 1` at any point in the window). This is the meadow: a record of everything I touched this week. It is intentionally generous.

**Group chats (Messages):** treated like any other conversation. If the last message in a group is inbound, it goes to the front. *(Noise risk: active group chats are almost always last-inbound and will tend to sit in the front even when I owe nothing. Mitigation below.)*

**Mute list:** a small config list of conversation/thread identifiers (and a newsletter/automated-sender list for email) that are forced to background regardless of inbound state. This is the escape hatch for noisy groups and no-reply senders. Easy to edit; expected to be tuned over the first week of real use.

**Render cap:** a bed shows the **true count** in its badge, but renders at most ~12 background plants so a small card does not overcrowd. Front items are not capped (if there are many, that is the signal).

**Staleness drives wilt:**
- front item with inbound age > ~48h → `alert` (wilts: yellow leaves, red bud, slight droop).
- front item age 18–48h → mild (`wilt` dot).
- younger → normal.

**Background window:** background plants are kept for the **last 7 days**, then fade out so the meadow stays a rolling weekly record rather than growing forever. Configurable constant.

`/api/state` returns, per bed: the front items (for the rows + upright plants) and the background items (for the meadow), plus a count badge = number of front items.

## 7. Frontend contract

The mockup already renders from a JSON structure. Backend must return that exact shape at `GET /api/state` so the page is unchanged except for the data source. Example:

```json
{
  "email":    { "front": [ {"id":"e1","who":"Aily Labs","preview":"Recruiter replied","ts":1717000000,"alert":true} ],
                "background": [ {"id":"e4","who":"Stella M.","preview":"Replied yesterday","ts":1716900000} ] },
  "messages": { "front": [ ... ], "background": [ ... ] },
  "news":     { "items": [ {"source":"Industrial AI","title":"...","url":"..."} ] },
  "calendar": { "events": [ {"time":"10:00","title":"Hyro call"} ] }
}
```

Plant height per item can stay pseudo-random-by-index (as in the mockup) — it's decorative. Sky tint per bed is derived: brighter if no front items, greyer if any front item is in `alert`.

## 8. Setup steps

**Manual prerequisites I complete before/around the build:**
1. Create an empty GitHub repo (no README) and copy its URL. *(Claude Code can `git init`, commit, add the remote, and push — but it should not create the repo under my account. I create the empty repo; it wires up and pushes.)*
2. Create the Google Cloud project + OAuth desktop credentials (Gmail + Calendar read-only scopes), download `client_secret.json` into the project root. *(Claude Code cannot do this for me.)*
3. Grant Full Disk Access to my terminal app so iMessage `chat.db` is readable.

**What Claude Code does from this PRD:**
4. Scaffold the folder structure in §3, set up a venv, `requirements.txt`, `.gitignore` (must ignore `client_secret.json`, `token.json`, `*.db`, `.env`).
5. Implement connectors, store, triage, Flask app, and serve the frontend.
6. `git init`, initial commit, add my remote, push.

## 9. Security & privacy (non-negotiable for v1)

- **Read-only API scopes only.** No send/modify/delete scopes requested anywhere.
- All data and tokens stay **local**. Nothing leaves the machine; no telemetry, no external calls except to Google/news endpoints for fetching.
- **Never copy secrets into the repo.** `client_secret.json`, `token.json`, the SQLite db, and any `.env` are git-ignored. Verify the first commit doesn't contain them.
- Open the iMessage `chat.db` from a **read-only copy**; never write to `~/Library/Messages`.
- No credentials, account numbers, or personal identifiers hardcoded in source.

## 10. Explicit non-goals for v1

- No sending, replying, drafting, deleting, archiving, or any write/modify action anywhere.
- No manual "tend" clicking (front/background is derived; manual tending is v2).
- No LinkedIn (no legitimate personal read API; stays manual).
- No WhatsApp/Telegram/Signal (I don't use the ones with real APIs; WhatsApp has no legit personal read).
- No push notifications / no alerts. I go to it; it never pings me.
- No analytics, streaks, or scoring. It's a calm morning object, not a metrics tool.
- No multi-user, no auth, no deployment.

## 11. Build order (milestones)

1. **Skeleton:** Flask app serves the existing mockup HTML at `/`, returns hardcoded sample JSON at `/api/state`. Page renders from the API instead of inline data. *(Proves the contract end to end before any real connector.)*
2. **Calendar + Gmail:** wire OAuth, implement those two connectors → real data into SQLite → real `/api/state`. (Easiest, pure API.)
3. **News:** RSS pull for my tracks.
4. **iMessage:** read-only `chat.db` copy + contacts name join.
5. **Triage polish:** tune the front/background and staleness thresholds against my actual inbox over a few days.
6. **Refresh loop:** background scheduler every 10–15 min + a 6am morning pull.

Ship after milestone 2 if needed — Calendar + Gmail alone is already useful. Everything after is additive.

## 12. Open questions to decide while building (not blockers)

- Exact RSS feeds/news source for my four tracks.
- Initial contents of the mute list (noisy group chats, newsletter senders). Expected to be the main thing tuned over the first week of real use.
- Whether past calendar events should recede into the background as the day progresses (nice fit, but v2).
- Background-meadow render cap exact number (default ~12 visible).
