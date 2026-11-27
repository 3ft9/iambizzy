from datetime import UTC, datetime, timedelta

from django.test import SimpleTestCase

from bizzy.feeds import parse_feed

START = datetime(2026, 3, 1, tzinfo=UTC)
END = datetime(2026, 5, 1, tzinfo=UTC)


def calendar(*events):
    return (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Test//EN\n"
        + "".join(f"BEGIN:VEVENT\n{event}\nEND:VEVENT\n" for event in events)
        + "END:VCALENDAR"
    ).encode()


class FeedTests(SimpleTestCase):
    def test_weekly_busy_series_across_dst_with_exception_and_free_event(self):
        body = calendar(
            "UID:weekly\nDTSTART;TZID=Europe/London:20260322T100000\nDURATION:PT1H\n"
            "RRULE:FREQ=WEEKLY;COUNT=3\nEXDATE;TZID=Europe/London:20260405T100000",
            "UID:free\nDTSTART:20260322T100000Z\nDURATION:PT1H\nTRANSP:TRANSPARENT",
        )
        events = parse_feed(body, START, END, "UTC")
        self.assertEqual([e.start.astimezone(UTC).hour for e in events], [10, 9])
        self.assertEqual(len({e.key for e in events}), 2)
        self.assertTrue(all(e.end - e.start == timedelta(hours=1) for e in events))

    def test_availability_feed_keeps_only_busy_periods(self):
        body = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VFREEBUSY
UID:availability
DTSTAMP:20260101T000000Z
FREEBUSY;FBTYPE=FREE:20260301T100000Z/20260301T110000Z
FREEBUSY:20260302T100000Z/PT1H,20260302T120000Z/20260302T123000Z
END:VFREEBUSY
END:VCALENDAR"""
        events = parse_feed(body, START, END, "UTC")
        self.assertEqual([e.start.hour for e in events], [10, 12])
        self.assertEqual(len({e.key for e in events}), 2)

    def test_rescheduled_recurring_instance_keeps_key_and_all_day_boundaries(self):
        body = "UID:weekly\nDTSTART;VALUE=DATE:20260302\nDTEND;VALUE=DATE:20260303\nRRULE:FREQ=WEEKLY;COUNT=2"
        original = parse_feed(calendar(body), START, END, "UTC")
        moved = parse_feed(
            calendar(
                body,
                "UID:weekly\nRECURRENCE-ID;VALUE=DATE:20260309\nDTSTART;VALUE=DATE:20260310\nDTEND;VALUE=DATE:20260311",
            ),
            START,
            END,
            "UTC",
        )
        self.assertEqual({e.key for e in original}, {e.key for e in moved})
        self.assertEqual(sorted(e.end.isoformat() for e in moved), ["2026-03-03", "2026-03-11"])

    def test_windows_feed_zone_for_floating_event(self):
        body = calendar("UID:floating\nDTSTART:20260322T100000\nDURATION:PT1H").replace(
            b"VERSION:2.0", b"VERSION:2.0\nX-WR-TIMEZONE:Eastern Standard Time"
        )
        [event] = parse_feed(body, START, END, "UTC")
        self.assertEqual(event.start.astimezone(UTC).hour, 14)

    def test_errors_outside_timing_properties_do_not_reject_the_feed(self):
        body = calendar(
            "UID:ok\nDTSTART:20260322T100000Z\nDURATION:PT1H",
            "UID:untidy\nDTSTART:20260323T100000Z\nDURATION:PT1H\nGEO:abc\nSEQUENCE:",
        )
        self.assertEqual(len(parse_feed(body, START, END, "UTC")), 2)
        with self.assertRaises(ValueError):
            parse_feed(calendar("UID:bad\nDTSTART:20260322T100000Z\nDTEND:"), START, END, "UTC")

    def test_sub_hourly_recurrence_is_rejected(self):
        body = calendar("UID:flood\nDTSTART:20260322T100000Z\nDURATION:PT1M\nRRULE:FREQ=MINUTELY")
        with self.assertRaisesMessage(ValueError, "frequency"):
            parse_feed(body, START, END, "UTC")

    def test_this_and_future_override_keeps_one_key_per_occurrence(self):
        body = calendar(
            "UID:c\nDTSTART:20260303T100000Z\nDURATION:PT1H\nRRULE:FREQ=WEEKLY;COUNT=4",
            "UID:c\nRECURRENCE-ID;RANGE=THISANDFUTURE:20260310T100000Z\n"
            "DTSTART:20260310T120000Z\nDURATION:PT1H",
        )
        events = parse_feed(body, START, END, "UTC")
        self.assertEqual((len(events), len({e.key for e in events})), (4, 4))
