"""Calendar tools over ``www.googleapis.com/calendar/v3`` (read only)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any, Final

from .http import GoogleHttp, ToolError, drop_empty, encode_id, int_arg, list_arg, text_arg

BASE: Final = "https://www.googleapis.com/calendar/v3"
_EVENT_KEYS: Final = (
    "id",
    "status",
    "summary",
    "description",
    "location",
    "start",
    "end",
    "organizer",
    "attendees",
    "htmlLink",
    "hangoutLink",
)
_CALENDAR_KEYS: Final = ("id", "summary", "primary", "accessRole", "timeZone")
_WEEKDAYS: Final = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _local_midnight(day: date) -> datetime:
    local_zone = datetime.now().astimezone().tzinfo
    return datetime(day.year, day.month, day.day, tzinfo=local_zone)


def _named_day(word: str, today: date) -> date | None:
    if word == "today":
        return today
    if word == "tomorrow":
        return today + timedelta(days=1)
    if word == "yesterday":
        return today - timedelta(days=1)
    if word in _WEEKDAYS:
        return today + timedelta(days=(_WEEKDAYS.index(word) - today.weekday()) % 7)
    return None


def to_rfc3339(value: str, *, end: bool = False, today: date | None = None) -> str:
    """RFC 3339 from RFC 3339, a date, ``now``, ``today`` or a weekday name.

    A bare date or day word names the whole day, so as a window end (``end=True``) it
    means the start of the next day.
    """
    word = value.strip().casefold()
    if word == "now":
        return datetime.now().astimezone().isoformat()
    day = _named_day(word, today or datetime.now().astimezone().date())
    if day is None:
        try:
            day = date.fromisoformat(word)
        except ValueError:
            return _full_timestamp(value.strip())
    start = _local_midnight(day)
    return (start + timedelta(days=1) if end else start).isoformat()


def _full_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ToolError(f"cannot read {value!r} as a date or time") from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.isoformat()


def _window(args: Mapping[str, Any], *, required: bool) -> dict[str, str]:
    start = text_arg(args, "from", required=required)
    end = text_arg(args, "to", required=required)
    return drop_empty(
        {
            "timeMin": to_rfc3339(start) if start else "",
            "timeMax": to_rfc3339(end, end=True) if end else "",
        }
    )


def _trim(item: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: item[key] for key in keys if key in item}


async def calendar_list(http: GoogleHttp, args: Mapping[str, Any]) -> Any:
    """The calendars this account can see."""
    listing = await http.request("GET", f"{BASE}/users/me/calendarList")
    return {"calendars": [_trim(item, _CALENDAR_KEYS) for item in listing.get("items", [])]}


async def _calendar_events(
    http: GoogleHttp, calendar_id: str, params: Mapping[str, Any]
) -> dict[str, Any]:
    listing = await http.request(
        "GET", f"{BASE}/calendars/{encode_id(calendar_id)}/events", params=params
    )
    return {
        "calendarId": calendar_id,
        "events": [_trim(item, _EVENT_KEYS) for item in listing.get("items", [])],
    }


async def events(http: GoogleHttp, args: Mapping[str, Any]) -> Any:
    """Events inside a time window, one block per calendar (the primary by default)."""
    params = drop_empty(
        {
            **_window(args, required=False),
            "q": text_arg(args, "query"),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": int_arg(args, "max", default=50, ceiling=250),
        }
    )
    calendars = list_arg(args, "calendars") or ["primary"]
    return {"calendars": [await _calendar_events(http, cal, params) for cal in calendars]}


async def _resolve_calendar(http: GoogleHttp, value: str) -> str:
    """A calendar id from an id or a calendar name."""
    if value == "primary" or "@" in value:
        return value
    for item in (await calendar_list(http, {}))["calendars"]:
        if str(item.get("summary", "")).casefold() == value.casefold():
            return str(item["id"])
    return value


async def freebusy(http: GoogleHttp, args: Mapping[str, Any]) -> Any:
    """Busy blocks for one calendar; availability only, no event contents."""
    calendar_id = await _resolve_calendar(http, text_arg(args, "cal", required=True))
    window = _window(args, required=True)
    return await http.request(
        "POST", f"{BASE}/freeBusy", body={**window, "items": [{"id": calendar_id}]}
    )
