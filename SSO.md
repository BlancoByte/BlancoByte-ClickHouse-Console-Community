# SSO (OIDC) — Setup Guide

> **Status:** scaffold present, full integration deferred.
> The reason: OIDC requires real provider config (Okta / Azure AD / Google /
> Keycloak) to test end-to-end. Once you give me your provider's discovery
> URL + client ID/secret, I can wire in the callback flow in ~150 lines.
>
> Below is the **architecture and the wiring points** so you can either
> implement it yourself or hand it back for me to finish.

## Architecture

```
Browser ─── 1. /api/auth/sso/login ───▶  app.py (302 redirect)
       ◀────────────────────────────  to:  https://<provider>/authorize?...

Browser ─── 2. user authenticates at provider ─────▶ provider
       ◀── 3. provider redirects back ─────────────  to:  /api/auth/sso/callback?code=...

Browser ─── 4. /api/auth/sso/callback?code=... ──▶  app.py
                                                    ├─ exchange code for token at provider
                                                    ├─ fetch userinfo
                                                    ├─ create/update users row (sso_subject)
                                                    └─ set session cookie + redirect to /
```

## Configuration (env vars)

```
OIDC_DISCOVERY_URL=https://login.microsoftonline.com/<tenant>/v2.0/.well-known/openid-configuration
OIDC_CLIENT_ID=...
OIDC_CLIENT_SECRET=...
OIDC_REDIRECT_URI=https://console.yourcompany.com/api/auth/sso/callback
OIDC_DEFAULT_ROLE=readonly                 # role assigned to new SSO users
OIDC_ROLE_CLAIM=groups                     # which JWT claim to map to roles (optional)
OIDC_ROLE_MAPPING={"clickhouse-console-admins":"admin","clickhouse-console-devs":"developer"}
```

## Database changes needed

Two columns to add to the `users` table (next iteration):

```sql
ALTER TABLE users ADD COLUMN sso_subject TEXT;        -- OIDC `sub` claim
ALTER TABLE users ADD COLUMN sso_provider TEXT;       -- "azure-ad" / "okta" / etc.
CREATE INDEX idx_users_sso ON users(sso_provider, sso_subject);
```

## Endpoint scaffold (already in `app.py`, returns 501 until configured)

- `GET  /api/auth/sso/providers`  → list configured providers (or empty)
- `GET  /api/auth/sso/login`      → 302 to provider authorize URL
- `GET  /api/auth/sso/callback`   → exchange code, create session

## Frontend integration

Add a button on the login screen: **“Sign in with SSO”**.
On click, navigates to `/api/auth/sso/login`. The callback sets the cookie and
redirects to `/` — same UX as the current local login.

## Recommended providers

| Provider     | Discovery URL pattern                                                              |
|--------------|------------------------------------------------------------------------------------|
| Azure AD     | `https://login.microsoftonline.com/<tenant>/v2.0/.well-known/openid-configuration` |
| Okta         | `https://<your-org>.okta.com/.well-known/openid-configuration`                     |
| Google       | `https://accounts.google.com/.well-known/openid-configuration`                     |
| Keycloak     | `https://<keycloak>/realms/<realm>/.well-known/openid-configuration`               |

## Once you're ready

Send me your discovery URL, client ID, and the role-claim you want to map. I'll
finish the callback flow in the next iteration.
