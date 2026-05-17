# Authentication and Email

Covers the full auth surface: registration, login, JWT lifecycle, email verification, and password reset. The matching frontend doc is `frontend-next/AUTH_FLOWS.md`.

## JWT

| Field | Value |
|---|---|
| Algorithm | HS256 (`app/core/security.py`) |
| Signed by | `JWT_SECRET` env var |
| Lifetime | 7 days (10,080 minutes) |
| Header claim | `type=access` (rejected if not present) |
| `sub` claim | `User.id` |
| Refresh | None — clients re-login when the token expires |

### Issuing

A token is issued by:
- `POST /api/auth/register` — also returns the new user
- `POST /api/auth/login`

No other route issues tokens. The reset/verification endpoints **never** return a JWT; the user must log in again after resetting.

### Validating

`get_current_user` (`app/api/deps.py:13`) on every protected route:

1. Reads `Authorization: Bearer <token>`. Missing or wrong scheme → 401.
2. Decodes the JWT. Expired or malformed → 401.
3. Checks the `type` claim is `access`. Otherwise → 401.
4. Loads the user from the DB by `sub`. Missing → 401.
5. Rejects if `is_active=False` → 403 ("Account disabled").

**Important:** the user's `is_admin` flag is read from the DB on every request. The JWT's `is_admin` claim is **not** used for authorization — role changes take effect immediately, no re-login required.

## Registration

```
POST /api/auth/register   {username, password, email?}
  ↓ first user (user_count == 0) → is_admin = True
  ↓ subsequent users → is_admin = False
  ↓ email provided  → generate verification token, send_verification_email()
                      token TTL: 7 days
  ↓ returns { access_token, user }
```

- Username uniqueness is enforced by a DB unique constraint; collision → 400.
- The verification email is sent best-effort. **If SMTP is not configured the warning is logged and registration still succeeds.** The user can call `POST /api/auth/resend-verification` later once SMTP is set up.

## Email verification

```
1. Registration (with email)           → token written to user row
                                       → POST /verify-email link sent
2. User clicks link in email
3. Frontend extracts ?token=… from URL
4. POST /api/auth/verify-email?token=…
   ├─ token not found  → 400 "invalid or expired"
   ├─ token expired    → 400 "invalid or expired"
   └─ ok               → user.email_verified = True, token cleared
```

`POST /api/auth/resend-verification` (auth required) generates a fresh 7-day token. Used when the original email never arrived or the token expired.

## Password reset

```
1. POST /api/auth/request-password-reset {email}
   ├─ email not found OR is_active=False → 204 (silent — anti-enumeration)
   └─ found → password_reset_token written, send_password_reset_email()
              token TTL: 1 hour
2. User clicks link in email
3. Frontend extracts ?token=… and calls:
   GET  /api/auth/validate-reset-token?token=…   (UX-only preflight)
        ├─ invalid/expired → 400
        └─ ok              → 204
   POST /api/auth/reset-password {token, new_password}
        ├─ invalid/expired → 400
        └─ ok              → password updated, token cleared, 204
4. User logs in with new password (no JWT issued by reset endpoint)
```

Tokens are random 256-bit values (`secrets.token_urlsafe(32)`). They are single-use — successful reset clears the row's `password_reset_token`.

## Account enumeration prevention

`request-password-reset` returns 204 whether or not the email exists. The verification endpoint, however, returns 400 on invalid tokens — that's fine because the token itself is opaque and unguessable.

If you want to harden verification too (e.g., to prevent "is this token in our system?" probes), return 204 instead of 400.

## SMTP configuration

Email sending is implemented in `app/services/email.py`. The module is **best-effort**: any SMTP error is logged and returns `False`. **Registration and password reset still succeed** even when email fails to send — the user just doesn't get a link.

### Required env vars (for real delivery)

| Variable | Default | Purpose |
|---|---|---|
| `SMTP_HOST` | *(empty)* | If empty, all sends are skipped with a warning log |
| `SMTP_PORT` | `587` | |
| `SMTP_USERNAME` | *(empty)* | Optional |
| `SMTP_PASSWORD` | *(empty)* | Optional |
| `SMTP_FROM_EMAIL` | *(empty)* | Required for the `From` header |
| `SMTP_FROM_NAME` | `A6 Stern` | Display name on the `From` header |
| `SMTP_USE_TLS` | `true` | `true` → STARTTLS, `false` → implicit SSL |
| `FRONTEND_URL` | `http://localhost:5173` | Prepended to `/verify-email?token=…` and `/reset-password?token=…` links |

### Behavior when SMTP is not configured

`SMTP_HOST` empty → every `send_email` call logs:

```
WARNING  SMTP_HOST not configured — skipping email to user@example.com (Verify your email address — A6 Stern)
```

You can still complete the full token flow without SMTP by reading the token from the database via pgAdmin:

```sql
SELECT email_verification_token FROM users WHERE username = 'alice';
SELECT password_reset_token       FROM users WHERE email    = 'alice@example.com';
```

Then call the corresponding endpoint with that token. This is the recommended way to verify the flow in dev.

### Tested providers

- **Resend** — `SMTP_HOST=smtp.resend.com`, `SMTP_PORT=587`, `SMTP_USERNAME=resend`, `SMTP_PASSWORD=<api-key>`, `SMTP_USE_TLS=true`
- **Postmark** — `smtp.postmarkapp.com:587`, username = API token, password = API token, STARTTLS
- **Mailgun** — `smtp.mailgun.org:587`, STARTTLS
- **Gmail** — `smtp.gmail.com:587`, STARTTLS, **App Password** (not the account password)

### `FRONTEND_URL` must be browser-reachable

Both email templates build links like `${FRONTEND_URL}/verify-email?token=…`. If `FRONTEND_URL` points at an internal Docker hostname (`http://frontend-next:3000`), the link in the email will not resolve from the user's browser. Set this to the public URL the browser uses to reach the app.

## Session model on the frontend

The frontend stores `{ accessToken, user }` in `localStorage` under `a6_auth_v2`. On any 401 response the client clears the key and redirects to `/login`. See `frontend-next/AUTH_FLOWS.md`.

## Operational checklist

- Set a strong random `JWT_SECRET` (32+ bytes). Tokens issued under the old secret are invalidated on rotation.
- Configure SMTP before user-facing rollout if you want email verification to be meaningful.
- Set `FRONTEND_URL` to the public address.
- After rotation of any user's password externally (e.g., compromised account), no further action is needed — JWTs do not include a password hash, but if you want to **force re-login**, also rotate `JWT_SECRET`.
