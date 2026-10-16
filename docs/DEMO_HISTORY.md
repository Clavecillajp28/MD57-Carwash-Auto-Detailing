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

Log in with your existing Owner account. History and Analytics open on **Live
operations**, showing actual employee work. Managers can switch **Record source**
to **Presentation history** to see the generated batch; in Analytics select the
last 90 days. The presentation view has a small **Simulated records** label.
Both views use the real service catalog, but generated jobs retain fictional
staff identities. PDF and CSV exports preserve the selected source. Staff logins
see only their own live service history, and live staff metrics, estimated
durations and the feedback wall exclude presentation records. There is no
site-wide notice.
No historical model predictions are invented. These rows cannot establish real
accuracy and are excluded from real-data model training and holdout evaluation.
Use actual observed walk-in services for those measurements.

Repeated starts on the same database reuse the first batch, without adding rows
or moving its dates. Free Render storage can disappear on restart or sleep; if
the database is reset, startup generates another demo batch in the new database.
Keep DEFAULT_ADMIN_PASSWORD configured for the fresh Owner bootstrap in that
case. Download a private backup from Operations before the presentation if you
need to keep any work done during the session.

Setting MD57_DEMO_HISTORY=false stops generation;
it does not remove records or their source labels. Keep it
enabled for the presentation. Do not use this setting on a real shop deployment.
