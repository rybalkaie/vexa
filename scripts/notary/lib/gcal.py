"""Минимальный Google Calendar client поверх OAuth-токенов google-calendar-mcp.

Используем те же файлы, что MCP-сервер `@cocal/google-calendar-mcp`:
  ~/.config/google-calendar-mcp/gcp-oauth.keys.json
  ~/.config/google-calendar-mcp/tokens.json

Это даёт нам refresh_token, который автоматически продлевается через
Google OAuth token endpoint. Не тянем google-auth/google-api-python-client
ради одного эндпоинта /calendar/v3/calendars/.../events.

API:
  load_credentials() → (access_token, refresh_token, client_id, client_secret)
  refresh_access_token(refresh_token, client_id, client_secret) → new_access_token
  list_events(calendar_id, time_min, time_max, *, recurring_event_id=None, event_id=None)
    → list[dict] — события на указанный интервал (instances для серий)
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

MCP_DIR = Path(os.path.expanduser("~/.config/google-calendar-mcp"))
KEYS_FILE = MCP_DIR / "gcp-oauth.keys.json"
TOKENS_FILE = MCP_DIR / "tokens.json"

TOKEN_URL = "https://oauth2.googleapis.com/token"
CAL_API_BASE = "https://www.googleapis.com/calendar/v3"

REQUEST_TIMEOUT = 15  # секунд


class GCalAuthError(RuntimeError):
    """OAuth refresh failed — credentials invalid/revoked."""


class GCalAPIError(RuntimeError):
    """Calendar API returned non-2xx."""


@dataclass
class Credentials:
    access_token: str
    refresh_token: str
    client_id: str
    client_secret: str
    expiry_epoch: float  # seconds since epoch; 0 = unknown


def load_credentials() -> Credentials:
    if not KEYS_FILE.exists():
        raise GCalAuthError(f"OAuth keys missing: {KEYS_FILE}")
    if not TOKENS_FILE.exists():
        raise GCalAuthError(f"OAuth tokens missing: {TOKENS_FILE} — нужен повторный логин Google")
    with KEYS_FILE.open("r", encoding="utf-8") as f:
        keys = json.load(f)
    inst = keys.get("installed") or keys.get("web") or {}
    client_id = inst.get("client_id", "")
    client_secret = inst.get("client_secret", "")
    if not (client_id and client_secret):
        raise GCalAuthError("OAuth keys malformed (no client_id/client_secret)")

    with TOKENS_FILE.open("r", encoding="utf-8") as f:
        tokens = json.load(f)
    n = tokens.get("normal") or {}
    refresh = n.get("refresh_token", "")
    access = n.get("access_token", "")
    if not refresh:
        raise GCalAuthError("OAuth tokens missing refresh_token — нужен повторный логин Google")
    # expiry_date в MCP — миллисекунды от epoch
    expiry_ms = n.get("expiry_date", 0)
    expiry = float(expiry_ms) / 1000.0 if expiry_ms else 0.0
    return Credentials(
        access_token=access,
        refresh_token=refresh,
        client_id=client_id,
        client_secret=client_secret,
        expiry_epoch=expiry,
    )


def refresh_access_token(creds: Credentials) -> Credentials:
    """Обновить access_token. Возвращает новый Credentials, не сохраняет на диск."""
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "refresh_token": creds.refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code == 401 or resp.status_code == 400:
        # invalid_grant — refresh_token отозван
        raise GCalAuthError(
            f"OAuth refresh failed (HTTP {resp.status_code}): {resp.text[:200]} — "
            "refresh_token отозван, нужен повторный логин Google"
        )
    if not resp.ok:
        raise GCalAPIError(f"OAuth refresh HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    new_access = data.get("access_token")
    if not new_access:
        raise GCalAuthError(f"OAuth refresh response без access_token: {data}")
    expires_in = data.get("expires_in", 3600)
    return Credentials(
        access_token=new_access,
        refresh_token=creds.refresh_token,
        client_id=creds.client_id,
        client_secret=creds.client_secret,
        expiry_epoch=time.time() + float(expires_in),
    )


def _ensure_fresh(creds: Credentials) -> Credentials:
    """Если access_token истекает в ближайшие 60 секунд — обновить."""
    if creds.expiry_epoch and creds.expiry_epoch > time.time() + 60:
        return creds
    return refresh_access_token(creds)


def list_instances(
    creds: Credentials,
    calendar_id: str,
    recurring_event_id: str,
    time_min: datetime,
    time_max: datetime,
) -> tuple[list[dict[str, Any]], Credentials]:
    """Вернуть instance-события серии за интервал [time_min, time_max].

    Google Calendar API эндпоинт `events/instances`:
      GET /calendars/{calendarId}/events/{recurringEventId}/instances
        ?timeMin=...&timeMax=...&singleEvents=true&maxResults=50

    Возвращает (events, updated_credentials). updated_credentials отличается
    если access_token был перевыпущен — вызвавшая сторона должна сохранить.
    """
    creds = _ensure_fresh(creds)
    url = f"{CAL_API_BASE}/calendars/{requests.utils.quote(calendar_id, safe='@.')}/events/{requests.utils.quote(recurring_event_id, safe='')}/instances"
    params = {
        "timeMin": _iso_utc(time_min),
        "timeMax": _iso_utc(time_max),
        "maxResults": 50,
        "showDeleted": "false",
    }
    headers = {"Authorization": f"Bearer {creds.access_token}"}
    resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 401:
        # Возможно токен только что протух — попробуем ещё раз с refresh.
        creds = refresh_access_token(creds)
        headers = {"Authorization": f"Bearer {creds.access_token}"}
        resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    if not resp.ok:
        raise GCalAPIError(f"Calendar API HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    items = data.get("items", [])
    return items, creds


def get_event(
    creds: Credentials,
    calendar_id: str,
    event_id: str,
) -> tuple[dict[str, Any] | None, Credentials]:
    """Прочитать одно событие по eventId. Возвращает (event_or_None, updated_creds).

    None — если событие удалено (404) или помечено отменённым.
    """
    creds = _ensure_fresh(creds)
    url = f"{CAL_API_BASE}/calendars/{requests.utils.quote(calendar_id, safe='@.')}/events/{requests.utils.quote(event_id, safe='')}"
    headers = {"Authorization": f"Bearer {creds.access_token}"}
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 401:
        creds = refresh_access_token(creds)
        headers = {"Authorization": f"Bearer {creds.access_token}"}
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 404:
        return None, creds
    if not resp.ok:
        raise GCalAPIError(f"Calendar API HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if data.get("status") == "cancelled":
        return None, creds
    return data, creds


def _iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_meeting_url(event: dict[str, Any]) -> str | None:
    """Достать ссылку на видеосвязь из события: conferenceData → hangoutLink → location.

    Возвращает первую найденную ссылку https:// (Telemost / Meet / Zoom — без фильтра здесь,
    фильтрация по платформе — в эвристике scheduler-а).
    """
    cd = event.get("conferenceData") or {}
    for ep in cd.get("entryPoints", []):
        uri = ep.get("uri", "")
        if uri.startswith("https://"):
            return uri
    hangout = event.get("hangoutLink")
    if hangout and hangout.startswith("https://"):
        return hangout
    loc = event.get("location") or ""
    # Простейший поиск ссылки в location.
    import re

    m = re.search(r"https?://\S+", loc)
    if m:
        return m.group(0)
    desc = event.get("description") or ""
    m = re.search(r"https?://\S+", desc)
    if m:
        return m.group(0)
    return None


def event_start_dt(event: dict[str, Any]) -> datetime | None:
    """Достать datetime начала события (с TZ). None если all-day или нет start."""
    start = event.get("start") or {}
    dt_str = start.get("dateTime")
    if not dt_str:
        return None
    # Google возвращает RFC-3339 с офсетом.
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except ValueError:
        return None
