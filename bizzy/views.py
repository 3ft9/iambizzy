import hashlib
import secrets
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from django import forms
from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .google import CALENDAR_LIST_SCOPE, EVENT_SCOPES, GoogleClient, ProviderError, tokens
from .models import Account, Calendar, Rule
from .security import seal


def calendar_context():
    calendars = list(Calendar.objects.order_by("name", "pk"))
    destinations = [calendar for calendar in calendars if calendar.writable]
    return {
        "calendars": calendars,
        "destinations": destinations,
        "ready": len(calendars) >= 2 and bool(destinations),
    }


@require_GET
def home(request):
    seconds = settings.POLL_SECONDS
    for divisor, unit in [(3600, "hour"), (60, "minute"), (1, "second")]:
        if seconds % divisor == 0:
            count = seconds // divisor
            interval = f"{count} {unit}{'s' if count != 1 else ''}"
            break
    return render(
        request,
        "home.html",
        {
            **calendar_context(),
            "page": "connections",
            "rules": Rule.objects.select_related("source", "destination").order_by("pk"),
            "interval": interval,
            "months": settings.WINDOW_MONTHS,
        },
    )


@require_GET
def calendars(request):
    return render(
        request,
        "calendars.html",
        {
            **calendar_context(),
            "page": "calendars",
            "google_ready": bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET),
            "choices": request.session.get("choices", [])
            if "google_grant" in request.session
            else [],
        },
    )


@require_GET
def new_connection(request):
    return render(
        request,
        "new_connection.html",
        {
            **calendar_context(),
            "page": "connections",
            "labels": LabelForm(instance=Rule(bidirectional=True)),
        },
    )


@require_POST
def add_feed(request):
    url = request.POST.get("url", "").strip()
    name = request.POST.get("name", "").strip()
    zone = request.POST.get("zone", "UTC").strip()
    try:
        parts = urlsplit(url)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or not name
            or len(name) > 255
        ):
            raise ValueError
        ZoneInfo(zone)
    except (ValueError, OSError, ZoneInfoNotFoundError):  # OSError: over-long zone name
        return HttpResponseBadRequest("Enter a name, HTTP(S) feed URL and valid IANA time zone.")
    Calendar.objects.update_or_create(
        external_id="ics:" + hashlib.sha256(url.encode()).hexdigest(),
        defaults={"name": name, "provider": "ics", "zone": zone, "feed_secret": seal(url)},
    )
    return redirect("calendars")


