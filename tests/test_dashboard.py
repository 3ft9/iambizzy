from django.test import TestCase

from bizzy.models import Calendar, Rule


class DashboardTests(TestCase):
    def test_onboarding_points_to_the_next_available_action(self):
        self.assertContains(self.client.get("/"), "Connect a calendar")
        Calendar.objects.create(name="Client", external_id="feed", provider="ics")
        self.assertContains(self.client.get("/"), "read/write access")
        Calendar.objects.create(name="Work", external_id="work", writable=True, provider="google")
        self.assertContains(self.client.get("/"), "Create a connection")
        self.assertContains(self.client.get("/connections/new"), "Choose a destination")

    def test_connected_dashboard_prioritizes_errors_and_keeps_setup_on_calendars_page(self):
        a, b = [
            Calendar.objects.create(name=n, external_id=n, writable=True)
            for n in ("Work", "Personal")
        ]
        Rule.objects.create(pair="1/2", source=a, destination=b, error="Sync failed")
        dashboard = self.client.get("/")
        self.assertContains(dashboard, "Needs attention")
        self.assertContains(dashboard, "Sync now")
        self.assertNotContains(dashboard, 'name="access"')
        self.assertContains(self.client.get("/calendars"), 'name="access"')

    def test_edit_connection_groups_management_actions(self):
        a, b = [Calendar.objects.create(name=n, external_id=n, writable=True) for n in ("A", "B")]
        rule = Rule.objects.create(source=a, destination=b, pair="a/b")
        url = f"/connections/{rule.pk}/edit"
        dashboard = self.client.get("/")
        self.assertContains(dashboard, f'href="{url}"')
        self.assertContains(dashboard, "Sync now")
        self.assertNotContains(dashboard, "Remove connection")
        edit = self.client.get(url)
        for action in (
            "Save block titles",
            "Pause connection",
            "Remove connection and blocks",
            "Remove connection and keep blocks",
        ):
            self.assertContains(edit, action)
        self.client.post(f"/rules/{rule.pk}", {"action": "pause"})
        self.assertContains(self.client.get(url), "Resume connection")
