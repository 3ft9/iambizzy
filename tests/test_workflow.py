import base64
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from bizzy.google import GoogleClient
from bizzy.models import Account, Calendar, Rule
from bizzy.security import unseal
from bizzy.sync import Appointment
from bizzy.worker import run_rule, tick


class WebTests(TestCase):
    def test_connect_google_and_feed_then_pause_resume_and_remove(self):
        client = GoogleClient("unused")
        entries = [
            {"id": "me@example.org", "primary": True, "accessRole": "owner", "summary": "Work"}
        ]
        with override_settings(GOOGLE_CLIENT_ID="id", GOOGLE_CLIENT_SECRET="secret"):
            start = self.client.post("/oauth/start", {"access": "write"})
        self.assertIn("accounts.google.com", start.url)
        state = self.client.session["oauth_state"]
        with (
            patch(
                "bizzy.views.tokens",
                return_value={"access_token": "access", "refresh_token": "refresh"},
            ),
            patch.object(type(client), "pages", return_value=entries),
        ):
            response = self.client.get("/oauth/callback", {"code": "code", "state": state})
        self.assertRedirects(response, "/calendars")
        self.assertEqual(
            self.client.get("/oauth/callback", {"code": "code", "state": state}).status_code, 400
        )
        self.client.post("/calendars/select", {"calendar": "me@example.org"})
        self.assertEqual(unseal(Account.objects.get().refresh_secret), "refresh")
        self.client.post("/feeds", {"name": "Client", "url": "https://example.org/private.ics"})
        feed = Calendar.objects.get(provider="ics")
        dest = Calendar.objects.get(provider="google")
        self.assertNotIn("private.ics", feed.feed_secret)
        params = {"source": feed.pk, "destination": dest.pk}
        self.assertEqual(
            self.client.post("/rules", {**params, "direction": "both"}).status_code, 400
        )
        self.assertRedirects(self.client.post("/rules", params), "/")
        rule = Rule.objects.get()
        self.assertEqual(self.client.post("/rules", params).status_code, 400)
        self.client.post(f"/rules/{rule.pk}", {"action": "pause"})
        with patch("bizzy.worker.run_rule") as run:
            tick()
            run.assert_not_called()
            self.client.post(f"/rules/{rule.pk}", {"action": "resume"})
            tick()
            run.assert_called_once()
        rule.refresh_from_db()
        self.assertIsNotNone(rule.last_success)
        self.client.post(f"/rules/{rule.pk}", {"action": "cleanup"})
        tick()
        self.assertFalse(Rule.objects.exists())

    @override_settings(GOOGLE_CLIENT_ID="id", GOOGLE_CLIENT_SECRET="secret")
    def test_dashboard_preserves_origin_for_oauth_form(self):
        client = Client(enforce_csrf_checks=True)
        page = client.get("/calendars")
        self.assertEqual(page.headers["Referrer-Policy"], "same-origin")
        self.assertNotContains(page, '<meta name="referrer"')
        form = {"csrfmiddlewaretoken": client.cookies["csrftoken"].value}
        self.assertEqual(client.post("/oauth/start", form, HTTP_ORIGIN="null").status_code, 403)
        start = client.post("/oauth/start", form, HTTP_ORIGIN="http://testserver")
        self.assertEqual(start.status_code, 302)
        self.assertTrue(start.url.startswith("https://accounts.google.com/"))

    @override_settings(GOOGLE_CLIENT_ID="id", GOOGLE_CLIENT_SECRET="secret")
    def test_google_requests_only_the_selected_event_scope(self):
        for form, suffix in [
            ({}, ".readonly"),
            ({"access": "read"}, ".readonly"),
            ({"access": "write"}, ""),
        ]:
            with self.subTest(form=form):
                url = self.client.post("/oauth/start", form).url
                scopes = parse_qs(urlsplit(url).query)["scope"][0].split()
                self.assertCountEqual(
                    scopes,
                    [
                        "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
                        "https://www.googleapis.com/auth/calendar.events" + suffix,
                    ],
                )
        self.assertEqual(self.client.post("/oauth/start", {"access": "other"}).status_code, 400)

    def test_favicon_is_served(self):
        icon = self.client.get("/favicon.ico")
        self.assertEqual(icon.status_code, 200)
        self.assertEqual(b"".join(icon.streaming_content)[:4], bytes.fromhex("00000100"))

    def test_feed_form_rejects_bad_input_without_crashing(self):
        for bad in ({"url": "ftp://x/a.ics"}, {"zone": "A" * 300}, {"name": ""}):
            form = {"name": "Feed", "url": "https://example.com/a.ics", "zone": "UTC", **bad}
            self.assertEqual(self.client.post("/feeds", form).status_code, 400)

    def test_state_changes_require_csrf(self):
        self.assertEqual(Client(enforce_csrf_checks=True).post("/feeds").status_code, 403)
        self.assertEqual(self.client.get("/feeds").status_code, 405)

    @override_settings(BASIC_AUTH_USER="user", BASIC_AUTH_PASSWORD="pass")
    def test_optional_basic_auth(self):
        self.assertEqual(self.client.get("/").status_code, 401)
        token = base64.b64encode(b"user:pass").decode()
        self.assertEqual(self.client.get("/", HTTP_AUTHORIZATION="Basic " + token).status_code, 200)

    @override_settings(BASIC_AUTH_USER="user", BASIC_AUTH_PASSWORD="pass")
    def test_health_probe_needs_no_credentials_or_known_host(self):
        reply = self.client.get("/healthz", HTTP_HOST="10.10.0.1:23456")
        self.assertEqual((reply.status_code, reply.content), (200, b"ok"))

    def test_calendar_removal_requires_disconnecting_rules_first(self):
        a, b = [Calendar.objects.create(name=n, external_id=n, writable=True) for n in ("a", "b")]
        self.client.post("/rules", {"source": a.pk, "destination": b.pk})
        self.assertEqual(self.client.post(f"/calendars/{a.pk}/remove").status_code, 409)
        self.client.post(f"/rules/{Rule.objects.get().pk}", {"action": "forget"})
        tick()
        self.assertRedirects(self.client.post(f"/calendars/{a.pk}/remove"), "/calendars")
        self.assertFalse(Calendar.objects.filter(pk=a.pk).exists())


