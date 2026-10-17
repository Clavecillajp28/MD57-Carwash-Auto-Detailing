# API and email check ? 17 October 2026

## Verified

- Local Gmail SMTP: connection, TLS negotiation and authentication passed. No message sent.
- Local sender is configured and uses the gmail.com domain. Local transport is SMTP.
- Current weather and forecast: live API responses HTTP 200 with valid payloads using local configuration.
- Deployed application: responds with HTTP 302.
- Deployed Google sign-in: redirects to Google with callback https://md57-carwash-auto-detailing.onrender.com/auth/google/callback. Interactive sign-in was not completed.
- Google discovery endpoint: HTTP 200.
- 35 automated tests passed, including email transport payloads, provider rejection/timeout handling, delivery callbacks, access control and operational workflows. Provider sending in these tests is mocked.
- 160 parameter-free GET API checks across Customer, Staff, Manager and Owner using disposable storage: 97 HTTP 200, 59 HTTP 403, 4 HTTP 400, no HTTP 500. These are smoke checks, not proof of every response's business correctness.

## Shared sender coverage

Registration verification, password-reset codes, employee invitations, owner security codes, profile verification/security notices, ready-for-pickup notifications, no-show/staff-absence notices and shift reminders all route through send_branded_email, the persisted outbox and the shared delivery function. Both SMTP and Brevo use MAIL_DEFAULT_SENDER. SMTP defaults to MAIL_USERNAME only when MAIL_DEFAULT_SENDER is absent. Brevo uses its API key; MAIL_USERNAME and MAIL_PASSWORD are not used by that transport.

## Fix applied

Removed a stray `il` expression at the end of _Backend/app.py that raised NameError during application startup. Full tests pass after removal. This local fix has not been deployed by this check.

## Pending verification

Render environment values were not accessible. Local .env has no BREVO_API_KEY or BREVO_WEBHOOK_TOKEN, so Render's Brevo credentials, registered/active sender and delivery webhook cannot be verified from this workspace. Do not copy local SMTP settings over Render's Brevo setup on the basis of this report.

A test recipient is needed before sending a single real delivery test. Provider acceptance, delivered events and Gmail inbox/spam placement are separate outcomes. No emails were sent during this check. Background reminders require the host workers to be awake, and their live operation was not verified.

## Authorized delivery test update

After the read-only checks, the user authorized one message to jonellevy123@gmail.com. The shared branded delivery function sent the test using local SMTP settings; SMTP accepted it. Inbox/spam placement requires recipient confirmation. Render/Brevo remains unverified. No automatic resend was attempted.
