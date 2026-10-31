# Applicant portal (`utsu_portal`)

FastAPI, database-agnostic form application. Install with `pip install -e ".[portal]"` and run:

```
PORTAL_SECRET=<long random, stable> PORTAL_ADMINS=alice uvicorn --factory utsu_portal:create_app
```

`PORTAL_STORE` selects the backend: `memory` (default), `sqlite:<path>` or `firestore` (`.[firestore]`). Add a backend by
implementing `utsu_portal.store.Store` (get/put/delete/list/create). `PORTAL_SECRET` peppers the username and student-id
hashes, so it must stay stable.

## Identity and accounts
- `uuid` = HMAC(secret, username) (v5-style); the full student number becomes a secondary uuid, stored only as a
  uniqueness index (`student_ids`). The **user record** holds only `uuid, email, preferred_name, first_name, last_name,
  student_number` (last 4 digits). Account status/role live in a separate `accounts` collection.
- Passwordless: username, then an emailed 6-digit OTP (hashed, expiring, attempt-limited) gives a signed bearer token.
- Activation: email OTP, then a live face + ID capture (browser camera, server-issued challenge phrase), current
  division codes and legal names, then **manual admin approval**. Only `verified` accounts can submit forms. Images are
  deleted as soon as a decision is made. Usernames listed in `PORTAL_ADMINS` are admins and skip review.
- Only `preferred_name` is user-mutable (`PATCH /api/me`). Recovery (`/api/recovery/*`): username + last 4 digits
  emails a sign-in code to the address on file; email or legal-name changes need the new-email OTP plus a full
  face + ID request approved by an admin.
- Note: a camera capture plus challenge phrase is not cryptographic proof of liveness; admins make the final decision.

## Forms
YAML definitions (see `src/utsu_portal/forms/` for the Senate, SABP and AGM examples) are validated strictly (unknown
keys rejected, `show_if` conditionals, types `text textarea email number date select radio multiselect checkbox info`).
Each definition has an integer `version` and a content hash. Published versions are immutable: saving or publishing a
changed definition under an already-published version is a `409` ("bump the version"). Bundled forms are checked the
same way at startup. Submissions record the version and hash and are rejected if stale.

- Public `GET /api/forms/{id}/static` returns the compiled JSON (ETag = hash); the browser rebuilds, renders and
  validates the form itself (`static/forms.js`) and only posts the data. The server re-validates and sanitises
  (NFC, control/bidi characters stripped, length caps, unknown fields rejected).
- Admin: draft/publish/unpublish, YAML editor with live preview and a field builder (`/api/admin/forms/convert`
  converts YAML and schema JSON), optimistic `base_rev` concurrency, and realtime draft sync over SSE
  (`/api/admin/forms/{id}/events`, an in-process bus; replace `EventBus` for multi-worker deployments).
- Applicants see a status page (`/api/me/submissions`); admins set statuses (`submitted`, `under_review`,
  `needs_info`, `accepted`, `rejected`).

## Extending
A module is an object with `name` and `router(ctx)`; pass it via `create_app(extra_modules=(...))`. `ctx` provides
the store, settings, mailer (`utsu_portal.mailer.Mailer`, override `send` for real mail), event bus and auth
dependencies (`require_user`, `require_verified`, `require_admin`).

## Clubs (`modules/clubs.py`)
- **Registration:** a verified user registers a club (`POST /api/clubs`, unique name) and becomes its owner, who can add
  `treasurer`/`executive` members. Admins approve via `POST /api/admin/clubs/{id}/decision`; it needs
  `club_registration_approvals` (default 2) **distinct** admins, and any rejection is final. Admins who belong to a club
  cannot approve its items.
- **Finances:** an integer-cents ledger. Admins add grants; owners/treasurers record income (auto-approved) and expenses.
  Expenses need at least one receipt and admin approval: 1 approver, or 2 at/above `expense_large_cents` (default $500).
  An expense cannot be approved beyond the club balance.
- **Receipts:** base64 jpeg/png/webp/pdf uploads (size-capped), de-duplicated per club by SHA-256, each usable once.
- **Tax:** each entry stores net/tax/gross using a rate in basis points (default 1300 = Ontario HST; 0 for exempt) and
  inclusive/exclusive entry. `GET /api/clubs/{id}/summary` reports balance, tax paid (input tax credit eligible), tax
  collected, net tax position and spend by category, counting approved entries only.
- The approval mechanism is the reusable `record_decision` helper (N distinct approvers, no self-approval).

## AGM: SSO provider, online ID verifier, invitation panel and check-in
- **SSO provider** (`modules/sso.py`, part of the core): OpenID Connect authorization-code flow with RS256 ID tokens
  (`/sso/.well-known/openid-configuration`, `/sso/jwks`, `/sso/authorize`, `/sso/token`, `/sso/userinfo`), optional PKCE,
  single-use 60 s codes, exact redirect-URI matching and pairwise subjects. Only name and email are released. Modules
  register relying parties via `ctx.services["sso"].register_client(...)` with an eligibility callback. Set
  `PORTAL_SSO_KEY` (PEM) so keys survive restarts, and `PORTAL_BASE_URL` as the issuer.
- **Online ID verifier** (`modules/agm_verify.py`, separate from account activation): an `agm_rsvp` registrant who chose
  *Online* submits a fresh live face + ID scan; an admin approves it (or `PORTAL_AGM_VERIFY_MODE=auto`), the images are
  purged, and a 12 h grant is recorded. Zoom SSO is refused without that grant.
- **Invitation panel** (`modules/agm.py`, "AGM" in the portal): in-person registrants see a QR code of their
  registration UUID; online registrants verify, then "Log in to Zoom" returns the Zoom link and SSO start URL
  (`PORTAL_ZOOM_URL`, `PORTAL_ZOOM_SSO_URL`, `PORTAL_ZOOM_CLIENT_SECRET`, `PORTAL_ZOOM_REDIRECT_URIS`).
- **Check-in screen** (`/static/checkin.html`, admins only): scan the QR (camera where `BarcodeDetector` exists, or a
  keyboard-wedge scanner / paste) to show the attendee's name, pronouns (an optional `pronouns` field on the RSVP form,
  v2, since the user record does not store them) and a "Checked in" message; repeat scans are flagged as duplicates.
