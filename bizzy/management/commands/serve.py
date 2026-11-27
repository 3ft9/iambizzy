import os
import signal
import threading

from django.contrib.sessions.models import Session
from django.core.management import BaseCommand, call_command
from django.core.wsgi import get_wsgi_application
from django.utils import timezone
from waitress import serve

from bizzy.worker import poll


class Command(BaseCommand):
    help = "Migrate the database, start the sync worker and serve HTTP."

    def handle(self, *args, **options):
        call_command("migrate", interactive=False)
        Session.objects.filter(expire_date__lt=timezone.now()).delete()
        stop = threading.Event()
        worker = threading.Thread(target=poll, args=(stop,), daemon=True, name="calendar-poller")

        # Waitress drains its HTTP dispatcher on KeyboardInterrupt. Containers send SIGTERM.
        def terminate(signum, frame):
            stop.set()
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, terminate)
        worker.start()
        try:
            serve(
                get_wsgi_application(),
                host=os.getenv("BIND_HOST", "0.0.0.0"),
                port=int(os.getenv("PORT", "8080")),
                clear_untrusted_proxy_headers=True,
            )
        finally:
            stop.set()
            worker.join(timeout=5)
