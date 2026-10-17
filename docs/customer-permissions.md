# Customer permission review

Reviewed 17 October 2026 against the local application.

| Area | Customer permission |
|---|---|
| Booking | Create a scheduled booking owned by the signed-in account; server chooses price, service bay, source and initial waiting state |
| Walk-ins | Shop personnel only; customers must provide a date and time |
| Queue | Read status/estimates; other customers have generic names, masked plates and no email, vehicle model or washer identity |
| Arrival and service | No check-in, start, completion, checklist or completion approval actions |
| Cancellation | Own waiting bookings only; no customer cancellation after service starts |
| Rescheduling | Own waiting scheduled bookings only; normal staffing and availability checks apply |
| Pickup | No pickup confirmation or history archival actions |
| My Bookings | Own records only |
| Feedback | Own completed reservation only, once per reservation; existing testimonials remain readable |
| Notifications | Own feed and own read state only |
| Profile | Own permitted profile/security fields only; role and ownership cannot be set by customer input |
| Employee and management functions | No attendance, shifts, employee management, pricing changes, analytics exports, backup/recovery or delivery-log management access |

## Changes

- The start-service API already rejected customer access. This review did not reproduce a customer successfully changing a reservation to in_progress through that API.
- Removed employee-only action buttons from server-rendered customer queue markup and guarded the shared service-action helper.
- Closed direct-API customer walk-in submission and cancellation of in-progress bookings.
- Removed unnecessary personal details from other customers' queue entries.

## Validation

All 41 automated tests passed. Customer denial checks cover every route declared for employee/management roles, including direct API requests. Ownership, cancellation state, scheduling requirements, forged booking fields and queue privacy have explicit tests. Browser checks opened waiting, in-progress and pending-confirmation details for Customer, Staff and Owner without JavaScript errors. Customer service action controls were absent.

Changes are local until deployed. If the deployed site still allows a customer service start, capture the endpoint, response and affected reservation state, and confirm the account role and deployed revision. Creating a reservation is intentionally different from starting its wash.
