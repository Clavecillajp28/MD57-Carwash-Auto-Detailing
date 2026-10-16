# Operations, recovery, security and service quality

## Start and restart

Install the pinned dependencies in `requirements.txt`. From the project root:

```powershell
.\manage-server.ps1 start
.\manage-server.ps1 status
.\manage-server.ps1 restart
.\manage-server.ps1 stop
```

The managed runtime uses Waitress with a workspace instance lock, detects an occupied port before loading the app, records its PID and loaded source fingerprint, and reports startup failures in `.runtime/`. `python _Backend/runtime.py` runs the same server in the current terminal. `python _Backend/app.py` also uses the managed server; the PowerShell commands manage instances started through the script. For an older manually started server, stop its terminal before using the script. No startup command kills an unknown process to free a port.

Owner and Manager can open **Operations** to see loaded version, whether source changes require a restart, delivery counts, and (for Owners) worker run health. The page does not expose the workspace path. Background startup remains controlled by `MD57_BACKGROUND_JOBS`; use `false` for tests and one-off imports. A public deployment also needs HTTPS and correctly configured proxy/cookie settings; Waitress does not itself provide TLS. Keep the service on a local interface behind the configured HTTPS proxy.

The managed server has 32 request threads and allows at most 16 concurrent live-update streams, leaving capacity for ordinary requests. Extra tabs close their stream and use the existing polling fallback. For intentional LAN hosting set `MD57_HOST=0.0.0.0`; the default is local access at `127.0.0.1`.

When deploying behind an HTTPS reverse proxy, set `MD57_TRUSTED_PROXY` to that proxy's exact address and enable secure session cookies. The server accepts forwarded client/host/protocol headers only from that upstream; by default it strips untrusted forwarded headers. This preserves correct HTTPS Google callback URLs without trusting headers from arbitrary direct clients.

## Backups and recovery

The scheduled worker checks hourly and creates a snapshot when `MD57_BACKUP_INTERVAL_HOURS` has elapsed (24 by default). `MD57_BACKUP_KEEP` retains 14 generated snapshots by default, with a minimum of two. `MD57_BACKUP_DIR` defaults to `backups/automated`. Only module-generated backup files are pruned. Every snapshot has a SHA-256 manifest, table counts and database integrity checks; scheduled snapshots also undergo a restore drill into a separate temporary database.

Owners can create snapshots, download them, and test recovery on Operations. Managers cannot access those APIs. An enrolled Owner needs recent email-code verification before creating or downloading a backup. Backups contain private account and customer data: keep them outside the web root, restrict filesystem access, and maintain a separately secured off-device copy. Local retention alone does not protect against loss of the computer. Keep the `.env` application secret and deployment configuration secured separately: encrypted MFA and queued mail need the same secret after restoration.

Read-only recovery verification and safe restore commands:

```powershell
python _Backend/backups.py list
python _Backend/backups.py verify --name md57_auto_TIMESTAMP.db
python _Backend/backups.py restore --name md57_auto_TIMESTAMP.db --target recovered-md57.db
```

Use a filename actually returned by `list`. Restore refuses an existing destination or the live database path. Before replacing the live database: stop MD57; run verification and restore to a new file; preserve the current database and any SQLite sidecar files as an incident snapshot; configure `MD57_DB_FILE` to the verified restored file; then start MD57 and check account access and history counts. Reverting the configuration points back to the preserved original. Test this procedure on a separate installation before an incident. There is no browser button that overwrites the live database.

## Analytics and prediction validation

Analytics and exports include all stored history by default as **System records**, with no record-type selector or presentation banner. Internal origin flags and explicit maintenance API filters remain intact; generated history is excluded from real-data training and does not establish field accuracy. Queue wait measures arrival to service start; service duration measures start to finish; pickup delay measures finish to physical collection, including overnight delays. Missing timestamps produce no measurement rather than an invented zero.