class LabelForm(forms.ModelForm):
    class Meta:
        model = Rule
        fields = ["forward_identifier", "reverse_identifier"]
        widgets = {
            key: forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Blue"})
            for key in fields
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.bidirectional:
            del self.fields["reverse_identifier"]

    def clean(self):
        values = super().clean()
        for name, value in list(values.items()):
            if value and not value.isprintable():
                self.add_error(name, "Use printable characters for the identifier.")
        return values


@require_POST
def add_rule(request):
    try:
        source = Calendar.objects.get(pk=int(request.POST.get("source", "")))
        dest = Calendar.objects.get(pk=int(request.POST.get("destination", "")))
    except (ValueError, Calendar.DoesNotExist):
        return HttpResponseBadRequest("Select two calendars.")
    both = request.POST.get("direction") == "both"
    if source == dest or not dest.writable or (both and not source.writable):
        return HttpResponseBadRequest(
            "Select distinct calendars with write access in each destination."
        )
    labels = LabelForm(request.POST, instance=Rule(bidirectional=both))
    if not labels.is_valid():
        return HttpResponseBadRequest("Use up to 32 printable characters per identifier.")
    pair = "/".join(map(str, sorted([source.pk, dest.pk])))
    _, created = Rule.objects.get_or_create(
        pair=pair,
        defaults={
            "source": source,
            "destination": dest,
            "bidirectional": both,
            **labels.cleaned_data,
        },
    )
    if not created:
        return render(
            request,
            "new_connection.html",
            {
                **calendar_context(),
                "page": "connections",
                "labels": LabelForm(instance=Rule(bidirectional=True)),
                "error": "A connection already exists between these calendars.",
            },
            status=400,
        )
    return redirect("home")


@require_POST
def action(request, pk):
    rule = get_object_or_404(Rule, pk=pk)
    command = request.POST.get("action")
    if command == "sync" and rule.status in ("active", "cleanup"):
        Rule.objects.filter(pk=pk).update(next_run=timezone.now())
    elif (
        command in ("pause", "resume", "cleanup", "forget") and rule.status in ("active", "paused")
    ) or (
        command == "forget" and rule.status == "cleanup"
    ):  # way out of a cleanup that cannot finish
        state = {"pause": "paused", "resume": "active"}.get(command, command)
        Rule.objects.filter(pk=pk, status=rule.status).update(status=state, next_run=timezone.now())
    else:
        return HttpResponseBadRequest("Action unavailable for this connection.")
    messages.success(
        request,
        {
            "sync": "Sync queued. Refresh the dashboard to see the result.",
            "pause": "Connection paused.",
            "resume": "Connection resumed. Sync queued.",
            "cleanup": "Removal queued, including future busy blocks.",
            "forget": "Removal queued. Existing busy blocks will stay on the destination.",
        }[command],
    )
    return redirect("home")


def callback_url():
    return settings.PUBLIC_URL + "/oauth/callback"


@require_POST
def oauth_start(request):
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        return HttpResponseBadRequest("Configure Google OAuth client credentials first.")
    access = request.POST.get("access", "read")
    if access not in EVENT_SCOPES:
        return HttpResponseBadRequest("Choose source-only or read/write access.")
    request.session.pop("google_grant", None)
    request.session.pop("choices", None)
    request.session["oauth_access"] = access
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    params = dict(
        client_id=settings.GOOGLE_CLIENT_ID,
        redirect_uri=callback_url(),
        response_type="code",
        scope=f"{CALENDAR_LIST_SCOPE} {EVENT_SCOPES[access]}",
        access_type="offline",
        prompt="consent select_account",
        state=state,
    )
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))


@require_GET
def oauth_callback(request):
    expected = request.session.pop("oauth_state", None)
    if not expected or not secrets.compare_digest(
        expected.encode(), request.GET.get("state", "").encode()
    ):
        return HttpResponseBadRequest("Expired or invalid authorization. Start again.")
    access = request.session.pop("oauth_access", None)
    if access not in EVENT_SCOPES:
        return HttpResponseBadRequest("Start again and choose the access for this account.")
    if not (code := request.GET.get("code")):
        return HttpResponseBadRequest("Google authorization was cancelled.")
    try:
        grant = tokens(grant_type="authorization_code", code=code, redirect_uri=callback_url())
        # OAuth permits omitting scope if it matches the request.
        if "scope" in grant:
            granted = set(grant["scope"].split())
            if not {CALENDAR_LIST_SCOPE, EVENT_SCOPES[access]} <= granted:
                return HttpResponseBadRequest(
                    "Google did not grant the selected access. Reconnect and approve the requested calendar permissions."
                )
        client = GoogleClient(grant["access_token"])
        entries = list(
            client.pages("/users/me/calendarList", minAccessRole="reader", showHidden="true")
        )
        identity = next((e["id"] for e in entries if e.get("primary")), None)
        if not identity or not grant.get("refresh_token"):
            raise ProviderError("Google did not return a primary calendar and offline access.")
    except (ProviderError, requests.RequestException, KeyError):
        return HttpResponse(
            "Could not connect to Google. Check consent, Calendar API access and client configuration.",
            status=502,
        )
    request.session["choices"] = [
        {
            "id": e["id"],
            "name": e.get("summaryOverride") or e.get("summary", e["id"]),
            "writable": access == "write" and e.get("accessRole") in ("writer", "owner"),
            "zone": e.get("timeZone", "UTC"),
        }
        for e in entries
    ]
    request.session["google_grant"] = {
        "identity": identity,
        "access": access,
        "refresh_secret": seal(grant["refresh_token"]),
    }
    return redirect("calendars")


