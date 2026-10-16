# MD57 Car Wash

Flask/Waitress application with SQLite, staff accounts, bookings, quality checks,
pickup confirmation and reports. Python 3.14 is used for validation. Git includes
source and tests; private configuration, databases, logs, employee transfers and
backups are excluded.

## Local setup

For a new installation only, copy `.env.example` to `.env`, replace placeholders,
and set `DEFAULT_ADMIN_PASSWORD` to a private password meeting the policy.
Preserve an existing `.env` and `SECRET_KEY`: protected data depends on it.

```powershell
python -m pip install -r requirements.txt
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\manage-server.ps1 start
```

Open http://127.0.0.1:5000. The script also supports `status`, `restart` and `stop`.
The equivalent Python command is `python _Backend/runtime.py`. Both that command
and `python _Backend/app.py` construct one application.

## Render hosting

Use a Python Web Service with the repository root as its working directory:

```text
Build: pip install -r requirements.txt
Start: python _Backend/runtime.py --host 0.0.0.0 --port $PORT
```

Configure these on the **Flask host**:

| Variable | Purpose |
| --- | --- |
| `SECRET_KEY` | Existing application secret, or a random secret for an empty installation |
| `DEFAULT_ADMIN_PASSWORD` | Initial Owner password; never overwrites an existing account |
| `PUBLIC_BASE_URL` | One public HTTPS origin without a path |
| `SESSION_COOKIE_SECURE=true` | HTTPS-only sessions |
| `MD57_BACKGROUND_JOBS=true` | Scheduled maintenance while the process runs |
| `MAIL_TRANSPORT=brevo` | HTTPS email transport |
| `BREVO_API_KEY` | Private Brevo API key |
| `MAIL_DEFAULT_SENDER` | Actual verified sender address |
| `BREVO_WEBHOOK_TOKEN` | Separate random bearer secret for delivery callbacks |

Google login requires both `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`;
otherwise its button is hidden. Register `PUBLIC_BASE_URL/auth/google/callback`
as the OAuth callback. Weather optionally uses `WEATHER_API_KEY` and
`WEATHER_CITY`; without a key it uses the existing fallback.

The app explicitly uses Asia/Manila wall time. Ambiguous legacy timestamps are
preserved. Public page visits on another host redirect to `PUBLIC_BASE_URL`;
`MD57_ENFORCE_CANONICAL=false` disables that redirect during migration.

**Render Free storage is temporary and its process can sleep.** Neither backups
on that disk nor Vercel make it durable. For a one-day presentation, download a
verified backup in Operations before redeploying and before ending. Background
jobs cannot run while the service sleeps. For durable operation, mount persistent
storage and set `MD57_DATA_DIR` to its private directory, or undertake a separate
migration to a managed database. The directory groups SQLite, the text log and
automated backups. Individual overrides remain available: `MD57_DB_FILE`,
`MD57_ACTIVITY_LOG`, `MD57_BACKUP_DIR`, `MD57_RUNTIME_DIR`. Restore existing
records privately before first startup, and retain an off-server backup.

## Email delivery

Accepted means the provider accepted a submission; it does not establish Gmail
delivery. Operations shows separate Delivered, Bounced, Failed and Uncertain
states. Ambiguous timeouts are not automatically retried. Do not use an SMTP key
as a Brevo API key.

Configure a transactional Brevo webhook:

```text
URL: https://YOUR-CANONICAL-HOST/api/mail/brevo-events
Authorization: Bearer YOUR_BREVO_WEBHOOK_TOKEN
```

Subscribe to delivered, hard bounce, blocked, invalid and error events. Configure
bearer authentication using [Brevo's secure webhook documentation](https://developers.brevo.com/docs/secured-webhooks).
The endpoint matches provider ID and recipient and tolerates early, duplicate
and out-of-order callbacks. Without that configuration, messages remain Accepted
and delivery must be checked in Brevo logs. Sender verification, activation and
recipient rejection require changes in the provider account.

## Optional Vercel proxy

Render serves the whole app directly. If a Vercel domain is needed, set
`MD57_BACKEND_ORIGIN` in **Vercel** to the Flask HTTPS origin. Vercel runs a proxy;
it does not host SQLite or workers. Choose one canonical login origin in Flask.
See [Vercel setup](docs/VERCEL.md).

## Records and recovery

- [Operations and recovery](docs/OPERATIONS.md)
- [Private employee transfer](docs/EMPLOYEE_TRANSFER.md)
- [60-workday presentation history](docs/DEMO_HISTORY.md)
- [Audit repair status](docs/AUDIT_REPAIRS.md)

New employee exports use an independent transfer key. Set
`MD57_EMPLOYEE_TRANSFER_KEY` on both installations, or privately configure the
generated `.runtime/employee-transfer.key` on the destination. Existing files
using the original session secret remain readable when no separate transfer key
is configured. Never rotate `SECRET_KEY` merely to transfer employees.

## Development

```powershell
python -m unittest discover -s tests -v
python -m compileall -q _Backend
```

Tests use disposable databases and mocked email. The optional browser regression
uses Playwright/Chromium. Declare runtime dependencies in `pyproject.toml`, then
regenerate the fully pinned deployment file:

```powershell
uv pip compile pyproject.toml -o requirements.txt --universal
```

History uses server pagination (`page`, `limit`, maximum 100), literal `search`,
`start`/`end` dates and role/source restrictions. `format=csv` streams all matching
records independently of the displayed page. Spreadsheet exports neutralize
formula prefixes while preserving original database values.