class LocalCalendar:
    def __init__(self, event):
        self.originals = [event]
        self.blocks = {}
        self.titles = {}

    def read(self, start, end):
        return self.originals

    def put(self, identifier, event, *, title="Busy"):
        self.titles[identifier] = title
        self.blocks[identifier] = event

    def remove(self, identifier):
        self.blocks.pop(identifier, None)


class LifecycleTests(TestCase):
    def test_labels_are_private_to_each_connection_and_direction(self):
        a, b, c = [
            Calendar.objects.create(name=n, external_id=n, writable=True)
            for n in ("Home", "Work", "Client")
        ]
        self.client.post(
            "/rules",
            {
                "source": a.pk,
                "destination": b.pk,
                "direction": "both",
                "reverse_identifier": "Work",
            },
        )
        rule = Rule.objects.get()
        self.client.post("/rules", {"source": a.pk, "destination": c.pk})
        now = timezone.now()
        calendars = {
            cal.pk: LocalCalendar(Appointment(cal.name, now, now + timedelta(hours=1)))
            for cal in (a, b, c)
        }

        def sync():
            for r in Rule.objects.all():
                run_rule(r, now, lambda cal: calendars[cal.pk])

        sync()
        (first,) = calendars[b.pk].blocks
        url = f"/rules/{rule.pk}/labels"
        self.assertRedirects(
            self.client.post(url, {"forward_identifier": " Blue ", "reverse_identifier": "Work"}),
            "/",
        )
        sync()
        self.assertEqual(calendars[b.pk].titles, {first: "Busy (Blue)"})
        self.assertEqual(list(calendars[a.pk].titles.values()), ["Busy (Work)"])
        self.assertEqual(list(calendars[c.pk].titles.values()), ["Busy"])
        self.client.post(url, {"forward_identifier": "", "reverse_identifier": "Work"})
        sync()
        self.assertEqual(calendars[b.pk].titles, {first: "Busy"})

    def test_bidirectional_sync_and_cleanup_only_removes_owned_blocks(self):
        a, b = [Calendar.objects.create(name=n, external_id=n, writable=True) for n in ("a", "b")]
        rule = Rule.objects.create(pair="a/b", source=a, destination=b, bidirectional=True)
        now = timezone.now()
        calendars = {
            c.pk: LocalCalendar(Appointment(c.name, now, now + timedelta(hours=1))) for c in (a, b)
        }

        def connect(c):
            return calendars[c.pk]

        run_rule(rule, now, connect)
        run_rule(rule, now, connect)
        for source, dest in [(a, b), (b, a)]:
            self.assertEqual(list(connect(dest).blocks.values()), connect(source).originals)
        rule.status = "cleanup"
        run_rule(rule, now, connect)
        for calendar in calendars.values():
            self.assertFalse(calendar.blocks)
            self.assertEqual(len(calendar.originals), 1)

    def test_window_end_never_moves_backwards_across_a_month_end(self):
        from datetime import UTC, datetime

        a, b = [Calendar.objects.create(name=n, external_id=n, writable=True) for n in ("a", "b")]
        rule = Rule.objects.create(pair="a/b", source=a, destination=b)
        ends = []

        class Source(LocalCalendar):
            def read(self, start, end):
                ends.append(end)
                return []

        for now in (datetime(2026, 11, 28, 23, tzinfo=UTC), datetime(2026, 11, 29, 1, tzinfo=UTC)):
            run_rule(rule, now, lambda cal: Source(None))
        self.assertLess(ends[0], ends[1])

    def test_failed_sync_retries_soon_and_stuck_cleanup_can_be_forgotten(self):
        a, b = [Calendar.objects.create(name=n, external_id=n, writable=True) for n in ("a", "b")]
        rule = Rule.objects.create(pair="a/b", source=a, destination=b)
        with patch("bizzy.worker.run_rule", side_effect=TimeoutError), self.assertLogs("bizzy"):
            tick()
        rule.refresh_from_db()
        self.assertTrue(rule.error)
        self.assertLessEqual(rule.next_run, timezone.now() + timedelta(minutes=5))

        Rule.objects.update(status="cleanup")
        self.assertContains(self.client.get("/"), "Remove and keep blocks")
        self.assertRedirects(self.client.post(f"/rules/{rule.pk}", {"action": "forget"}), "/")
        tick()
        self.assertFalse(Rule.objects.exists())
        self.assertEqual(
            self.client.post(f"/rules/{rule.pk}", {"action": "pause"}).status_code, 404
        )


