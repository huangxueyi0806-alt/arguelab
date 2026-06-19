"""
Supabase client for ArgueLab subscriber management.
Uses the `beta_users` table as the single source of truth for subscribers.

Falls back to local `data/subscribers.json` when Supabase is unreachable.
"""

import json
import os
import urllib.request
import urllib.error
from datetime import datetime

# ── Config ──
import os as _os
SUPABASE_URL = _os.environ.get("SUPABASE_URL") or "https://guhcfdllaxzbcvqwhzzc.supabase.co"
SUPABASE_KEY = (
    _os.environ.get("SUPABASE_SERVICE_KEY")
    or "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imd1aGNmZGxsYXh6YmN2cXdoenpjIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MTI1ODgxOSwiZXhwIjoyMDk2ODM0ODE5fQ."
    "ACm_S8iG0RnccwsnyavvToiwk9v3wyJwQqYRi2KGxfA"
)
TABLE = "beta_users"

# Path to local fallback JSON (same as server.py)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SUBSCRIBERS_JSON = os.path.join(BASE_DIR, "data", "subscribers.json")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "apikey": SUPABASE_KEY,
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _fetch(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _mutate(url: str, method: str, body: dict) -> list[dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


# ── Public API ────────────────────────────────────────────────────────────────

def get_subscribers(active_only: bool = True) -> list[dict]:
    """
    Fetch subscribers from Supabase `beta_users` table.
    Returns a list of dicts compatible with the old subscribers.json format.

    Fallback: if Supabase is unreachable, reads from local subscribers.json.
    """
    try:
        url = f"{SUPABASE_URL}/rest/v1/{TABLE}?select=*"
        if active_only:
            url += "&status=eq.active&unsubscribed_at=is.null"
        rows = _fetch(url)
        # Normalize to old format for compatibility
        return [_normalize(row) for row in rows]
    except Exception as e:
        print(f"[supabase] fetch failed: {e} — falling back to local JSON")
        return _load_local_fallback()


def add_subscriber(email: str, name: str = "", **kwargs) -> dict:
    """
    Add (or re-activate) a subscriber in Supabase.
    If the email already exists but is inactive, re-activates it.
    Returns the subscriber dict (normalized format).
    """
    email = email.strip().lower()
    now = datetime.utcnow().isoformat()

    try:
        # Check if email already exists
        url = f"{SUPABASE_URL}/rest/v1/{TABLE}?select=*&email=eq.{email}"
        existing = _fetch(url)

        if existing:
            row = existing[0]
            if row.get("status") == "active" and not row.get("unsubscribed_at"):
                return _normalize(row)  # already active
            # Re-activate
            update_url = f"{SUPABASE_URL}/rest/v1/{TABLE}?id=eq.{row['id']}"
            _mutate(update_url, "PATCH", {
                "status": "active",
                "unsubscribed_at": None,
                "last_subscribed_at": now,
                "updated_at": now,
            })
            print(f"[supabase] re-activated: {email}")
        else:
            # Insert new
            insert_url = f"{SUPABASE_URL}/rest/v1/{TABLE}"
            payload = {
                "email": email,
                "identity": kwargs.get("identity", "unknown"),
                "goals": kwargs.get("goals", []),
                "topics": kwargs.get("topics", []),
                "frequency": kwargs.get("frequency", "weekly-1"),
                "consent": True,
                "status": "active",
                "source": kwargs.get("source", "manual"),
                "created_at": now,
                "updated_at": now,
                "last_subscribed_at": now,
            }
            _mutate(insert_url, "POST", payload)
            print(f"[supabase] inserted: {email}")

        # Return the updated/inserted row
        rows = _fetch(f"{SUPABASE_URL}/rest/v1/{TABLE}?select=*&email=eq.{email}")
        row = rows[0] if rows else {"email": email}
        # Also persist to local JSON as backup
        _sync_to_local_json()
        return _normalize(row)

    except Exception as e:
        print(f"[supabase] add_subscriber failed: {e}")
        # Fallback: write to local JSON
        return _add_to_local_json(email, name, **kwargs)


def remove_subscriber(email: str) -> dict:
    """
    Soft-delete: set status=inactive and unsubscribed_at.
    Returns {"status": "ok"} on success.
    """
    email = email.strip().lower()
    now = datetime.utcnow().isoformat()

    try:
        url = f"{SUPABASE_URL}/rest/v1/{TABLE}?email=eq.{email}"
        _mutate(url, "PATCH", {
            "status": "inactive",
            "unsubscribed_at": now,
            "updated_at": now,
        })
        print(f"[supabase] removed: {email}")
        _sync_to_local_json()
        return {"status": "ok", "email": email}
    except Exception as e:
        print(f"[supabase] remove_subscriber failed: {e}")
        return _remove_from_local_json(email)


def update_subscriber(email: str, **kwargs) -> dict:
    """Update subscriber preferences (goals, topics, frequency, etc.)."""
    email = email.strip().lower()
    now = datetime.utcnow().isoformat()

    try:
        url = f"{SUPABASE_URL}/rest/v1/{TABLE}?email=eq.{email}"
        payload = {"updated_at": now}
        for k, v in kwargs.items():
            if k in ("goals", "topics", "frequency", "identity", "name", "consent", "status"):
                payload[k] = v
        _mutate(url, "PATCH", payload)
        rows = _fetch(f"{SUPABASE_URL}/rest/v1/{TABLE}?select=*&email=eq.{email}")
        return {"status": "ok", "subscriber": _normalize(rows[0]) if rows else {}}
    except Exception as e:
        print(f"[supabase] update_subscriber failed: {e}")
        return {"status": "error", "message": str(e)}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _normalize(row: dict) -> dict:
    """Convert a beta_users row to the old subscribers.json format."""
    return {
        "email": row.get("email", ""),
        "name": row.get("name", ""),
        "subscribed_at": (row.get("last_subscribed_at") or row.get("created_at") or "")[:19],
        "verified": row.get("consent", False),
        "token": row.get("id", "")[:12],  # use UUID prefix as token
        "identity": row.get("identity", ""),
        "goals": row.get("goals", []),
        "status": row.get("status", "active"),
    }


def _load_local_fallback() -> list[dict]:
    try:
        with open(SUBSCRIBERS_JSON) as f:
            return json.load(f)
    except Exception:
        return []


def _sync_to_local_json() -> None:
    """Mirror Supabase subscribers to local JSON as backup."""
    try:
        rows = _fetch(f"{SUPABASE_URL}/rest/v1/{TABLE}?select=*&status=eq.active&unsubscribed_at=is.null")
        normalized = [_normalize(r) for r in rows]
        os.makedirs(os.path.dirname(SUBSCRIBERS_JSON), exist_ok=True)
        with open(SUBSCRIBERS_JSON, "w") as f:
            json.dump(normalized, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[supabase] backup sync failed: {e}")


def _add_to_local_json(email: str, name: str = "", **kwargs) -> dict:
    subs = _load_local_fallback()
    if any(s.get("email") == email for s in subs):
        return {"status": "exists", "email": email}
    sub = {
        "email": email,
        "name": name or email.split("@")[0],
        "subscribed_at": datetime.utcnow().isoformat(),
        "verified": False,
        "token": "",
    }
    subs.append(sub)
    os.makedirs(os.path.dirname(SUBSCRIBERS_JSON), exist_ok=True)
    with open(SUBSCRIBERS_JSON, "w") as f:
        json.dump(subs, f, indent=2, ensure_ascii=False)
    return {"status": "ok", "email": email}


def _remove_from_local_json(email: str) -> dict:
    subs = _load_local_fallback()
    new = [s for s in subs if s.get("email") != email]
    if len(new) == len(subs):
        return {"status": "not_found", "email": email}
    os.makedirs(os.path.dirname(SUBSCRIBERS_JSON), exist_ok=True)
    with open(SUBSCRIBERS_JSON, "w") as f:
        json.dump(new, f, indent=2, ensure_ascii=False)
    return {"status": "ok", "email": email}
