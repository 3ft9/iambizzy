import uuid

from django.db import models
from django.utils import timezone


class Account(models.Model):
    # One grant per Google account. Reconnecting replaces it in place.
    identity = models.CharField(max_length=512, unique=True)
    access = models.CharField(max_length=5, default="read")
    refresh_secret = models.TextField()


class Calendar(models.Model):
    name = models.CharField(max_length=255)
    provider = models.CharField(max_length=12)
    external_id = models.CharField(max_length=1024, unique=True)
    feed_secret = models.TextField(blank=True)
    account = models.ForeignKey(Account, null=True, on_delete=models.PROTECT)
    writable = models.BooleanField(default=False)
    zone = models.CharField(max_length=128, default="UTC")


class Rule(models.Model):
    source = models.ForeignKey(Calendar, related_name="+", on_delete=models.PROTECT)
    destination = models.ForeignKey(Calendar, related_name="+", on_delete=models.PROTECT)
    pair = models.CharField(max_length=64, unique=True)
    bidirectional = models.BooleanField(default=False)
    forward_identifier = models.CharField(max_length=32, blank=True, default="")
    reverse_identifier = models.CharField(max_length=32, blank=True, default="")
    status = models.CharField(max_length=12, default="active")
    next_run = models.DateTimeField(default=timezone.now)
    last_success = models.DateTimeField(null=True)
    error = models.TextField(blank=True)


class Block(models.Model):
    rule = models.ForeignKey(Rule, on_delete=models.CASCADE)
    destination = models.ForeignKey(Calendar, on_delete=models.PROTECT)
    occurrence = models.CharField(max_length=64)
    remote_id = models.UUIDField(default=uuid.uuid4, unique=True)
    ends = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["rule", "destination", "occurrence"], name="one_block_per_occurrence"
            )
        ]