@override_settings(GOOGLE_CLIENT_ID="id", GOOGLE_CLIENT_SECRET="secret")
class GooglePermissionTests(TestCase):
    def authorize(self, access, scope=None):
        entries = [
            {"id": "personal", "primary": True, "accessRole": "owner"},
            {"id": "work", "accessRole": "writer"},
            {"id": "client", "accessRole": "reader"},
        ]
        grant = {"refresh_token": access, "access_token": "token"}
        if scope is not None:
            grant["scope"] = scope
        self.client.post("/oauth/start", {"access": access})
        state = self.client.session["oauth_state"]
        with (
            patch("bizzy.views.tokens", return_value=grant),
            patch.object(GoogleClient, "pages", return_value=entries),
        ):
            return self.client.get("/oauth/callback", {"code": "code", "state": state})

    def test_reconnecting_an_account_replaces_its_grant_in_place(self):
        self.assertRedirects(self.authorize("read"), "/calendars")
        self.client.post("/calendars/select", {"calendar": ["personal", "work", "client"]})
        self.assertFalse(Calendar.objects.filter(writable=True).exists())

        # Upgrade: nothing selected, calendars imported earlier follow the new grant.
        self.assertRedirects(self.authorize("write"), "/calendars")
        self.assertRedirects(self.client.post("/calendars/select"), "/")
        account = Account.objects.get()
        self.assertEqual((account.access, unseal(account.refresh_secret)), ("write", "write"))
        source, dest, shared = [
            Calendar.objects.get(external_id=key) for key in ("personal", "work", "client")
        ]
        self.assertEqual([c.writable for c in (source, dest, shared)], [True, True, False])
        self.assertRedirects(
            self.client.post("/rules", {"source": source.pk, "destination": dest.pk}), "/"
        )

        # Downgrade is refused while a calendar on the account receives blocks.
        self.assertRedirects(self.authorize("read"), "/calendars")
        self.assertEqual(self.client.post("/calendars/select").status_code, 409)
        account.refresh_from_db()
        self.assertEqual((account.access, unseal(account.refresh_secret)), ("write", "write"))
        self.assertTrue(Calendar.objects.get(pk=dest.pk).writable)

        # A fresh token for the same mode keeps the connection.
        self.assertRedirects(self.authorize("write"), "/calendars")
        self.assertRedirects(self.client.post("/calendars/select"), "/")
        self.assertEqual((Account.objects.count(), Rule.objects.count()), (1, 1))

    def test_a_new_account_must_select_a_calendar(self):
        self.authorize("write")
        self.assertEqual(self.client.post("/calendars/select").status_code, 400)
        self.assertFalse(Account.objects.exists())

    def test_partial_consent_cannot_create_a_destination(self):
        response = self.authorize(
            "write", "https://www.googleapis.com/auth/calendar.calendarlist.readonly"
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Account.objects.exists())
        self.assertEqual(
            self.client.post("/calendars/select", {"calendar": "personal"}).status_code, 400
        )


class MigrationTests(TransactionTestCase):
    def test_upgrade_merges_an_account_with_two_grants_into_its_read_write_one(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        executor.migrate([("bizzy", "0004_connection_labels")])
        old = executor.loader.project_state([("bizzy", "0004_connection_labels")]).apps
        grants = {
            mode: old.get_model("bizzy", "Account").objects.create(
                identity="me", access=mode, refresh_secret=mode
            )
            for mode in ("read", "write")
        }
        old.get_model("bizzy", "Calendar").objects.create(
            name="a", external_id="a", account=grants["read"]
        )
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        account = Account.objects.get()
        self.assertEqual((account.access, account.refresh_secret), ("write", "write"))
        self.assertEqual(Calendar.objects.get().account, account)
