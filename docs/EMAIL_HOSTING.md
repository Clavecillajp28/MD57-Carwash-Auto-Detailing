# Email on Render Free

Render Free blocks outgoing SMTP ports 25, 465 and 587. Gmail SMTP configured
in MAIL_SERVER/MAIL_PORT therefore cannot deliver invitations or security codes
there. Use HTTPS transactional email; this application supports Brevo's API.

1. Create a Brevo account and enable transactional email. Verify a sender address
   following Brevo's instructions; complete any account approval or domain
   authentication the provider requires.
2. Create an **API key**, not an SMTP key. Keep it private.
3. In Render's service Environment settings, set:

   ```dotenv
   MAIL_TRANSPORT=brevo
   BREVO_API_KEY=your_private_api_key
   MAIL_DEFAULT_SENDER=the_sender_address_verified_in_brevo
   ```

4. Save and deploy the latest commit. Existing Gmail credentials can stay for
   local use but are not used when MAIL_TRANSPORT=brevo.
5. Send a new invitation from Accounts. The previous failed invitation was
   revoked, so its link cannot be used. Check the recipient inbox and spam
   folder, along with Brevo's transactional logs if delivery is delayed.

All branded application mail uses this transport, including invitations,
verification, password recovery and pickup notifications. The existing outbox
continues to track provider acceptance; acceptance does not prove inbox delivery.
There is no automatic fallback to SMTP on API failure. Local development
continues to use SMTP unless explicitly configured otherwise.

This code does not send any message during setup or testing; the owner sends
messages through normal application actions. API emails retain the MD57 text
branding without the SMTP-only inline logo attachment.

References: [Render Free limits](https://render.com/docs/free),
[Brevo send API](https://developers.brevo.com/docs/send-a-transactional-email).
