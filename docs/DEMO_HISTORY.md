# 60-workday presentation history

On Render, open the service's Environment settings and add:

```dotenv
MD57_DEMO_HISTORY=true
```

Save and redeploy the latest code. Startup generates the previous 60 complete
Monday-Friday workdays using Asia/Manila dates. The log confirms when loaded.
The history includes completed and cancelled bookings, three disabled demo
staff accounts, attendance and linked feedback. Customers use demo names and
plates; no email addresses, notifications or usable staff passwords are created.
Existing accounts and records stay unchanged.

Log in with your existing Owner account. Open History or Manager Analytics,
select the last 90 days and choose All records or Simulated records. Real records
intentionally excludes this batch. History identifies DEMO plates, and analytics
and reports retain their data-source disclosure; no notice appears site-wide.
No historical model predictions are invented. These rows cannot establish real
accuracy and are excluded from real-data model training and holdout evaluation.
Use actual observed walk-in services for those measurements.

Repeated starts on the same database reuse the first batch, without adding rows
or moving its dates. Free Render storage can disappear on restart or sleep; if
the database is reset, startup generates another demo batch in the new database.
Keep DEFAULT_ADMIN_PASSWORD configured for the fresh Owner bootstrap in that
case. Download a private backup from Operations before the presentation if you
need to keep any work done during the session.

Setting MD57_DEMO_HISTORY=false stops generation and the history-page notice;
it does not remove records or their synthetic provenance in analytics. Keep it
enabled for the presentation. Do not use this setting on a real shop deployment.
