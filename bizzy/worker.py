import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from .feeds import Feed
from .google import GoogleCalendar, GoogleClient, ProviderError
from .models import Rule
from .sync import reconcile

log = logging.getLogger(__name__)


def adapter(calendar):
    if calendar.provider == "ics":
        return Feed(calendar)
    return GoogleCalendar(calendar, GoogleClient.for_account(calendar.account))


def run_rule(rule, now, connect=adapter):
    pairs = [(rule.source, rule.destination, rule.forward_identifier)]
    if rule.bidirectional:
        pairs.append((rule.destination, rule.source, rule.reverse_identifier))
    # Fixed-length days keep the window end moving forward. Calendar months clamp at short
    # month ends, which made the end step backwards and drop blocks near the edge.
    end = now + timedelta(days=31 * settings.WINDOW_MONTHS)
    for source, destination, identifier in pairs:
        if not destination.writable:
            raise ProviderError(
                "Destination requires read/write access. Reconnect its Google calendar."
            )
        if rule.status == "cleanup":
            if not rule.block_set.filter(destination=destination).exists():
                continue
            events = []
        else:
            events = list(connect(source).read(now, end))
        reconcile(
            rule,
            destination,
            connect(destination),
            events,
            now,
            title=f"Busy ({identifier})" if identifier else "Busy",
            zone=source.zone,
        )


def tick():
    now = timezone.now()
    ids = Rule.objects.filter(
        status__in=["active", "cleanup", "forget"], next_run__lte=now
    ).values_list("pk", flat=True)
    for pk in list(ids):
        rule = Rule.objects.filter(pk=pk).first()
        if not rule or rule.status == "paused":
            continue
        # Claim the scheduled time before network I/O. A concurrent UI request can queue another run.
        claimed = Rule.objects.filter(pk=pk, status=rule.status, next_run=rule.next_run).update(
            next_run=now + timedelta(seconds=settings.POLL_SECONDS)
        )
        if not claimed:
            continue
        if rule.status == "forget":
            rule.delete()
            continue
        try:
            run_rule(rule, now)
        except Exception as exc:
            message = (
                str(exc)
                if isinstance(exc, ProviderError)
                else f"Sync failed ({type(exc).__name__}). Check source access and data."
            )
            # Retry soon: until this rule syncs, new appointments have no block.
            Rule.objects.filter(pk=pk).update(
                error=message,
                next_run=now + timedelta(seconds=min(settings.POLL_SECONDS, 300)),
            )
            expected = isinstance(exc, (ProviderError, requests.RequestException))
            log.warning("Rule %s: %s", pk, message, exc_info=not expected)
            continue
        if rule.status == "cleanup":
            rule.delete()
        else:
            Rule.objects.filter(pk=pk).update(last_success=timezone.now(), error="")


def poll(stop):
    while not stop.is_set():
        try:
            close_old_connections()
            tick()
        except Exception:
            log.exception("Poller failed")
        finally:
            close_old_connections()
        stop.wait(2)
