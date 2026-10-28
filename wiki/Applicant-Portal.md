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
