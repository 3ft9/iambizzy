from datetime import UTC, datetime
from itertools import combinations
from unittest.mock import Mock

from django.test import TestCase

from bizzy.google import GoogleCalendar
from bizzy.models import Calendar, Rule
from bizzy.worker import run_rule


class GoogleEventStore:
    """Retain Google's event payloads so the next rule reads the previous rule's writes."""

    def __init__(self, name, day):
        self.events = {
            name: {
                "id": name,
                "start": {"date": f"2026-03-0{day}"},
                "end": {"date": f"2026-03-0{day + 1}"},
            }
        }

    def pages(self, path, **params):
        return list(self.events.values())

    def request(self, method, path, **kwargs):
        if method == "GET":
            event = self.events.get(path.rsplit("/", 1)[1])
            return Mock(status_code=200 if event else 404, json=lambda: event)
        if method == "POST":
            event = kwargs["json"]
            assert event["id"] not in self.events
            self.events[event["id"]] = event
            return Mock(status_code=200)
        raise AssertionError(f"Unexpected {method} on unchanged appointments")


class FourCalendarTests(TestCase):
    def test_each_calendar_receives_exactly_one_block_per_direct_neighbor(self):
        calendars = [Calendar.objects.create(name=n, external_id=n, writable=True) for n in "ABCD"]
        stores = {c.pk: GoogleEventStore(c.name, day) for day, c in enumerate(calendars, 1)}
        adapters = {c.pk: GoogleCalendar(c, stores[c.pk]) for c in calendars}
        rules = [
            Rule.objects.create(source=a, destination=b, pair=a.name + b.name, bidirectional=True)
            for a, b in combinations(calendars, 2)
        ]

        def sync(connections):
            for rule in connections:
                run_rule(rule, datetime(2026, 3, 1, tzinfo=UTC), lambda c: adapters[c.pk])

        def assert_days(expected):
            for cal, days in zip(calendars, expected, strict=True):
                events = stores[cal.pk].events
                self.assertIn(cal.name, events)
                self.assertCountEqual([int(e["start"]["date"][-2:]) for e in events.values()], days)

        incomplete = [r for r in rules if r.pair != "AD"]
        sync(incomplete)
        sync(reversed(incomplete))
        assert_days([[1, 2, 3], [1, 2, 3, 4], [1, 2, 3, 4], [2, 3, 4]])

        sync(rules)
        identities = [set(stores[c.pk].events) for c in calendars]
        sync(reversed(rules))
        assert_days([[1, 2, 3, 4]] * 4)
        self.assertEqual([set(stores[c.pk].events) for c in calendars], identities)
