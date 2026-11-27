# iAmBizzy

iAmBizzy copies busy time between calendars. Connect a personal Google calendar and a client’s ICS feed, for example, and each busy appointment from the client appears as a private **Busy** event on Google. The destination gets the times and availability. Titles, attendees, descriptions, meeting links and locations stay on the source.

| Calendar source | Read busy events | Create blocks |
| --- | --- | --- |
| Google Calendar with OAuth | Yes | With write access |
| HTTP(S) ICS subscription, including published Microsoft 365 and Zoho feeds | Yes | No |

Choose one-way sync or connect two writable Google calendars in both directions. Overlapping appointments create separate blocks. The default poll interval is one hour and the rolling window is three months of 31 days each. A failed sync retries within five minutes. Bookings made between polls can still overlap. Feed publishers may also cache their calendars.

## Quick start

From the project directory, with Docker and Compose 2.24 or newer installed, run

```sh
cp .env.example .env
docker compose up --build -d
```

Open **http://localhost:8080**. Compose binds the port to the local machine and saves the database and encryption key in a named volume. Open **Calendars** to add an ICS source now or follow the Google setup to add a writable destination. Apply changes to the environment with `docker compose up -d`.

The GitHub Actions workflow publishes a multi-architecture image to `ghcr.io/<owner>/<repository>` on default-branch pushes and version tags. After a successful publish, you can run that image directly, with a volume mounted at `/data`. The same image runs in Docker Compose, Nomad, or another OCI runtime.

## Connect Google

