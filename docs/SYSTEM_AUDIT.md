# System audit — 17 October 2026

This records the pre-repair assessment. See [repair status](AUDIT_REPAIRS.md)
for the subsequently implemented changes and remaining hosting configuration.

Reviewed revision: `933a920`. This is an assessment, not an applied cleanup.

The system is functional, but the current source contains **four high-priority
risks**, several reproducible data-quality defects, and deployment/maintenance
debt. Passing the existing tests does not establish that these paths are safe.
Some findings concern code added during the recent deployment work and should
be corrected alongside older code.

**Scope and evidence**

- Inventoried 50 backend Python modules (11,185 lines), 25 HTML templates,
  nine shared JavaScript files, database initialization/migrations, four test
  modules, dependencies, runtime, and deployment documentation/configuration.
- All 50 backend modules parsed, all nine shared JavaScript files passed
  `node --check`, and all 18 existing tests passed.
- Ran 59 GET routes for each of Owner, Staff, and Customer: **177 responses,
  no HTTP 500s**. Expected access denials, redirects and missing-parameter
  responses were counted separately. This is a route smoke check, not a proof
  of complete authorization correctness.
- Used disposable databases for malformed-input, booking, attendance,
  initialization and concurrent-request probes. CSRF was disabled only in
  these local probes to isolate business logic. No live database was modified.
- Confirmed the escaping issue in an isolated browser using a harmless marker.
  No attack was run against the hosted service and no real email was sent.
- Provider delivery logs, current Render/Vercel environment values, external
  account permissions, recovery from actual infrastructure loss, dependency
  CVEs and sustained concurrent load were not independently audited.

**Findings, in repair order**

1. **High — stored JavaScript execution through inline handlers.**
   Sources: `_Frontend/static/js/common.js:211`,
   `_Frontend/templates/manage_pricing.html:242`, and other uses of
   `escapeJsString` inside generated `onclick` attributes.
   The helper escapes literal quotes/backslashes but does not escape `&`.
   An HTML character reference survives the helper and becomes a quote when
   the browser decodes the attribute. A user-controlled vehicle model can
   therefore break out of the intended string when management clicks an
   affected button. The model field accepts enough characters for the local
   proof. **Verified:** the isolated browser marker changed from 0 to 1.
   Fix: construct buttons with DOM APIs and `addEventListener`; pass values in
   closures, not inline JavaScript. Review all callers, not just one page.

2. **High — password-reset codes are not consumed atomically.**
   Source: `_Backend/blueprints/auth.py:385`.
   The route reads an unused code, checks it, hashes the new password and only
   then updates the user/marks codes used. There is no write transaction around
   the read/check/consume sequence and no conditional consumption check.
   **Verified:** two simultaneous requests using one code both returned 302
   success; both were allowed to reset the account. The probe synchronized
   threads after they had read the same valid code.
   Fix: atomically claim/consume the unexpired code in a transaction, check the
   affected-row count, and ensure attempts and account state are rechecked.
   Add a regression test with simultaneous requests.

3. **High — concurrent reschedules can double-book a bay.**
   Source: `_Backend/blueprints/queue_api.py:530`.
   New booking creation uses `BEGIN IMMEDIATE`; rescheduling checks availability
   before acquiring an equivalent write lock. Two changes can both observe a
   free slot before either writes.
   **Verified:** both concurrent reschedules returned 200 and left two waiting
   bookings in the same bay at 12:00 PM. The probe used the real slot checker,
   synchronizing after each availability check. Staffing was held available
   to isolate this race.
   Fix: validate ownership, current state, staffing and availability inside one
   write transaction; check the update count before returning success.

