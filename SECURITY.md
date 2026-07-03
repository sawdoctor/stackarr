# Security

Stackarr is a small self-hosted app intended to run on your private network
(or behind a reverse proxy you control), alongside Audiobookshelf and Chaptarr.

## Model

- **Auth** is delegated to Audiobookshelf — users sign in with their own ABS
  credentials; Stackarr verifies via the ABS `/login` API and stores the
  returned ABS token (to read that user's history). Stackarr never stores ABS
  passwords. Tokens are kept in the SQLite DB under `/config` — protect that
  volume.
- **Sessions** are Flask signed cookies (random per-install secret), `HttpOnly`,
  `SameSite=Lax`. Set `STACKARR_HTTPS=true` when serving over HTTPS to add the
  `Secure` flag.
- **Framing/clickjacking:** embedding is restricted to the app's own origin by
  default. To embed in nzb360/a dashboard, set `STACKARR_FRAME_ANCESTORS` to that
  origin (or `*` to allow anywhere — not recommended).
- **Admin:** server-wide reads use `ABS_ADMIN_TOKEN`. Stackarr admins are listed
  in `STACKARR_ADMINS`. Instance-wide integration settings (SMTP, service
  connections, and the shared reading-list tokens — Hardcover / Goodreads) are
  visible and editable to admins only; regular users see only their own
  per-account preferences.
- **All routes require auth** (except `/login`, `/api/health`, the PWA manifest
  and service worker). Same level as Sonarr/Audiobookshelf.
- **Brute-force protection:** repeated failed logins from an IP are throttled
  (locked for ~15 min after 5 failures).
- **CSRF:** cookie-authenticated state-changing requests (POST/PUT/DELETE/PATCH)
  must be same-origin; cross-origin attempts are rejected. Automation that uses
  the `X-Api-Key` header is exempt (it isn't cookie-bound).

## Recommendations for public exposure

- Put it behind HTTPS (reverse proxy) and set `STACKARR_HTTPS=true`.
- Keep `STACKARR_FRAME_ANCESTORS` at the default unless you need embedding.
- Anyone with an account on your Audiobookshelf can sign in and request books
  (handed to Chaptarr). Only expose it to people you'd give that to.

## Reporting

This is a pre-release, vibecoded project — please open a GitHub issue (or a
private advisory for anything sensitive). No formal SLA.
