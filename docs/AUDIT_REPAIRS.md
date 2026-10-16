# Audit repair status

Repairs for the [17 October audit](SYSTEM_AUDIT.md). Existing employee accounts,
private transfer files and operational history are preserved. No production
database was opened or edited during verification. Tests use temporary databases
and mocked delivery; no real email was sent.

| Finding | Result |
| --- | --- |
| 1. Dynamic handler injection | Removed `escapeJsString` and all its callers. Whitelisted actions consume encoded JSON data, with click/keyboard browser regression. |
| 2. Reset race | Write lock protects code validation/consumption and password update; expensive hashing happens before locking. Existing credential trigger revokes sessions. |
| 3. Reschedule race | Availability check and update run under one write lock. Restore/cancel/check-in/pickup transitions also serialize conflicting changes. |
| 4. Ephemeral hosting | `MD57_DATA_DIR` groups private database/log/backups; documentation explains persistent mounts and off-server backup. Actual Render persistence and sleeping remain infrastructure constraints. |
| 5. Unrequested fixture reviews | Fresh databases no longer insert them. Exact legacy unlinked fixtures are marked synthetic and excluded from live reviews/analytics; records are retained. |
| 6. Acceptance vs delivery | Outbox stores Accepted separately from Delivered/Bounced, retains provider ID, and accepts authenticated delivery callbacks. Old Sent rows migrate to Accepted. |
| 7. Hidden provider errors | Safe HTTP/configuration/connection error codes are retained without private response bodies. |
| 8. Ambiguous retries | Unknown responses become Uncertain and are not automatically retried. Eligible operational retries require duplicate-risk acknowledgement. |
| 9. Attendance inputs | Clock syntax/order and status/time combinations are checked. Atomic attendance insertion prevents duplicate clock-ins and approvals; time-out requires matching active attendance. |
| 10. JSON failures | Shared object/scalar/field-size validation rejects malformed structures with HTTP 400; requests are bounded to 2 MB. Route business rules remain authoritative. |
| 11. Deleted models return | Default vehicle models seed only when their table is first created. Existing catalogues are not replenished on restart. |
| 12. Duplicate retries | Actor-scoped idempotency keys save the booking and replay response in the same transaction. Browser retries retain the key after lost/5xx responses and refresh. Changed requests cannot reuse a committed key. |
| 13. Weather under write lock | Weather fetch occurs before the booking transaction. |
| 14. Lifetime scans | History paginates/filters in SQL, streams filtered CSV, and loads visits only for displayed plates. Analytics bounds full booking rows to current/comparison periods; first-record metadata is aggregated in SQL, prior visits are retained for return intervals, and lookup batches are bounded. Lifetime facts still require historical database reads; they are not fabricated from the displayed page. |
| 15. Double startup | Direct app execution passes the existing application into the runtime. |
| 16. Mandatory OAuth | Google registration/button require both keys. Disabled routes return controlled 503 responses. |
| 17. Host time | Shared Manila clock replaces host-local operational clocks. SQL audit triggers use the same clock. Existing timestamps are preserved. |
| 18. Formula exports | JS and Python CSV exports prefix formula-like string cells, while numeric values retain their type. |
| 19. Dead context processor | Removed unused demo template injection; presentation provenance remains intact. |
| 20. Dependency drift | `pyproject.toml` is authoritative; `requirements.txt` is generated with fully pinned transitive dependencies. Removed misleading Vercel Python entrypoint. |
| 21. Stale setup | README, operations, employee-transfer instructions and environment example now distinguish local, Flask host and optional Vercel configuration. |
| 22. Timing boundaries | Shared hours/gap/overdue policy; pending confirmation blocks same-day admission, and protected bookings include cleanup time. Live predictions, catalogue slot reservation and model input features retain their documented distinct purposes. Removed superseded analytics visit loader. |
| 23. Multiple origins | Validated canonical origin, page redirects and documented direct Render hosting. Optional Vercel proxy remains available. |
| 24. Transfer/session coupling | New exports use a separate configured/generated transfer key. Explicit legacy exports and existing imports remain compatible; session secrets are not rotated. |
| 25. Test gaps | Added concurrent reset/reschedule/clock approval, idempotency, JSON, attendance, CSRF, optional OAuth, checklist-to-pickup, archive/source separation, paginated export, backup recovery, provider status/replay/early-callback and Chromium action regressions. |

Additional corrections: only the assigned washer can complete a legacy job;
feedback submissions serialize the one-review check; vehicle visits include
archives without duplicating live IDs; CSRF headers are attached only to
same-origin requests; pending booking recovery data is cleared on logout.

## Deployment actions outside the repository

1. Download and verify an off-server backup before a Render redeployment: free
   instances can lose their current SQLite records during restart/redeployment.
2. Deploy these source changes. Additive migrations run at startup. Legacy
   reviews are classified and old mail statuses are renamed; employee credentials
   and real reservation attribution are not replaced.
3. Set one `PUBLIC_BASE_URL` and use it for browser login and OAuth callbacks.
4. Set a private `BREVO_WEBHOOK_TOKEN` on Flask and configure Brevo's transactional
   webhook with matching bearer authentication. See [setup](../README.md).
   Without provider setup, Accepted remains the honest available status.
5. For durable operation, provision actual persistent storage. A folder variable
   alone cannot make Render Free durable or prevent it from sleeping. For the
   temporary presentation, keep downloaded backups and the private employee
   transfer file outside the server.

## Verification scope

Local verification: **35 tests passed**, including Chromium; **177 GET requests**
across three roles returned no server errors. **63 rendered inline scripts**
passed Node syntax checks. Mobile browser checks covered employee management,
pricing, Operations, History pagination and transaction details without script
errors. Python compilation and shared JavaScript syntax checks also passed.

Run `python -m unittest discover -s tests -v`, compile Python modules, check shared
JavaScript with Node, and smoke role-restricted routes on a disposable app.
The browser regression requires Playwright and Chromium; it is skipped when the
Python package is unavailable. Provider account settings, actual Gmail delivery,
external OAuth consent, production recovery and sustained load require a hosted
environment and are not established by local tests. No CVE attestation is made.