@require_POST
def select_calendars(request):
    chosen = set(request.POST.getlist("calendar"))
    grant = request.session.get("google_grant")
    if not grant:
        return HttpResponseBadRequest("Reconnect to Google and select calendars.")
    choices = {e["id"]: e for e in request.session.get("choices", [])}
    entries = [choices[key] for key in chosen if key in choices]
    with transaction.atomic():
        account = Account.objects.filter(identity=grant["identity"]).first()
        if not entries and not account:
            return HttpResponseBadRequest("Choose at least one calendar.")
        # The new grant replaces the old one, so it also governs calendars imported earlier.
        writable = {
            c.external_id: choices[c.external_id]["writable"]
            if c.external_id in choices
            else c.writable and grant["access"] == "write"
            for c in Calendar.objects.filter(account=account)
        } | {e["id"]: e["writable"] for e in entries}
        read_only = [key for key, allowed in writable.items() if not allowed]
        if Rule.objects.filter(
            Q(destination__external_id__in=read_only)
            | Q(bidirectional=True, source__external_id__in=read_only)
        ).exists():
            return HttpResponse(
                "A calendar on this account is a destination. Remove its destination connections "
                "before switching it to read-only, or reconnect with read/write access.",
                status=409,
            )
        account, _ = Account.objects.update_or_create(
            identity=grant["identity"],
            defaults={"access": grant["access"], "refresh_secret": grant["refresh_secret"]},
        )
        previous = list(
            Calendar.objects.filter(
                external_id__in=[e["id"] for e in entries], account__isnull=False
            ).values_list("account_id", flat=True)
        )
        for entry in entries:
            Calendar.objects.update_or_create(
                external_id=entry["id"],
                defaults={
                    "name": entry["name"],
                    "provider": "google",
                    "account": account,
                    "zone": entry["zone"],
                },
            )
        for calendar in Calendar.objects.filter(account=account):
            if calendar.writable != writable[calendar.external_id]:
                calendar.writable = writable[calendar.external_id]
                calendar.save(update_fields=["writable"])
        Account.objects.filter(pk__in=previous, calendar__isnull=True).delete()
    request.session.pop("google_grant")
    request.session.pop("choices", None)
    return redirect("home")


@require_http_methods(["GET", "POST"])
def edit_connection(request, pk):
    rule = get_object_or_404(Rule.objects.select_related("source", "destination"), pk=pk)
    if rule.status not in ("active", "paused"):
        return redirect("home")
    form = LabelForm(request.POST if request.method == "POST" else None, instance=rule)
    if request.method == "POST" and form.is_valid():
        Rule.objects.filter(pk=pk).update(**form.cleaned_data)
        messages.success(
            request, "Block titles saved. Changes apply on this connection’s next sync."
        )
        return redirect("home")
    return render(
        request,
        "edit_connection.html",
        {"page": "connections", "rule": rule, "labels": form},
        status=400 if form.errors else 200,
    )


@require_POST
def remove_calendar(request, pk):
    from django.db.models.deletion import ProtectedError

    try:
        with transaction.atomic():
            calendar = get_object_or_404(Calendar, pk=pk)
            account_id = calendar.account_id
            calendar.delete()
            if account_id:
                Account.objects.filter(pk=account_id, calendar__isnull=True).delete()
    except ProtectedError:
        return HttpResponse(
            "Remove this calendar's connections before disconnecting it.", status=409
        )
    return redirect("calendars")


@require_GET
def favicon(request):
    return FileResponse(
        Path(__file__).with_name("favicon.ico").open("rb"), content_type="image/x-icon"
    )
