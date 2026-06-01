"""
Garden — Personal Command Center (v1)
Flask app: serves the single-page frontend and the /api/state JSON it renders from.

Milestone 1 (this file, for now): serves the page + HARDCODED sample state, so the
full frontend<->backend contract is proven end to end before any real connector is
wired. Real connectors (Gmail, Calendar, iMessage, News) replace SAMPLE_STATE later,
feeding through store.py + triage.py per the PRD.
"""

from flask import Flask, jsonify, send_from_directory
from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"

app = Flask(__name__, static_folder=None)


# --- Sample state matching the frontend contract in static/garden-mockup.html ---
# email/messages: front (needs you) + background (the handled meadow).
# news: items list. today: calendar events. This is a realistic "busy morning".
SAMPLE_STATE = {
    "meta": {
        "clock": "Sat · 7:02",
        "greeting": "Morning, <em>Martin.</em>",
        "weather": "☁️ 58°",
        "stale": False,
    },
    "beds": {
        "email": {
            "icon": "✉️", "iconBg": "#f7ecd2", "title": "Email",
            "front": [
                {"id": "e1", "who": "Aily Labs", "preview": "Recruiter replied · Business Impact Lead", "age": "2d", "level": "alert"},
                {"id": "e2", "who": "Nitra", "preview": "Next-round scheduling", "age": "1d", "level": "wilt"},
                {"id": "e3", "who": "MtM Labo", "preview": "Invoice approved · fyi", "age": "5h", "level": "normal"},
            ],
            "background": [
                {"id": "e10", "who": "Stella M.", "preview": "Replied yesterday"},
                {"id": "e11", "who": "GitHub", "preview": "Digest · read"},
                {"id": "e12", "who": "Lena", "preview": "Sent Tue"},
                {"id": "e13", "who": "Accountant", "preview": "Sent Mon"},
                {"id": "e14", "who": "Coursera", "preview": "Replied Mon"},
            ],
        },
        "messages": {
            "icon": "\U0001f4ac", "iconBg": "#fae3de", "title": "Messages",
            "front": [
                {"id": "m1", "who": "Lena \U0001f1eb\U0001f1f7", "preview": "France trip dates", "age": "3d", "level": "alert"},
                {"id": "m2", "who": "Mama", "preview": "Voice note", "age": "1d", "level": "wilt"},
            ],
            "background": [
                {"id": "m10", "who": "Tyler", "preview": "Replied today"},
                {"id": "m11", "who": "BJJ group", "preview": "Replied today"},
                {"id": "m12", "who": "Sam", "preview": "Sent yesterday"},
                {"id": "m13", "who": "Dad", "preview": "Sent Tue"},
            ],
        },
        "news": {
            "icon": "\U0001f4f0", "iconBg": "#e2f0e4", "title": "News",
            "items": [
                {"source": "Industrial AI", "title": "Siemens expands decision-AI pilots", "url": "https://example.com/a"},
                {"source": "Fintech", "title": "New AML tooling round closes", "url": "https://example.com/b"},
                {"source": "Materials", "title": "Bio-fur startup lands luxury deal", "url": "https://example.com/c"},
            ],
        },
        "today": {
            "icon": "\U0001f4c5", "iconBg": "#e8eefb", "title": "Today",
            "events": [
                {"time": "10:00", "title": "Hyro call"},
                {"time": "13:30", "title": "BJJ open mat"},
                {"time": "19:00", "title": "Watercolor"},
            ],
        },
    },
}


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "garden-mockup.html")


@app.route("/api/state")
def api_state():
    # Milestone 1: hardcoded. Later: pull connectors -> store -> triage -> here.
    return jsonify(SAMPLE_STATE)


if __name__ == "__main__":
    # Local only. Debug on for development; this never gets deployed (PRD §2, §10).
    app.run(host="127.0.0.1", port=5001, debug=True)
