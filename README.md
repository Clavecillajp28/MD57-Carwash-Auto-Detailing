# MD57 deployment copy

This folder contains the application, runtime dependencies, a consistent snapshot
of the existing database, the current private configuration, the audit text log,
and the operations/recovery guide. Python 3.14 was used for validation.

## Start on Windows

Open PowerShell in this folder and run:

```powershell
python -m pip install -r requirements.txt
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\manage-server.ps1 start
```

Open http://127.0.0.1:5000. The same script accepts `status`, `restart` and `stop`.
If the original workspace still occupies port 5000, stop it using its own
`manage-server.ps1 stop` before starting this deployment copy. Keep one active
installation as the source of the shop's records so independent copies do not
diverge. The original workspace has been preserved.

## Start on a Linux server

Create a Python 3.14 virtual environment and install the same requirements:

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python _Backend/runtime.py
```

Use your host's service manager for restart-on-failure. The managed Waitress
runtime serves on 127.0.0.1:5000 by default; configure the HTTPS reverse proxy
and `MD57_TRUSTED_PROXY` for the actual host. Oracle or a Cloudflare tunnel can
be configured around this runtime after selecting the public address.

## Configuration and records

Keep the existing `.env`: it contains the keys needed for encrypted account
security and queued email. `.env.example` is a reference template, not a
replacement for that private file. Existing accounts retain their passwords,
roles and history. Starting the application uses the supplied database.

For public hosting, set the canonical `PUBLIC_BASE_URL`, secure cookies after
HTTPS is configured, and the Google OAuth redirect address:
`https://YOUR-HOST/auth/google/callback`. Match the server timezone to
Asia/Manila. Verify Gmail delivery and background workers on that host.

Store SQLite, its sidecar files, the audit log, and backups on persistent private
storage. The default locations are in this folder; optional `MD57_DB_FILE`,
`MD57_ACTIVITY_LOG`, `MD57_BACKUP_DIR` and `MD57_RUNTIME_DIR` override them.
Maintain a separate off-device backup of the database and private configuration.
Transfer `.env`, the database and audit log privately; the included `.gitignore`
excludes them from version control.

## Operations and recovery

See [docs/OPERATIONS.md](docs/OPERATIONS.md) for backup creation, recovery drills,
Owner security, worker health, and service workflows. At first startup, the
application creates its own `.runtime/` state and automatic backup directory.

## Package contents

- `_Backend/`: application modules, routes, migrations and recovery tools.
- `_Frontend/`: templates, CSS, JavaScript and the logo.
- `requirements.txt`: runtime dependencies.
- `manage-server.ps1`: Windows startup and process management.
- `.env` and `.env.example`: private configuration and a placeholder reference.
- `md57_carwash.db`: consistent snapshot of the existing system records.
- `system_activity.log`: existing text audit history.
- `docs/OPERATIONS.md`: deployment operations and recovery guide.

Tests, development dependencies, Git/editor metadata, cached files, old backups,
paper notes, credential exports and one-off history generators are excluded.
