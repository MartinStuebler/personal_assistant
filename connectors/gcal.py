"""
Google Calendar connector — read-only (PRD §4.2).

Pulls today's events from ALL of the user's calendars (not just primary — real days
live on shared/secondary calendars like "Social planning") and normalizes each:
  who      event title (the shared schema's 'who' slot; the frontend's "today"
           bed reads time+title out of extra, but we keep who populated too)
  preview  event title
  ts       unix start time (all-day events sort to start of day)
  extra    {time: "HH:MM" or "all-day", all_day: bool, calendar: source name}

Today's window is local-midnight to next-local-midnight, timezone-aware.
"""

from datetime import datetime, time as dtime, timedelta

from googleapiclient.discovery import build

# Calendars to skip when merging: purely decorative subscriptions that aren't real
# appointments (week-number overlays). Matched as a substring against the calendar id.
SKIP_CALENDAR_IDS = ("weeknum",)


def _local_day_bounds():
    """Return (start, end) of today as timezone-aware datetimes in the local tz."""
    now = datetime.now().astimezone()
    tz = now.tzinfo
    start = datetime.combine(now.date(), dtime.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return start, end


def _calendars(service) -> list[dict]:
    """Every calendar on the user's list we can actually read events from.
    freeBusyReader access exposes only busy/free blocks (no event detail), so skip it."""
    cals, page_token = [], None
    while True:
        resp = service.calendarList().list(pageToken=page_token).execute()
        for c in resp.get("items", []):
            cid = c.get("id", "")
            if c.get("accessRole") == "freeBusyReader":
                continue
            if any(skip in cid for skip in SKIP_CALENDAR_IDS):
                continue
            cals.append({"id": cid, "name": c.get("summary", cid)})
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return cals


def _normalize(ev: dict, cal_name: str, cal_id: str) -> dict:
    start_info = ev.get("start", {})
    title = ev.get("summary", "(no title)")

    if "dateTime" in start_info:
        # The API returns each event in its calendar's own tz (e.g. a LA-based shared
        # calendar yields -07:00); convert to local tz so the displayed time is right.
        dt = datetime.fromisoformat(start_info["dateTime"]).astimezone()
        time_str = dt.strftime("%H:%M")
        ts = int(dt.timestamp())
        all_day = False
    else:  # all-day event: {"date": "YYYY-MM-DD"}
        d = datetime.fromisoformat(start_info["date"]).astimezone()
        time_str = "all-day"
        ts = int(d.timestamp())
        all_day = True

    # Prefix the id with the calendar so the same invite on two calendars can't
    # collide on the (source, id) primary key in the store.
    return {
        "id": f"{cal_id}:{ev.get('id', title)}",
        "who": title,
        "preview": title,
        "ts": ts,
        "last_from_me": None,
        "unread": None,
        "extra": {"time": time_str, "all_day": all_day, "calendar": cal_name},
    }


def fetch(creds) -> list[dict]:
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    start, end = _local_day_bounds()
    items: list[dict] = []
    for cal in _calendars(service):
        try:
            events = (
                service.events()
                .list(
                    calendarId=cal["id"],
                    timeMin=start.isoformat(),
                    timeMax=end.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
                .get("items", [])
            )
        except Exception:  # a single inaccessible calendar shouldn't sink the rest
            continue
        items.extend(_normalize(ev, cal["name"], cal["id"]) for ev in events)

    items.sort(key=lambda it: it["ts"])
    return items
