import time as clock
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import recurring_ical_events
import requests
from icalendar import Calendar as ICalendar
from icalendar.timezone import tzp

from .security import unseal
from .sync import Appointment

# Errors in other properties (GEO, SEQUENCE, ...) do not affect busy time.
TIMING = {"DTSTART", "DTEND", "DURATION", "RRULE", "RDATE", "EXDATE", "RECURRENCE-ID"}


def parse_feed(body, start, end, default_zone):
    calendar = ICalendar.from_ical(body)
    if calendar.name != "VCALENDAR" or calendar.errors:
        raise ValueError("Invalid iCalendar document")
    zone = ZoneInfo(default_zone)
    if label := calendar.get("X-WR-TIMEZONE"):
        zone = tzp.timezone(str(label))
        if zone is None:
            raise ValueError("Unknown feed time zone")
        # Recurrence expansion accepts IANA names for X-WR-TIMEZONE.
        calendar["X-WR-TIMEZONE"] = zone

    recurring = set()
    for item in calendar.walk("VEVENT"):
        broken = {str(name).upper() for name, _ in item.errors}
        if broken & TIMING or "UID" not in item or "DTSTART" not in item:
            raise ValueError("Invalid calendar event")
        for name in broken:
            del item[name]  # unparsed values would break recurrence expansion
        rules = item.get("RRULE", [])
        for rule in rules if isinstance(rules, list) else [rules]:
            # One such rule expands to millions of occurrences in the window.
            if {str(f).upper() for f in rule.get("FREQ", [])} & {"SECONDLY", "MINUTELY"}:
                raise ValueError("Unsupported recurrence frequency")
        if any(k in item for k in ("RRULE", "RDATE", "RECURRENCE-ID")):
            recurring.add(str(item["UID"]))

    def localized(value):
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=zone)
        return value

    def instant(value):
        if isinstance(value, datetime):
            return value.astimezone(UTC)
        return datetime.combine(value, time.min, zone).astimezone(UTC)

    events, seen = [], set()
    for item in recurring_ical_events.of(calendar).between(
        start.astimezone(zone), end.astimezone(zone)
    ):
        if (
            str(item.get("TRANSP", "")).upper() == "TRANSPARENT"
            or str(item.get("STATUS", "")).upper() == "CANCELLED"
        ):
            continue
        begin, finish = localized(item.start), localized(item.end)
        if instant(finish) <= start or instant(begin) >= end or instant(begin) >= instant(finish):
            continue
        uid = str(item["UID"])
        key = uid
        if uid in recurring:
            original = localized(item.decoded("RECURRENCE-ID"))
            stamp = original.astimezone(UTC) if isinstance(original, datetime) else original
            key = f"{uid}\x00{stamp.isoformat()}"
            if key in seen:  # RANGE=THISANDFUTURE gives later occurrences the same RECURRENCE-ID
                key += "\x00" + instant(begin).isoformat()
        seen.add(key)
        events.append(Appointment(key, begin, finish))
    for item in calendar.walk("VFREEBUSY"):
        if item.errors:
            raise ValueError("Invalid free/busy component")
        periods = item.get("FREEBUSY", [])
        for period in periods if isinstance(periods, list) else [periods]:
            if str(period.params.get("FBTYPE", "BUSY")).upper() == "FREE":
                continue
            begin, finish = period.start, period.end
            if begin < end and finish > start:
                key = f"period\x00{item.get('UID', '')}\x00{begin.isoformat()}\x00{finish.isoformat()}"
                events.append(Appointment(key, begin, finish))
    return events


class Feed:
    def __init__(self, calendar):
        self.calendar = calendar

    def read(self, start, end):
        with requests.get(
            unseal(self.calendar.feed_secret), stream=True, timeout=(5, 30)
        ) as response:
            response.raise_for_status()
            data = bytearray()
            # The request timeout bounds each socket read, not the transfer. A slow feed
            # would otherwise hold the only poller thread.
            deadline = clock.monotonic() + 60
            while chunk := response.raw.read1(65536, decode_content=True):
                data.extend(chunk)
                if len(data) > 16 * 1024 * 1024:
                    raise ValueError("Feed exceeds 16 MiB")
                if clock.monotonic() > deadline:
                    raise TimeoutError("Feed took longer than 60 seconds")
        return parse_feed(bytes(data), start, end, self.calendar.zone)