The Staff directory and its CSV also include all stored finished services, attributed by account ID. Done Today uses the completion date; In Progress uses live tasks only. Cancelled services are excluded. Staff display criteria include retained history, while live assignment and real-data model validation retain their separate operational data rules. Failed directory loads show an unavailable or stale-data message and disable export until a successful refresh.

Saved walk-in estimates predict arrival-to-finish turnaround, not just time in the waiting queue. The report compares the saved estimate with the later recorded finish and states its reporting window and sample count. A separate latest chronological validation uses real walk-ins only: the newest 20% form the test split, and every training job must finish before the first test arrival. It requires at least 20 training and five test jobs, plus 30 total eligible rows. The validation model is separate from the live predictor; afterward the live model can retrain on all eligible records. Validation reports training/test dates, counts, MAE, RMSE and bias. Small datasets explicitly show that no validation result is available. The latest validation window is labelled independently of the selected report period.

Live timing displays on Booking, its confirmation ticket, the Queue and My Bookings separate **wait before service**, **service duration**, and **estimated finish**. An empty queue can have zero queue delay and a positive service duration. Matching finished service/vehicle-size history (including retained generated records) supplies the median of up to 100 recent valid durations when at least five exist. Otherwise the service component uses a zero-queue AI estimate plus the vehicle-size adjustment, falling back to the catalogue duration if the model fails or returns an invalid value. This historical baseline is an operational estimate, not new model validation.

The read-only resource calculation considers opening/closing hours, booked-slot priority, each bay's remaining work and clean-up gap, and washer availability across bays. Future dates use active rostered staff who are available that day and are labelled provisional; an immediate service needs actual clock-in confirmation. Unknown staffing or pending completion confirmation leaves start/finish unknown while still showing the service duration. Scheduled lead time before the booked appointment is excluded from queue wait. Live updates and the existing one-minute refresh recalculate the display. These computed `estimate` fields are separate from the original saved `predicted_wait_minutes`; viewing or refreshing a page never overwrites prediction evidence or generates records.

## Durable email delivery

Branded email uses an encrypted database outbox, stable message IDs, deduplication keys, atomic delivery claims and bounded exponential retries (five attempts). Ready emails use the booking ID and completion timestamp as their key. A retry does not notify a vehicle already collected. Confirmed sends clear their encrypted payload; expired messages are scrubbed. Operational messages expire after 24 hours. A worker runs every minute; it does not bypass an explicit disable of background jobs.

Verification codes and employee invitation/recovery links are attempted immediately and are **not automatically retried** after a failed send, since the calling flow may invalidate them. A fresh request issues fresh access credentials. Manager views show operational email status, timestamps and attempt counts without message bodies, codes, tokens or detailed provider responses. Security-code/invitation metadata and detailed worker diagnostics remain Owner-only.

SMTP cannot guarantee exactly-once delivery: a process might crash after the provider accepts a message but before its database acknowledgement. Expired delivery claims become **Uncertain**, rather than automatically resending. A manager must acknowledge the possible duplicate before queuing a retry. A sent status means SMTP acceptance, not proof that a person read the email or that their provider placed it in the inbox.

## Owner Gmail verification

Open **My Profile ? Manage email verification and recovery codes**. Enter the current password (or sign in with Google within five minutes), confirm the Owner Gmail address, choose **Send code**, then enter the received six-digit code and choose **Enable email verification**. Enrollment completes only after mailbox possession is confirmed. Configuring a contact address alone does not enable verification or count as verified ownership.

Codes expire after five minutes, are purpose-bound and single-use, allow five incorrect attempts, and require a one-minute resend interval. An account can request at most ten codes per hour. Issuing a new code invalidates older codes for that purpose. Challenges are stored as keyed hashes, tied to the account/session version and requesting browser challenge, and become unusable after access changes. Security emails are delivered immediately through the encrypted outbox with automatic retries disabled. Failed delivery invalidates that challenge and does not activate protection.

