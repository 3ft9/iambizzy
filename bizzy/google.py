from datetime import date, datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from django.conf import settings
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .security import unseal
from .sync import Appointment, RetiredID

CALENDAR_LIST_SCOPE = "https://www.googleapis.com/auth/calendar.calendarlist.readonly"
EVENT_SCOPES = {
    "read": "https://www.googleapis.com/auth/calendar.events.readonly",
    "write": "https://www.googleapis.com/auth/calendar.events",
}
API = "https://www.googleapis.com/calendar/v3"


class ProviderError(Exception):
    """Messages may be shown to the operator. Never include tokens, URLs or response bodies."""


def tokens(**grant):
    reply = requests.post(
        "https://oauth2.googleapis.com/token",
        data=dict(
            client_id=settings.GOOGLE_CLIENT_ID,
            client_secret=settings.GOOGLE_CLIENT_SECRET,
            **grant,
        ),
        timeout=20,
    )
    if not reply.ok:
        try:
            code = reply.json().get("error")  # an error code, not a secret
        except (ValueError, AttributeError):
            code = None
        if code == "invalid_grant" and "refresh_token" in grant:
            raise ProviderError(
                "Google access was revoked or expired. Reconnect this Google account on Calendars."
            )
        raise ProviderError(
            f"Google token request failed (HTTP {reply.status_code}). Check authorization."
        )
    return reply.json()


class GoogleClient:
    def __init__(self, access_token):
        self.headers = {"Authorization": "Bearer " + access_token}
        # Writes are safe to retry: block IDs are client-supplied and a repeat insert is a 409.
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=None,
            raise_on_status=False,
        )
        self.session = requests.Session()
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    @classmethod
    def for_account(cls, account):
        access = tokens(grant_type="refresh_token", refresh_token=unseal(account.refresh_secret))
        return cls(access["access_token"])

    def request(self, method, path, *, accept=(), **kwargs):
        response = self.session.request(
            method, API + path, headers=self.headers, timeout=(5, 30), **kwargs
        )
        if not response.ok and response.status_code not in accept:
            raise ProviderError(f"Google Calendar request failed (HTTP {response.status_code}).")
        return response

    def pages(self, path, **params):
        while True:
            page = self.request("GET", path, params=params).json()
            yield from page.get("items", [])
            if not (token := page.get("nextPageToken")):
                return
            params["pageToken"] = token


class GoogleCalendar:
    def __init__(self, calendar, client):
        self.calendar = calendar
        self.client = client
        self.path = "/calendars/" + quote(calendar.external_id, safe="") + "/events"

    def read(self, start, end):
        def when(data):
            if "date" in data:
                return date.fromisoformat(data["date"])
            value = datetime.fromisoformat(data["dateTime"])
            return (
                value
                if value.tzinfo
                else value.replace(tzinfo=ZoneInfo(data.get("timeZone", self.calendar.zone)))
            )

        for item in self.client.pages(
            self.path,
            singleEvents="true",
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            maxResults=2500,
            fields="nextPageToken,items(id,start,end,status,transparency,extendedProperties/private,attendees(self,responseStatus))",
        ):
            if item.get("extendedProperties", {}).get("private", {}).get("iambizzy"):
                continue
            if item.get("status") == "cancelled" or item.get("transparency") == "transparent":
                continue
            if any(
                a.get("self") and a.get("responseStatus") == "declined"
                for a in item.get("attendees", [])
            ):
                continue
            yield Appointment(item["id"], when(item["start"]), when(item["end"]))

    def _lookup(self, identifier):
        response = self.client.request("GET", self.path + "/" + identifier, accept=(404, 410))
        if response.status_code == 404:
            return None
        if response.status_code == 410:
            raise RetiredID
        current = response.json()
        if current.get("status") == "cancelled":
            raise RetiredID
        if current.get("extendedProperties", {}).get("private", {}).get("iambizzy") != identifier:
            raise ProviderError(
                "Destination event has lost its ownership marker. Review it before syncing."
            )
        return current

    def put(self, identifier, event, *, title="Busy"):
        def when(value):
            return {"dateTime" if isinstance(value, datetime) else "date": value.isoformat()}

        payload = {
            "summary": title,
            "visibility": "private",
            "transparency": "opaque",
            "start": when(event.start),
            "end": when(event.end),
            "extendedProperties": {"private": {"iambizzy": identifier}},
            "reminders": {"useDefault": False},
        }
        current = self._lookup(identifier)
        if current is None:
            response = self.client.request(
                "POST", self.path, json={"id": identifier, **payload}, accept=(409,)
            )
            if response.status_code != 409:
                return
            current = self._lookup(identifier)
            if current is None:
                raise ProviderError("Google has not made the new event visible yet. Retry sync.")

        def matches(field, value):
            actual = current.get(field)
            if (
                field in ("start", "end")
                and actual
                and "dateTime" in value
                and "dateTime" in actual
            ):
                return datetime.fromisoformat(actual["dateTime"]) == datetime.fromisoformat(
                    value["dateTime"]
                )
            if field == "transparency":
                return current.get(field, "opaque") == value
            return actual == value

        if any(
            not matches(k, payload[k])
            for k in ("summary", "visibility", "transparency", "start", "end")
        ):
            # Full replace. PATCH merges start/end, so a timed block could not become all-day.
            self.client.request("PUT", self.path + "/" + identifier, json=payload)

    def remove(self, identifier):
        try:
            if self._lookup(identifier) is not None:
                self.client.request("DELETE", self.path + "/" + identifier, accept=(404, 410))
        except RetiredID:
            pass
