# Deploy with Vercel

This deployment uses Vercel as an HTTPS reverse proxy. Flask still renders the
pages and serves the API and static assets on a persistent backend server.
The browser uses one Vercel domain for sessions, forms and API requests.

The application cannot run unchanged entirely inside Vercel Functions: it writes
SQLite files, registers SQLite audit functions, keeps live events in memory and
runs continuous background threads. A temporary `/tmp` database would lose shop
records and would not be shared between instances. A Vercel-only deployment
requires a separate database, worker and event architecture migration.

## 1. Start the persistent backend

Use the existing Windows or Linux startup instructions in README.md. Give that
server persistent private storage and a public HTTPS address reachable by Vercel.
Use one application process for the in-memory live event feed. Keep its existing
database and private SECRET_KEY together so encrypted records remain readable.
Keep the backend running; Vercel does not start it or run its workers.

Set these on the backend (example public Vercel domain):

```dotenv
PUBLIC_BASE_URL=https://your-project.vercel.app
SESSION_COOKIE_SECURE=true
MD57_BACKGROUND_JOBS=true
TZ=Asia/Manila
```

Retain the existing Google, mail, encryption and other private configuration.
Set MD57_DB_FILE, MD57_ACTIVITY_LOG and MD57_BACKUP_DIR to persistent private
paths if necessary. Configure MD57_TRUSTED_PROXY for the backend host's actual
reverse proxy, as described in README.md. Ensure its forwarded scheme is HTTPS.

## 2. Configure Vercel

1. Import this repository with the project root set to this folder.
2. Use the **Other** framework preset. The committed vercel.json specifies the
   install command, build command and output directory; remove conflicting
   dashboard overrides. Python and its requirements are not installed on Vercel.
3. Add **MD57_BACKEND_ORIGIN** in Vercel environment variables, for example
   `https://your-backend.example.com`. Use only an HTTPS origin with no path.
   It must point to the persistent Flask server, never back to the Vercel domain.
4. Deploy. Set the variable separately for each enabled environment. Redeploy
   after changing it because it is consumed at build time.

Only the proxy routing output is deployed. .vercelignore excludes the private
database, environment files, audit logs, backups and backend source from upload.
App secrets belong on the backend; the backend origin is the only required
Vercel build variable. The existing local pyproject.toml entrypoint is excluded
from Vercel upload; the explicit framework preset disables Flask autodetection.

To inspect the generated routing output locally (Node.js 22 or newer):

```powershell
$env:MD57_BACKEND_ORIGIN = 'https://your-backend.example.com'
node scripts/build-vercel.mjs
Get-Content .vercel/output/config.json
```

The build fails if the origin is missing or invalid. It never copies application
files or private records into the deployment output. Every path and HTTP method
is forwarded to Flask, including /static, login, API writes and /api/live.
No authenticated response should be cached at the CDN.

## 3. Configure Google and verify

Register `https://your-project.vercel.app/auth/google/callback` as an authorized
Google OAuth redirect URI. For a custom domain, use that domain consistently in
PUBLIC_BASE_URL and Google settings. Preview deployments should use a separate
backend/database and registered OAuth domain to avoid changing production data.

After deployment, verify:

- Login, logout, registration verification and Google sign-in.
- Static images and styles, protected pages and CSRF-protected form/API writes.
- A reservation survives backend restart and Vercel redeployment.
- Two open pages receive live changes; verify fallback polling if the proxy
  closes a long-lived stream. Check worker health, queued mail and backups on
  the backend; these operations are independent of Vercel request execution.
- PDF downloads, password recovery links and email links use the public domain.

This repository preparation does not provision a backend, upload shop records,
or create a Vercel project. The public backend origin is required to go live.

References: [Vercel external rewrites](https://vercel.com/docs/routing/rewrites)
and [Build Output configuration](https://vercel.com/docs/build-output-api/configuration).