After activation, password or Google sign-in establishes only a pending challenge; the Owner receives a Gmail code and cannot use authenticated routes until verification succeeds. A new code can be requested on the verification page after the cooldown. Recovery codes remain available if mail delivery or mailbox access is unavailable. Enabling, disabling, or replacing recovery codes revokes every session. Ten high-entropy recovery codes are shown once, stored as keyed hashes, and can each be consumed once. Pending first-factor login challenges expire after five minutes; resending does not extend that deadline.

Sensitive Owner controls require verification within the previous five minutes. In Account Security, choose the intended action, send its code, then confirm it. A code for verifying actions cannot disable protection or replace recovery codes. Existing recovery codes can also approve these actions. Gmail destinations are read from the enrolled account, never from login request fields. To change an enrolled security Gmail, disable email verification with existing proof, enter the new Gmail in Account Security, and confirm its code before enabling it again. The security destination is stored separately from login and password-recovery contacts; using a Gmail already attached to the same person's customer account does not merge accounts, change identities, or transfer history.

Already enabled legacy authenticators continue protecting their accounts until the Owner proves the existing authenticator/recovery factor and confirms the new Gmail code. Switching clears the old authenticator secret and revokes sessions. No legacy protection is silently removed. Losing both mailbox access and all recovery codes requires controlled offline recovery; password recovery alone does not disable verification.

Email verification is the selected access-control method for this application. It depends on the Gmail account's security and delivery availability and should not be described as phishing-resistant MFA or NIST-compliant out-of-band authentication. No biometric or passkey feature is implied.

## Queue completion and pickup

The workflow is Waiting ? In progress ? Pending confirmation ? Ready ? Completed.
Starting requires a free, clocked-in lead washer; the system assigns that washer.
In-progress and pending-confirmation work both reserve the assigned washer.
Management can mark an in-progress job Ready directly after the required checks.
All completion paths require a recorded start and, for newly started jobs, the
quality checklist. Historical rollout exceptions remain intact.

Notifying a customer keeps the vehicle on Ready. Owner/Manager confirms physical
pickup separately, recording the authenticated account ID/name and collection
time. Single and bulk notification actions never confirm pickup. API consumers
should use reservation IDs because a vehicle can have multiple visits. Historical
records without a pickup timestamp stay unmeasured; no collection time is inferred.

Database initialization applies additive migrations and fills missing catalogue
entries without resetting saved prices, durations, bays or active states. Migration
and legacy account-retirement code remain necessary for existing databases.
Analytics tracks one lead washer per job; staff-per-car is a planning assumption,
not an observed crew roster. Real-only operational allocation/training rules and
internal history-origin flags remain separate from all-history analytics reports.

## Structured audit and quality checklist

Operations & Recovery provides paginated audit search by person/action/reason, record type, record ID and date range. Database triggers capture inserts, updates and deletes for account identity/access, reservation state, services, settings and checklists. Account IDs/names, before/after values and supplied reasons are stored without passwords, OTPs, invitation tokens or MFA secrets. SQL triggers reject updates/deletes of audit records through ordinary application connections. This does not protect against a database administrator editing the database offline; the existing text log remains available, and old text entries are not fabricated into structured history. External write tools must use `database.get_db_connection()` so trigger attribution functions are registered.

Newly started services require a quality checklist. The assigned washer or management checks the requested work, vehicle concerns and final finish, then saves before marking the job done. Staff cannot access another washer's checklist or resolve a concern. Managers/Owners review concerns before completion approval. Edits carry an optimistic revision to prevent overwriting another person's changes. Finished checklists are read-only and retain the contributor's name/account ID and manager review. Existing historical/active records without the rollout flag keep their prior workflow; no past checklist is invented. On the queue detail dialog use **Service quality checklist**; incomplete completion attempts also link to the form.

## Deployment verification

After starting this copy, verify login, the Staff directory, booking timing, Analytics and PDF export, email delivery, and Operations worker health. See the root README for startup and host configuration. Use a separate private backup before changing deployment settings.
