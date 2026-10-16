# Use existing employee logins on the hosted installation

The deployed database is separate from the local one. This private transfer
copies active, non-owner staff and manager accounts with their existing password
hashes. It does not copy plaintext passwords, Owner accounts, disabled legacy
accounts, bookings, attendance, MFA secrets or other private database tables.
Employees retain their original login method; Google-linked employees still use
Google sign-in. Existing deployed usernames, employee names and contact emails
are skipped so no existing password or role is overwritten.

1. In the local project folder, run:

   ```powershell
   python _Backend/employee_transfer.py
   ```

2. The private encrypted file is written to
   `.runtime/employee-transfer.md57employees`. It is excluded from Git and Vercel
   uploads. Keep it private; it contains account details and password hashes.
3. Set the same private `MD57_EMPLOYEE_TRANSFER_KEY` on the local and hosted
   installations. If unset locally, the exporter generates and reuses
   `.runtime/employee-transfer.key`; configure that value privately on Render.
   Never paste it into chat or commit it. Preserve each installation's existing
   `SECRET_KEY`; transferring employees does not require rotating session keys.
   Older export files remain compatible with the original session secret when
   no separate transfer key is configured on the destination. The explicit
   `--legacy-session-key` export flag is available only for that older workflow.
4. Deploy the latest commit. Sign in to the hosted installation as its Owner,
   then open **Operations and recovery** (`/operations`).
5. Under **Import existing employee logins**, choose the encrypted file and
   click **Import employees**. If prompted, verify the security code in Account
   Security first and retry. The import reports added and skipped counts.
6. Sign out and test one imported employee using their previous username and
   password. Active employees retain their local manager/staff role, but do not
   acquire Owner rights. New database IDs are assigned deliberately; unrelated
   deployed history is not attributed to imported employees.

The endpoint requires an Owner session and CSRF protection, validates the
authenticated encrypted payload, limits upload size, and imports in one
transaction. Re-uploading the same file does not duplicate accounts.

On free Render hosting, a database reset also removes imported employees.
Re-import the file if that happens. Existing generated demo staff stay disabled
and are separate from real employee logins.
