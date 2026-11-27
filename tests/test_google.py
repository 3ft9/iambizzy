from datetime import UTC, datetime
from unittest.mock import Mock

from django.test import SimpleTestCase

from bizzy.google import GoogleCalendar
from bizzy.models import Calendar


class GoogleTests(SimpleTestCase):
    def test_bidirectional_read_skips_generated_blocks_and_follows_pages(self):
        client = Mock()
        event = {"id": "one", "start": {"date": "2026-03-01"}, "end": {"date": "2026-03-02"}}
        client.pages.return_value = iter(
            [
                {**event, "extendedProperties": {"private": {"iambizzy": "generated"}}},
                {**event, "transparency": "transparent"},
                {**event, "status": "cancelled"},
                {**event, "attendees": [{"self": True, "responseStatus": "declined"}]},
                event,
            ]
        )
        calendar = GoogleCalendar(Calendar(external_id="a/b", zone="UTC"), client)
        events = list(
            calendar.read(datetime(2026, 3, 1, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC))
        )
        self.assertEqual([e.key for e in events], ["one"])
        self.assertEqual(events[0].start.isoformat(), "2026-03-01")

    def test_google_writes_private_busy_only_and_repairs_manual_edits(self):
        from bizzy.sync import Appointment

        client = Mock()
        calendar = GoogleCalendar(Calendar(external_id="a/b", zone="UTC"), client)
        event = Appointment(
            "sensitive-source-id",
            datetime(2026, 3, 1, 10, tzinfo=UTC),
            datetime(2026, 3, 1, 11, tzinfo=UTC),
        )
        missing, created, ok = Mock(status_code=404), Mock(status_code=200), Mock(status_code=200)
        client.request.side_effect = [missing, created]
        calendar.put("012abc", event)
        method, path = client.request.call_args.args
        body = client.request.call_args.kwargs["json"]
        self.assertEqual((method, path), ("POST", "/calendars/a%2Fb/events"))
        self.assertEqual(
            (body["summary"], body["visibility"], body["transparency"]),
            ("Busy", "private", "opaque"),
        )
        self.assertNotIn(event.key, str(body))

        ok.json.return_value = {**body, "summary": "Edited"}
        client.request.side_effect = [ok, created]
        calendar.put("012abc", event)
        self.assertEqual(client.request.call_args.args[0], "PUT")
        ok.json.return_value = body
        client.request.reset_mock(side_effect=True)
        client.request.return_value = ok
        calendar.put("012abc", event)
        self.assertEqual(client.request.call_count, 1)

        calendar.put("012abc", event, title="Busy (Blue)")
        self.assertEqual(client.request.call_args.args, ("PUT", "/calendars/a%2Fb/events/012abc"))
        labeled = client.request.call_args.kwargs["json"]
        self.assertEqual(labeled["summary"], "Busy (Blue)")
        self.assertEqual(labeled["visibility"], "private")
        self.assertNotIn(event.key, str(labeled))

    def test_cleanup_refuses_unmarked_event(self):
        from bizzy.google import ProviderError

        client = Mock()
        client.request.return_value.json.return_value = {"id": "abc", "summary": "Personal"}
        calendar = GoogleCalendar(Calendar(external_id="a"), client)
        with self.assertRaises(ProviderError):
            calendar.remove("abc")
        self.assertEqual(client.request.call_count, 1)

    def test_google_client_fetches_every_page(self):
        from bizzy.google import GoogleClient

        client = GoogleClient("test")
        client.request = Mock(
            side_effect=[
                Mock(json=lambda: {"items": [1], "nextPageToken": "page-two"}),
                Mock(json=lambda: {"items": [2]}),
            ]
        )
        self.assertEqual(list(client.pages("/calendarList")), [1, 2])
        self.assertEqual(client.request.call_args.kwargs["params"]["pageToken"], "page-two")

    def test_block_changing_from_all_day_to_timed_is_replaced_whole(self):
        from bizzy.sync import Appointment

        client = Mock()
        calendar = GoogleCalendar(Calendar(external_id="a", zone="UTC"), client)
        current = Mock(status_code=200)
        current.json.return_value = {
            "summary": "Busy",
            "visibility": "private",
            "start": {"date": "2026-03-01"},
            "end": {"date": "2026-03-02"},
            "extendedProperties": {"private": {"iambizzy": "012abc"}},
        }
        client.request.return_value = current
        timed = Appointment(
            "k", datetime(2026, 3, 1, 10, tzinfo=UTC), datetime(2026, 3, 1, 11, tzinfo=UTC)
        )
        calendar.put("012abc", timed)
        self.assertEqual(client.request.call_args.args[0], "PUT")
        self.assertEqual(
            client.request.call_args.kwargs["json"]["start"],
            {"dateTime": "2026-03-01T10:00:00+00:00"},
        )

    def test_revoked_refresh_token_tells_the_operator_to_reconnect(self):
        from unittest.mock import patch

        from bizzy.google import ProviderError, tokens

        reply = Mock(ok=False, status_code=400, json=lambda: {"error": "invalid_grant"})
        with patch("bizzy.google.requests.post", return_value=reply):
            with self.assertRaisesMessage(ProviderError, "Reconnect"):
                tokens(grant_type="refresh_token", refresh_token="x")
