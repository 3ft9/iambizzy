# Developing iAmBizzy

The application uses Python, Django’s ORM and migrations, server-rendered HTML and a Waitress HTTP server. Local development requires uv. The checkout pins Python 3.13 and includes a dependency lockfile.

## Run locally

```sh
uv sync --locked
uv run python manage.py serve
```

Open http://localhost:8080. The serve command migrates the database and starts both the UI and the poller. Export settings in your shell or use `uv run --env-file .env python manage.py serve` to load a local environment file. For template development, `manage.py migrate` followed by `manage.py runserver` gives you Django’s development server. Only the serve command runs the background worker.

## Verify a change

```sh
uv run ruff check .
uv run ruff format --check .
uv run python manage.py makemigrations --check --dry-run
uv run python manage.py test
docker build -t iambizzy:dev .
```

The default tests use a temporary SQLite database and fake provider responses. For PostgreSQL, start a disposable database and run the suite with the same settings as a deployment:

```sh
docker run --rm -d --name bizzy-dev-db -p 127.0.0.1:55432:5432 -e POSTGRES_PASSWORD=test postgres:17
# Wait until the service reports that it is ready.
docker exec bizzy-dev-db pg_isready -U postgres
DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres uv run python manage.py test
docker stop bizzy-dev-db
```

Django’s test runner needs a PostgreSQL role that can create and drop a temporary test database. CI exercises both engines, checks migration drift and builds the container for amd64 and arm64. Pull requests build the image. Default-branch and version-tag pushes can publish to GHCR with the repository’s package-write permission. A newly published package may require a visibility change in GitHub for anonymous pulls.

For sync changes, start with a failing behavioral test, then run the smallest relevant suite while implementing the change. For example, use `uv run python manage.py test tests.test_sync` for reconciliation. Provider fixtures should contain synthetic events. Never include subscription URLs, customer events or tokens in test data.

## How a sync works

1. The web UI persists a rule and its next due time. The poller claims that scheduled time and runs the two directions sequentially when requested. The app targets a single worker and one process per deployment.
2. A source adapter returns a complete snapshot of busy occurrences overlapping the rolling window. Google follows every page with recurring instances expanded. ICS reads a full subscription, expands recurrences, handles date-only boundaries and uses recurrence IDs for moved exceptions. Each normalized appointment contains only a stable source key, start and exclusive end.
3. Reconciliation hashes the source key and uses a unique mapping per rule, destination and occurrence. The mapping stores a destination UUID **before** issuing a remote write. A retry uses the same Google event ID after a lost response or process restart. Google events carry a private ownership marker. The writer checks that marker before changing or deleting an existing event.
4. Missing future occurrences cause removal of the corresponding managed blocks. Finished mappings expire locally. A cleanup request uses an empty desired set and requires only destination access. Forgetting a connection drops its mappings and leaves remote events in place.
5. Provider failures leave the rule available for retry. The UI records a sanitized error and the last successful completion. Keep network calls outside database transactions. A remote write can succeed independently of the process that initiated it.

## Code map

| Module | Responsibility |
| --- | --- |
| `bizzy/views.py`, `bizzy/templates/` | Calendar setup, OAuth state, rule lifecycle and operator dashboard |
| `bizzy/worker.py` | Scheduling, adapter selection and direction orchestration |
| `bizzy/sync.py` | Normalized appointment and idempotent reconciliation |
| `bizzy/google.py`, `bizzy/feeds.py` | Provider authentication, reads and Google writes |
| `bizzy/models.py`, `bizzy/migrations/` | Portable schema and database invariants |
| `bizzy/security.py`, `bizzy/settings.py` | Credential encryption, optional Basic Auth and runtime configuration |

Google OAuth binds the requested access mode to the server-side session alongside the authorization state. Each mode requests calendar-list read access and its corresponding event scope. There is one stored grant per Google identity, replaced in place on reconnect. Calendar write capability requires both a read/write grant and an owner or writer role on that calendar.

## Dashboard assets

The Connections, Calendars and New connection pages share a Django base template. WhiteNoise serves the local files in `bizzy/static/` in the container. Tabler Core 1.7.5 supplies the component CSS. The project theme and the connection form’s progressive enhancement are in `dashboard.css` and `connection.js`. The form also works with JavaScript disabled and the server enforces calendar permissions.

To update Tabler, extract `dist/css/tabler.min.css` from the chosen `@tabler/core` npm release, replace the vendored CSS and its MIT license in `bizzy/static/vendor/tabler/`, and remove the trailing source-map reference. Check the empty dashboard, rule actions and calendar setup at desktop and mobile widths. Node and a CDN are unnecessary for ordinary builds and deployments.

## Extend a provider or database

A source implements `read(start, end)` and returns appointments with stable occurrence keys. Timed values must be timezone-aware. All-day values use date objects with an exclusive end. Read every page, exclude the app’s own generated blocks, and raise if the snapshot is incomplete. Returning an empty list means the source was successfully read and had zero matching appointments.

A writable adapter adds `put(identifier, event, *, title)` and `remove(identifier)`. The worker derives the title from the connection’s optional identifier for the current direction. A title change keeps the same remote event ID. Both operations must tolerate retries and check ownership. If the provider permanently retires a deleted ID, raise `RetiredID` on put so reconciliation can reserve a fresh ID. Add the provider to the connection flow, declare writable capabilities, and register adapter construction in the worker. Test a retry after remote success, moved and cancelled instances, overlaps and loop suppression before enabling a new destination type.

The ORM keeps provider code independent of database SQL. For another Django-supported database, add its driver and URL configuration if needed, run migrations against an empty database, then run the full integration suite there. Include the new backend in CI before advertising support.

For a live acceptance check, use two disposable Google calendars. Connect them in both directions, create an overlapping pair and a recurring event, and run **Sync now**. Check the event counts, privacy and date boundaries on both calendars. Move one instance, mark another free, cancel one, then sync twice. Finally remove the connection with cleanup and confirm only the generated future blocks disappear.