4. **High — the chosen free-hosting arrangement does not preserve shop data.**
   Sources: `_Backend/config.py:26`, `_Backend/backups.py:14`, and the current
   Render Free + local SQLite deployment design.
   Records, imported employees, audit text logs, outbox state and on-server
   backups live on the service filesystem. Render Free storage is ephemeral;
   a restart/redeploy/spin-down can remove them. Thread-based jobs also cannot
   run while the service sleeps. Regenerating presentation history does not
   restore employee imports or actions performed during the presentation.
   Fix: for the temporary defense, retain private off-server backups and a
   rehearsed recovery plan. Before real operation, choose durable storage and
   a reliable execution environment for scheduled jobs. See
   [Render's documented limits](https://render.com/docs/free).

5. **Medium — fictional testimonials are inserted into the live dataset.**
   Sources: `_Backend/database.py:911`,
   `_Backend/blueprints/feedback_api.py:94`, `_Backend/analytics.py:329`.
   Four sample reviews are inserted whenever `feedbacks` is empty, even with
   `MD57_DEMO_HISTORY=false` and legacy seeding disabled. They have no linked
   reservation; the live filter treats a missing reservation's provenance as
   zero, so they appear as live reviews and can affect rating KPIs.
   **Verified:** a fresh non-demo database contained four unlinked reviews.
   Fix: remove sample insertion from normal initialization. Seed only through
   the opt-in generator and give every generated review explicit provenance
   or a source-linked booking. Review existing rows before removing them.

6. **Medium — “sent” means provider acceptance, not inbox delivery.**
   Sources: `_Backend/email_transport.py:33`, `_Backend/mail_outbox.py:74`,
   `_Backend/employee_accounts.py:105`.
   Brevo HTTP 201 causes the outbox to store `sent` and the UI to report an
   emailed invitation. No delivery-event endpoint or provider-message-ID
   mapping updates that status for delivery, bounce or rejection. This explains
   the reported success message followed by Brevo Error events.
   Fix: store provider IDs, distinguish accepted/delivered/bounced/failed, and
   process authenticated delivery events. Until then, say “accepted by email
   provider” and link operators to provider logs rather than implying arrival.

7. **Medium — email failure details are discarded before operators can use them.**
   Sources: `_Backend/email_transport.py:35`, `_Backend/mail_outbox.py:65`.
   The transport distinguishes HTTP rejection from connectivity failure, but
   the outbox keeps only the exception class, commonly `RuntimeError`.
   Credential errors, quota limits, sender verification failures and timeouts
   become indistinguishable in Operations.
   Fix: persist a safe structured error code and HTTP status. Do not expose API
   keys, verification codes, invitation links or raw provider response bodies.

8. **Medium — ambiguous network failures can lead to duplicate email.**
   Sources: `_Backend/email_transport.py:26`, `_Backend/mail_outbox.py:69`.
   The HTTP transport makes one request, but every exception becomes a normal
   failed outbox attempt. A timeout after the provider accepted a message can
   be retried for operational messages. The existing `uncertain` handling only
   covers expired sending leases after a process interruption.
   Fix: distinguish definite rejection from unknown delivery outcome; use
   provider-supported idempotency or reconciliation before retrying ambiguity.
   This is a code-path risk; duplicate inbox delivery was not tested live.

9. **Medium — attendance accepts invalid clock values.**
   Source: `_Backend/blueprints/staff_api.py:189`.
   Date and status are checked, but `time_in`/`time_out` are not consistently
   parsed or checked for ordering. These values influence punctuality,
   availability and staff rankings.
   **Verified:** `time_in="not-a-time"` was accepted with HTTP 200 for a valid
   past date and staff account.
   Fix: validate clock syntax, permitted placeholder/status combinations,
   time-out after time-in, and explicit policies for historical corrections.

10. **Medium — malformed request shapes produce server errors.**
    Sources: `_Backend/blueprints/admin_api.py:32`,
    `_Backend/blueprints/auth.py:483`, and other routes using unchecked `.get`.
    A nonempty JSON array reaches code expecting a mapping.
    **Verified:** array payloads to invitations and registration returned 500.
    Fix: a shared mapping/schema validator, per-field type/length checks and
    controlled 400 responses. Review numeric, null and oversized values too.

11. **Medium — removed default vehicle models reappear on restart.**
    Sources: `_Backend/database.py:908`,
    `_Backend/blueprints/admin_api.py:650`.
    The Owner can remove a model, but initialization unconditionally inserts
    all default models again using `INSERT OR IGNORE`.
    **Verified:** deletion returned 200, the row count became zero, and calling
    initialization restored it to one.
    Fix: seed defaults once through a versioned migration or retain an explicit
    inactive flag that startup respects.

12. **Medium — booking creation has no retry idempotency.**
    Source: `_Backend/blueprints/queue_api.py:1060`.
    A transaction protects queue-number allocation, but does not recognize a
    repeated request after an uncertain client/network outcome.
    **Verified:** submitting the same walk-in payload twice returned 200 twice
    and created distinct bookings. Multiple services for one car can be valid,
    so a blanket plate uniqueness constraint is not the right solution.
    Fix: accept a client-generated request ID and return the already-created
    booking when that same request is retried.

13. **Medium — weather lookup can hold the global SQLite write lock.**
    Source: `_Backend/blueprints/queue_api.py:1285`.
    `get_weather_reading()` is called inside the booking's `BEGIN IMMEDIATE`.
    When the cache is cold/expired it performs an external request with a
    three-second timeout. Other writers can wait behind that network call.
    Fix: resolve/cache external input before the write transaction and keep
    transactional checks local. Preserve the captured weather timestamp.

14. **Medium — reports and history repeatedly load lifetime records.**
    Sources: `_Backend/analytics.py:211`,
    `_Backend/blueprints/queue_api.py:341`, `_Backend/vehicles.py:42`.
    Analytics loads live/archive data then filters periods in Python. History
    returns all matching rows and computes visits with another table scan;
    five-row pagination happens in the browser. Small presentation datasets
    hide the cost.
    Fix: server-side date/source pagination and indexed aggregates, preserving
    the definitions used for returning vehicles and cross-period comparisons.
    No sustained-load performance limit was measured in this audit.

15. **Medium — the documented direct app startup initializes two app objects.**
    Sources: `_Backend/app.py:320`, `_Backend/runtime.py:60`.
    Running `python _Backend/app.py` builds the application as `__main__`, then
    calls the runtime, which imports `app` under a different module name.
    Initialization and app-specific worker registration can therefore run
    twice. `python _Backend/runtime.py` does not take this duplicate path.
    Fix: keep one construction path or pass the already-created application
    into the runtime. Verify a single worker registry during local startup.

16. **Medium — optional Google login is a mandatory startup dependency.**
    Sources: `_Backend/app.py:72`, `_Backend/extensions.py:21`.
    The entire app raises if Google credentials are missing, even when a user
    wants only employee/password access.
    Fix: enable OAuth only when configured, hide its button when disabled, and
    return a controlled unavailable response if someone opens its route.

17. **Medium — shop time depends on environment configuration.**
    Sources: `_Backend/utils.py` (`now_str` and business hours),
    `_Backend/blueprints/queue_api.py`, `_Backend/attendance_close.py`,
    `_Backend/demo_history.py`.
    Most operational code uses naive `datetime.now()` in the host timezone;
    demo generation explicitly uses Asia/Manila. If `TZ` is omitted or the
    process timezone differs, booking validation, clock-in, no-show and
    historical timestamps can disagree with the generated data and the shop.
    Fix: one explicit shop-clock abstraction and a documented storage/display
    convention; test it independently of host timezone. The hosted `TZ` value
    was not inspected.

18. **Medium — spreadsheet exports do not neutralize formulas.**
    Source: `_Frontend/static/js/common.js:717` and server CSV exports.
    CSV quoting handles separators but preserves values beginning with formula
    prefixes. User-entered names/models can therefore be interpreted as
    formulas by spreadsheet applications when an operator opens an export.
    Fix: one documented spreadsheet-safe export policy applied on both client
    and server, with tests for formula prefixes and ordinary numeric fields.
    No exported file was opened in an external spreadsheet during the audit.

19. **Low — an obsolete demo context processor remains.**
    Source: `_Backend/app.py:281`.
    `demo_history_notice()` injects `demo_history`, but none of the 25 current
    templates references that variable. It is left over from the removed
    site-wide notice. Remove that context processor after retaining any startup
    summary actually needed by the generator; do not delete the generator.

20. **Low — dependency declarations are duplicated without a lock workflow.**
    Sources: `requirements.txt`, `pyproject.toml`.
    Both hand-maintain the same direct pins. Editing one can produce different
    local and hosted environments, while transitive dependencies remain
    unconstrained. Choose one authoritative manifest and generate the other
    deployment artifact/lock consistently. No CVE scan was performed.

21. **Low — setup documentation mixes an old deployment-copy workflow with Git hosting.**
    Sources: `README.md`, `docs/VERCEL.md`, `docs/OPERATIONS.md`,
    `pyproject.toml`, `_Backend/app.py` startup messages/comments.
    README describes private records/configuration as supplied package contents
    and says tests are excluded, whereas Git deliberately excludes the database
    and `.env`, and includes tests. Some messages still direct users to Vercel
    secrets although Flask is hosted on Render. The pyproject Vercel entrypoint
    suggests a Flask Function while vercel.json deploys a proxy.
    Fix: document separate local, Render and optional Vercel paths, with one
    exact environment-variable checklist per path.

22. **Architecture — service timing has several partly overlapping authorities.**
    Sources: `_Backend/scheduling.py`, `_Backend/service_estimates.py`,
    `_Backend/wait_estimate.py`, `_Backend/slot_hold.py`,
    `_Backend/late_notice.py`, `_Backend/ai_engine.py`.
    Booking admission, live display, work-ahead snapshots, protected-slot holds
    and late notifications use related but different timing rules. For example,
    live estimates use recorded durations/AI while admission and holds use
    catalogue standards. The model target is arrival-to-finish, which must not
    be confused with queue waiting alone.
    Fix: define shared timing/state contracts and test agreement before merging
    implementations. These modules are used; deleting them as “duplicate” would
    break behavior. Differences need review, not automatic removal.

23. **Architecture — Vercel is optional overhead for the current Render app.**
    Sources: `vercel.json`, `scripts/build-vercel.mjs`, `docs/VERCEL.md`.
    Flask already serves pages, assets and APIs on Render; Vercel currently adds
    a reverse-proxy hop. It does not solve persistent storage or worker uptime.
    Two usable public origins also complicate cookies, canonical links and OAuth
    callbacks, especially when PUBLIC_BASE_URL points at one while users open
    the other. Keep the layer only for a specific domain/routing requirement;
    otherwise standardize on one public origin. This is a simplification option,
    not a finding that the proxy code is unused or broken.

24. **Architecture — employee transfer unnecessarily couples session secrets.**
    Sources: `_Backend/employee_transfer.py:16`, `docs/EMPLOYEE_TRANSFER.md`.
    The import file encryption derives from the application's SECRET_KEY, so
    the current workflow requires local and hosted installations to share
    that key. This makes independent environment key management harder.
    Fix: a dedicated transfer secret/key with an explicit lifecycle. Do not
    casually rotate an existing SECRET_KEY: encrypted MFA and outbox payloads
    depend on it. Existing encrypted transfer files contain credential hashes
    and must remain private even though they are not plaintext passwords.

25. **Coverage gap — tests focus on recent additions, not the core lifecycle.**
    Sources: the four modules under `tests/`.
    The 18 tests cover demo generation, employee transfer, mail transport and
    source separation. They do not currently cover the confirmed escaping,
    password-reset race, reschedule race, full booking-to-pickup flow, clock-in
    approval races, recovery/restore incidents, OAuth lifecycle, or provider
    delivery events. Add tests for the confirmed defects first, then a small
    role/state transition suite. Avoid replacing useful behavior tests with
    tests that merely repeat the implementation.

**What should and should not be removed**

- Safe cleanup candidate identified: the unused `demo_history` template
  context processor, outdated setup wording, excess comments/blank lines after
  obsolete branches are removed, and the redundant dependency-maintenance path.
- Conditional simplification: Vercel's proxy configuration if the project will
  use Render directly; Google OAuth if the product intentionally drops that
  sign-in option. Make that product decision before removing code.
- Keep: role/session checks, CSRF, source provenance, checklist enforcement,
  backup validation, legacy IDs referenced by history, and disabled demo staff
  referenced by presentation records. SMTP and HTTPS email support serve
  different local/hosted environments; neither is automatically dead code.
- All nine shared JavaScript files are referenced by templates. The inspected
  imports and public helper references did not establish another wholly dead
  backend module. Large files are maintenance risks, not proof that they are
  trash. Pillow, ReportLab, NumPy, pandas and scikit-learn support existing
  PDF/model functionality; remove them only together with the corresponding
  intentionally retired feature.
- `.runtime/` contains ignored operational state, private transfer exports and
  review artifacts. Do not upload it or blanket-delete a running server's
  runtime directory. Private database files and backups are records, not trash.

**Suggested implementation sequence**

1. Add regression tests and fix findings 1–3. Keep the fixes small and reviewable.
2. Remove live-mode sample testimonials; validate input shapes and attendance;
   preserve model removals across initialization; add booking request IDs.
3. Correct email status/error reporting and ambiguous-delivery handling.
4. Consolidate startup, clock handling and optional integration configuration.
   Select a durable hosting/storage plan before using the system operationally.
5. Add query pagination, then reconcile timing rules with focused scenarios.
6. Remove the proven unused context processor and update dependencies/docs.
   Do not start with mass deletion or a rewrite of all UI/backend modules.

Audit-only probe scripts and evidence are in ignored `.runtime/audit_checks.py`,
`.runtime/audit_races.py`, `.runtime/system_audit_evidence.json`, and
`.runtime/system_audit_races.json`. They operate on disposable databases and
must not be repurposed to probe public accounts without a separate review.
No application source was changed, no deployment was triggered, and no fixes
have been claimed as completed by this report.
