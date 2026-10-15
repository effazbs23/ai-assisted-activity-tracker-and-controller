from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from app.config import Settings
from app.odoo.client import OdooAPI
from app.services.errors import NotFound, ValidationFailed

ODOO_FMT = "%Y-%m-%d %H:%M:%S"
MAX_DURATION_MIN = 8 * 60


def _utc_str(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime(ODOO_FMT)


def _from_odoo(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, ODOO_FMT).replace(tzinfo=dt.timezone.utc)


def organizer_partner_id(odoo: OdooAPI, user_id: int) -> int:
    rows = odoo.search_read("res.users", [["id", "=", user_id]], ["partner_id"], limit=1)
    if not rows:
        raise NotFound("Odoo user not found")
    return rows[0]["partner_id"][0]


def parse_local(settings: Settings, value: str | dt.datetime) -> dt.datetime:
    """Interpret naive datetimes in the configured timezone; keep explicit offsets."""
    d = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(value)
    return d if d.tzinfo else d.replace(tzinfo=ZoneInfo(settings.timezone))


def busy_events(odoo: OdooAPI, partner_ids: list[int], start: dt.datetime, stop: dt.datetime) -> list[dict]:
    return odoo.search_read(
        "calendar.event",
        [["start", "<", _utc_str(stop)], ["stop", ">", _utc_str(start)], ["partner_ids", "in", partner_ids]],
        ["id", "name", "start", "stop"], limit=100, order="start")


def availability(odoo: OdooAPI, settings: Settings, partner_ids: list[int], day: dt.date, duration_min: int) -> dict:
    if not 5 <= duration_min <= MAX_DURATION_MIN:
        raise ValidationFailed(f"Duration must be 5..{MAX_DURATION_MIN} minutes")
    tz = ZoneInfo(settings.timezone)
    open_, close = (dt.datetime.combine(day, dt.time(h), tz) for h in (settings.work_start_hour, settings.work_end_hour))
    events = busy_events(odoo, partner_ids, open_, close)
    busy = sorted((_from_odoo(e["start"]), _from_odoo(e["stop"])) for e in events)
    slots, cursor, step = [], open_, dt.timedelta(minutes=duration_min)
    for b_start, b_stop in busy + [(close, close)]:
        while cursor + step <= min(b_start, close):
            slots.append({"start": cursor.isoformat(), "end": (cursor + step).isoformat()})
            cursor += step
        cursor = max(cursor, b_stop.astimezone(tz))
    return {"date": day.isoformat(), "timezone": settings.timezone,
            "busy": [{"name": e["name"], "start": _from_odoo(e["start"]).astimezone(tz).isoformat(),
                      "end": _from_odoo(e["stop"]).astimezone(tz).isoformat()} for e in events],
            "free_slots": slots[:20]}


def validate_event(odoo: OdooAPI, settings: Settings, organizer: int, attendees: list[int],
                   start: dt.datetime, duration_min: int) -> dict:
    if not 5 <= duration_min <= MAX_DURATION_MIN:
        raise ValidationFailed(f"Duration must be 5..{MAX_DURATION_MIN} minutes")
    now = dt.datetime.now(dt.timezone.utc)
    if start < now - dt.timedelta(minutes=5):
        raise ValidationFailed("Cannot book an event in the past")
    stop = start + dt.timedelta(minutes=duration_min)
    people = list(dict.fromkeys([organizer, *attendees]))
    found = odoo.search_read("res.partner", [["id", "in", people]], ["id", "name"], limit=len(people))
    if len(found) != len(people):
        raise NotFound("One or more attendees were not found")
    clash = busy_events(odoo, people, start, stop)
    if clash:
        raise ValidationFailed("Time conflict with: " + ", ".join(f"{e['name']} ({e['start']} UTC)" for e in clash))
    tz = ZoneInfo(settings.timezone)
    return {"start": start.astimezone(tz).isoformat(), "end": stop.astimezone(tz).isoformat(),
            "duration_min": duration_min, "attendees": [p["name"] for p in found], "attendee_ids": people}


def create_event(odoo: OdooAPI, settings: Settings, user_id: int, name: str, preview: dict, description: str = "") -> dict:
    start = dt.datetime.fromisoformat(preview["start"])
    stop = dt.datetime.fromisoformat(preview["end"])
    # Re-check right before writing: the slot may have been taken since the preview.
    if busy_events(odoo, preview["attendee_ids"], start, stop):
        raise ValidationFailed("That slot was taken in the meantime")
    event_id = odoo.create("calendar.event", {
        "name": name, "start": _utc_str(start), "stop": _utc_str(stop), "user_id": user_id,
        "partner_ids": [(6, 0, preview["attendee_ids"])], "description": description})
    return odoo.search_read("calendar.event", [["id", "=", event_id]], ["id", "name", "start", "stop"], limit=1)[0]