1. In Google Cloud Console, create or select a project and enable **Google Calendar API**.
2. Configure Google Auth Platform branding, audience and data access. For an external app in testing, add each Google account as a test user. Configure the Calendar scopes `calendar.calendarlist.readonly`, `calendar.events.readonly` and `calendar.events` under `https://www.googleapis.com/auth/`.
3. Create an OAuth client of type **Web application**. Add the exact authorized redirect URI `http://localhost:8080/oauth/callback`. For a private deployment behind HTTPS, use its browser-facing URL followed by `/oauth/callback`.
4. Set `PUBLIC_URL`, `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in the environment. Recreate the container and open the UI at that same public URL.
5. On **Calendars**, choose **Source only (read-only)** to copy busy time from Google, or **Destination or both directions (read/write)** to create blocks there. Click **Connect Google account**, grant access, select one or more calendars and click **Save**. Repeat for other accounts. The calendar list shows which ones allow writes.

Each Google account has one authorization. Connecting the same account again replaces it in place: its calendars, connections and busy blocks stay as they are. Do this when Google access has expired or been revoked, or to change the account between source-only and read/write. You need not select any calendar unless you are adding one. Remove any connections that write to the account’s calendars before switching it to source-only. Calendars shared with you as a reader remain source-only in either mode. Upgrading merges an account that had both kinds of authorization into its read/write one.

Google requires HTTPS for ordinary web redirect domains and allows HTTP on localhost. The callback is a browser redirect, so a private HTTPS hostname reachable by your browser works. The container needs outbound access to Google. External apps in testing can receive refresh tokens that expire after seven days. If consent expires, reconnect the account and reselect its calendars. Workspace policies can require administrator approval.

## Create and operate a connection

1. To add an ICS source, enter a name and the full subscription URL supplied by the publisher. Use an HTTP(S) feed that returns iCalendar data. For a webcal subscription, use its HTTPS URL. The time zone field accepts an IANA name such as `Europe/London` for floating event times.
2. Choose the source, destination and direction under **Connections → Create a connection**. Only writable Google calendars appear as destinations. Creating a connection queues its first sync immediately.
3. Refresh the dashboard to see the last successful sync or error. **Sync now** queues a run without waiting for the next poll. Edit a test appointment and sync again to check its block moves. A failed source read leaves existing blocks in place.
4. Select **Edit** beside a connection to change its block titles, pause or resume syncing, or remove it. **Pause connection** suspends subsequent polls. A run already in progress may finish. **Resume** queues a fresh run. Under **Remove connection**, choose whether to delete future and ongoing managed blocks or leave them on the destination. If block removal cannot finish, for example because Google access was revoked, choose **Remove and keep blocks** on the dashboard. Finished blocks stay in calendar history. If you keep blocks, the app relinquishes them and a new connection can create additional blocks for the same appointments.
5. Disconnect a calendar after removing its connections. Removing the last imported calendar for an account also removes its saved refresh token. You can independently revoke the app’s access in your Google account.

Set an optional **block label** when creating a connection, or choose **Edit** beside the connection later. For example, use `Client` on the Personal → Work connection and leave the Personal → Shared connection blank. Work receives `Busy (Client)` and Shared receives `Busy` for the same appointment. Anyone who can read the title can see its label. Two-way connections have a separate label for each destination. Changes update existing managed blocks in the sync window on the connection’s next successful sync. Upgrading copies previous calendar labels to existing connections in the corresponding direction. New connections start with blank labels.

Google and ICS events marked free or cancelled produce no block. Google invitations declined by the source calendar are also skipped. Recurring events are expanded within the window and all-day events retain their calendar dates. An ICS availability-only feed can supply busy periods directly. If its publisher combines multiple overlapping appointments into a single period, that single period is all iAmBizzy receives. Google blocks created by iAmBizzy are excluded from subsequent Google source reads. Connect each desired pair directly. Avoid adding an ICS re-export of a destination in a circular chain because feed publishers may discard ownership information.

## Deployment settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `8080` | HTTP listening port |
| `BIND_HOST` | `0.0.0.0` | Listening interface inside the container |
| `PUBLIC_URL` | `http://localhost:8080` | Browser-facing origin, including any nonstandard port |
| `POLL_SECONDS` | `3600` | Interval between scheduled sync attempts |
| `WINDOW_MONTHS` | `3` | Rolling look-ahead in 31-day months |
| `DATABASE_URL` | SQLite in the data directory | PostgreSQL example: `postgresql://bizzy:password@db:5432/bizzy` |
| `DATA_DIR` | `/data` in Docker, `./data` locally | Persistent directory for the default database and key |
| `APP_SECRET` | Generated in `DATA_DIR/secret` | Persistent secret for credential encryption and Django signing |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Empty | Google OAuth web application credentials |
| `BASIC_AUTH_USER`, `BASIC_AUTH_PASSWORD` | Empty | Set both to enable HTTP Basic Auth for the entire UI |

**Access is unrestricted by default.** Anyone who can reach the UI can manage connected calendars and configure feed URLs fetched from the container’s network. Restrict it to trusted operators using private networking, an authenticated proxy, or Basic Auth over HTTPS. Forward the browser’s Host header through a reverse proxy and set `PUBLIC_URL` to the external HTTPS origin. Keep proxy access logs from retaining OAuth callback query strings.

Run **one application replica per database**. It serves HTTP, applies migrations at startup and runs the poller. On Nomad, use one allocation with a persistent volume mounted at `/data`, supply settings through the job environment or a secrets template, and route your chosen port. `GET /healthz` returns 200 without credentials for any Host header, for use as the health check. The image runs as UID/GID 10001. Give that account write access to bind-mounted data. With PostgreSQL, create the database first and grant the application user permission to create and alter its tables.

Back up the database **and the same encryption key**. Google refresh tokens and ICS URLs are encrypted in the database. A new key cannot decrypt existing credentials. Stop the instance before copying a SQLite volume. For PostgreSQL use a consistent database backup and save the key separately. Restore both before starting a replacement instance. Keep the data volume across upgrades and take a backup before applying migrations.

See the [developer guide](docs/development.md) for local setup, tests, architecture and extension points. Released under the [MIT License](LICENSE).
