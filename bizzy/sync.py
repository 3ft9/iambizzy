import hashlib
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from .models import Block


class RetiredID(Exception):
    """The provider has permanently retired this event identifier."""


@dataclass(frozen=True)
class Appointment:
    key: str
    start: datetime | date
    end: datetime | date

    def end_instant(self, zone):
        if isinstance(self.end, datetime):
            return self.end
        return datetime.combine(self.end, time.min, ZoneInfo(zone))


def reconcile(rule, destination, writer, events, now, *, title="Busy", zone=None):
    events = list(events)  # Finish reading every page before changing any blocks.
    remaining = {b.occurrence: b for b in Block.objects.filter(rule=rule, destination=destination)}
    failure = None
    for event in events:
        key = hashlib.sha256(event.key.encode()).hexdigest()
        # All-day events end at midnight where the source calendar lives. The source stops
        # listing them then, and the block must count as finished at that same moment.
        ends = event.end_instant(zone or destination.zone)
        block, _ = Block.objects.get_or_create(
            rule=rule,
            destination=destination,
            occurrence=key,
            defaults={"ends": ends},
        )
        remaining.pop(key, None)
        # Persist the latest possible end until the provider confirms the write.
        if ends > block.ends:
            block.ends = ends
            block.save(update_fields=["ends"])
        try:
            try:
                writer.put(block.remote_id.hex, event, title=title)
            except RetiredID:
                block.remote_id = uuid.uuid4()
                block.save(update_fields=["remote_id"])
                writer.put(block.remote_id.hex, event, title=title)
        except Exception as exc:
            # One unwritable event must not stop the others or the stale removals below.
            failure = failure or exc
            continue
        if block.ends != ends:
            block.ends = ends
            block.save(update_fields=["ends"])
    for block in remaining.values():
        try:
            if block.ends > now:
                writer.remove(block.remote_id.hex)
        except Exception as exc:
            failure = failure or exc
            continue
        block.delete()
    if failure:
        raise failure
