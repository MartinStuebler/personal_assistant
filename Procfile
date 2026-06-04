# Railway / Heroku-style process definition.
# Single worker: the login brute-force lockout and the price/news caches are
# in-memory per-process (single-user app), so one worker keeps them consistent.
# Railway injects $PORT; gunicorn imports the Flask object `app` from app.py.
web: gunicorn app:app --bind 0.0.0.0:$PORT --workers 1
