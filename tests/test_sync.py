from datetime import UTC, datetime, timedelta

from django.test import TestCase

from bizzy.models import Calendar, Rule
from bizzy.sync import Appointment, reconcile

NOW = datetime(2026, 1, 1, tzinfo=UTC)
MEETING = Appointment("meeting", NOW + timedelta(days=1), NOW + timedelta(days=1, hours=1))


class Destination:
    def __init__(self):
        self.events = {}

    def put(self, remote_id, event, *, title):
        self.events[remote_id] = event

    def remove(self, remote_id):
        self.events.pop(remote_id, None)


class SyncTests(TestCase):
    def setUp(self):
        a, b = [Calendar.objects.create(name=n, external_id=n) for n in ("a", "b")]
        self.rule = Rule.objects.create(source=a, destination=b, pair="1/2")
        self.dest = Destination()

    def sync(self, events):
        reconcile(self.rule, self.rule.destination, self.dest, events, NOW)

    def test_sync_twice_creates_one_block(self):
        self.sync([MEETING])
        self.sync([MEETING])
        self.assertEqual(list(self.dest.events.values()), [MEETING])

    def test_overlapping_blocks_move_and_cancel_independently(self):
        other = Appointment("other", MEETING.start, MEETING.end)
        moved = Appointment(
            MEETING.key, MEETING.start + timedelta(hours=2), MEETING.end + timedelta(hours=2)
        )
        self.sync([MEETING, other])
        first, second = self.dest.events
        self.sync([moved])
        self.assertEqual(self.dest.events, {first: moved})
        self.assertNotIn(second, self.dest.events)

    def test_retry_after_remote_write_reuses_identity(self):
        put = self.dest.put

        def lost_response(remote_id, event, *, title):
            put(remote_id, event, title=title)
            raise TimeoutError()

        self.dest.put = lost_response
        with self.assertRaises(TimeoutError):
            self.sync([MEETING])
        first = next(iter(self.dest.events))
        self.dest.put = put
        self.sync([MEETING])
        self.assertEqual(self.dest.events, {first: MEETING})

    def test_partial_source_failure_preserves_blocks(self):
        self.sync([MEETING])

        def broken_source():
            yield Appointment("other", MEETING.start, MEETING.end)
            raise TimeoutError()

        with self.assertRaises(TimeoutError):
            self.sync(broken_source())
        self.assertEqual(list(self.dest.events.values()), [MEETING])

    def test_finished_blocks_remain_on_destination(self):
        self.sync([MEETING])
        reconcile(self.rule, self.rule.destination, self.dest, [], NOW + timedelta(days=2))
        self.assertEqual(list(self.dest.events.values()), [MEETING])
        self.assertFalse(self.rule.block_set.exists())

    def test_cancel_after_lost_reschedule_response_removes_extended_block(self):
        self.sync([MEETING])
        original_put = self.dest.put

        def write_then_timeout(remote_id, event, *, title):
            original_put(remote_id, event, title=title)
            raise TimeoutError()

        self.dest.put = write_then_timeout
        longer = Appointment(MEETING.key, MEETING.start, MEETING.end + timedelta(days=5))
        with self.assertRaises(TimeoutError):
            self.sync([longer])
        reconcile(self.rule, self.rule.destination, self.dest, [], MEETING.end + timedelta(days=1))
        self.assertFalse(self.dest.events)

    def test_one_failed_write_does_not_stop_other_events_or_removals(self):
        stale = Appointment("stale", MEETING.start, MEETING.end)
        self.sync([stale])
        original_put = self.dest.put

        def reject_bad(remote_id, event, *, title):
            if event.key == "bad":
                raise ValueError()
            original_put(remote_id, event, title=title)

        self.dest.put = reject_bad
        bad = Appointment("bad", MEETING.start, MEETING.end)
        later = Appointment("later", MEETING.start, MEETING.end)
        with self.assertRaises(ValueError):
            self.sync([MEETING, bad, later])
        self.assertCountEqual(self.dest.events.values(), [MEETING, later])

    def test_all_day_block_finishes_at_midnight_in_the_source_zone(self):
        from datetime import date

        self.rule.destination.zone = "America/Los_Angeles"
        day = Appointment("day", date(2026, 1, 2), date(2026, 1, 3))
        args = (self.rule, self.rule.destination, self.dest)
        reconcile(*args, [day], NOW, zone="Pacific/Auckland")
        # Auckland has finished the day. Los Angeles has not.
        reconcile(*args, [], datetime(2026, 1, 2, 12, tzinfo=UTC), zone="Pacific/Auckland")
        self.assertEqual(list(self.dest.events.values()), [day])
