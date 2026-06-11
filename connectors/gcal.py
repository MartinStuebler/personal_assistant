"""
Google Calendar connector — read-only (PRD §4.2).

Pulls today's events from ALL of the user's calendars (not just primary — real days
live on shared/secondary calendars like "Social planning") and normalizes each:
  who      event title (the shared schema's 'who' slot; the frontend's "today"
           bed reads time+title out of extra, but we keep who populated too)
  preview  event title
  ts       unix start time (all-day events sort to start of day)
  extra    {time: "HH:MM" or "all-day", all_day: bool, calendar: source name}

Every event is rendered in DISPLAY_TZ (America/New_York) regardless of the source
calendar's tz OR the host process's tz. This matters on a UTC server (e.g. Railway/
gunicorn): bare datetime.astimezone() would resolve to the host tz, so a 9pm-ET event
would render as next-day UTC and surface on the wrong day. Pinning the tz fixes that.
Today's window is DISPLAY_TZ-midnight to next DISPLAY_TZ-midnight.
"""

from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

# The single timezone every event is displayed in, independent of host or source cal.
DISPLAY_TZ = ZoneInfo("America/New_York")

# Calendars to skip when merging: purely decorative subscriptions that aren't real
# appointments (week-number overlays). Matched as a substring against the calendar id.
SKIP_CALENDAR_IDS = ("weeknum",)


def _local_day_bounds():
    """Return (start, end) of today as DISPLAY_TZ-aware datetimes (not host-local)."""
    now = datetime.now(DISPLAY_TZ)
    start = datetime.combine(now.date(), dtime.min, tzinfo=DISPLAY_TZ)
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
        # calendar yields -07:00). Convert to DISPLAY_TZ — NOT bare .astimezone(), which
        # would follow the host tz and mis-place events on a non-ET server.
        dt = datetime.fromisoformat(start_info["dateTime"]).astimezone(DISPLAY_TZ)
        time_str = dt.strftime("%H:%M")
        ts = int(dt.timestamp())
        all_day = False
    else:  # all-day event: {"date": "YYYY-MM-DD"} — anchor at DISPLAY_TZ midnight.
        d = datetime.fromisoformat(start_info["date"]).replace(tzinfo=DISPLAY_TZ)
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
