# Deployment (Cloud Run + Firestore, and how to avoid lock-in)

The portal is a stateless container configured only through `PORTAL_*` environment variables. All GCP-specific code lives
in `deploy/gcp/deploy.sh`; the application never imports a cloud SDK except the Firestore adapter, which is loaded only
when `PORTAL_STORE=firestore`.

## Deploy to GCP
In Google Cloud Shell (billing-enabled project): `./deploy/gcp/deploy.sh`. It is safe to re-run and, like
[agm-gcp-runbook](https://github.com/utsu-ca/agm-gcp-runbook), it: enables APIs, creates a native-mode Firestore database
and a least-privilege service account (`datastore.user`), deploys Cloud Run with ingress limited to the load balancer, and
creates Cloud Armor (SQLi/XSS WAF, per-IP throttle), a global HTTPS load balancer, static IP and managed certificate.
Differences: applicants are the public so there is no site-wide IP allowlist (set `ADMIN_IPS` to restrict only
`/api/admin` and the check-in page); secrets (`PORTAL_SECRET`, the SSO signing key, SMTP URL, Zoom client secret) are
generated once into Secret Manager and mounted as env vars. **Never rotate `PORTAL_SECRET`**: it peppers all uuid hashes.

## Configuration
| Variable | Purpose |
|---|---|
| `PORTAL_SECRET` (required) | stable pepper for username/student-id hashes and tokens |
| `PORTAL_STORE` | `memory`, `sqlite:<path>`, `sql:<url>` (or `sql` + `DATABASE_URL`), `firestore`, or `pkg.module:factory` |
| `PORTAL_ADMIN_EMAILS` | emails that become admin once verified by OTP |
| `PORTAL_BASE_URL`, `PORTAL_SSO_KEY` | SSO issuer URL and RSA private key (PEM) |
| `PORTAL_SMTP_URL`, `PORTAL_MAIL_FROM` | OTP email over any SMTP service; without it codes are only logged |
| `PORTAL_ZOOM_*`, `PORTAL_AGM_VERIFY_MODE` | see the AGM section of [Applicant portal](Applicant-Portal.md) |

`GET /healthz` is the liveness probe. The container listens on `$PORT`.

## Database portability
Everything goes through the six-method `Store` interface (`get/put/delete/list/create/replace`; `create` and `replace` must be atomic). Built in:
- **Firestore** (wrapped in `ChunkingStore`, which shards values over Firestore's 1 MiB document limit, e.g. receipts and scans).
- **SQL** via SQLAlchemy: a single `portal_docs(col, id, body)` table created automatically, so there are no migrations.
  - Supabase / any PostgreSQL: `PORTAL_STORE=sql:postgresql+psycopg://USER:PASS@HOST:5432/DB` (psycopg is in the image).
  - Azure SQL / SQL Server: `sql:mssql+pyodbc://...?driver=ODBC+Driver+18+for+SQL+Server` (install `pyodbc` and the ODBC driver in a derived image).
  - MySQL: `sql:mysql+pymysql://...`.
- **Custom**: implement `Store` and point `PORTAL_STORE` at `my_pkg.module:factory`.

`list()` filters by top-level equality; the SQL backend filters in Python, which is fine for AGM/club scale but add an
indexed column if a collection grows to many hundreds of thousands of rows. The suite runs the same contract tests on every
backend (`tests/test_stores.py`).

## Moving off GCP
Build the image (`docker build .`) and run it anywhere (Azure Container Apps, Fly, Kubernetes): set the env vars above,
supply secrets from that platform's vault, and choose a store. Two things to know when scaling past one instance:
realtime admin sync uses an in-process `EventBus` (swap it for Redis/pub-sub), and the Cloud Armor rules should be replaced
by your platform's WAF. Until then `deploy.sh` pins `--max-instances=1`.

## Scaling out: Redis
Set `PORTAL_REDIS_URL` (e.g. `rediss://:PASSWORD@HOST:6379`) and every replica shares (a) the realtime admin-sync bus
(pub/sub behind the SSE endpoint) and (b) the rate-limit counters (`/api/auth/*`, `/api/recovery*`, `/sso/*` POSTs,
`PORTAL_RATE_LIMIT_AUTH` per minute per client IP, default 30). Without it both are in-process, so run a single replica
(this is why `deploy.sh` defaults to `MAX_INSTANCES=1`; raise it after adding Redis/Memorystore). Redis holds nothing
durable. Realtime publish failures are logged, never fail the write. The rate limiter fails *open* if Redis is down (OTP
attempt limits still apply); `/readyz` reports Redis health.

## Standalone authentication service
The auth API runs independently of the portal: `PORTAL_APP=utsu_portal.authsvc:create_auth_app` (same image) or
`uvicorn --factory utsu_portal.authsvc:create_auth_app`. It owns accounts, users, OTPs, identity-verification review, the
session-signing key, its own store and audit log (`/api/admin/auth-audit`).

| | Auth service | Portal (`PORTAL_AUTH_URL` set) |
|---|---|---|
| Holds accounts / users / scans | yes | **no** (modules that mutate them are not even mounted) |
| Session token | signs (Ed25519, `PORTAL_AUTH_SIGNING_KEY`) | verifies offline with `PORTAL_AUTH_PUBLIC_KEY` only; cannot mint sessions |
| Live status/role, name lookups | serves `/internal/*` | calls it (cached 15 s, **fails closed** if unreachable) |
| Secrets | own `PORTAL_SECRET`, data key, DB, SMTP | own `PORTAL_SECRET`, data key, DB; plus `PORTAL_SERVICE_TOKEN` |

`/internal/*` requires `PORTAL_SERVICE_TOKEN` (≥32 chars, constant-time compared; returns 404 otherwise or when unset),
exposes a minimal projection (no hashes or lookup keys), and must never be routed publicly (see `deploy/k8s`). In a
single-process deployment nothing changes: the same Ed25519 tokens are signed with a key derived from `PORTAL_SECRET`.
Use a short `PORTAL_TOKEN_TTL` (e.g. 900) in split mode. `PORTAL_AUTH_URL` must be https or a cluster `.svc` address.
Route `/api/auth`, `/api/me`, `/api/verification`, `/api/recovery`, `/api/admin/verifications`, `/api/admin/auth-audit` to
the auth service and everything else to the portal (`deploy/k8s/ingress.yaml`). Rotate signing keys by giving the portal
two concatenated public PEMs, then switching the auth service key.

## Kubernetes
`kubectl apply -k deploy/k8s` (after creating the secrets from `secret.example.yaml`). Includes: restricted namespace,
auth + portal Deployments (non-root UID 10001, read-only root FS, all capabilities dropped, seccomp, no service-account
token, secrets as read-only files via `*_FILE` variables, `/readyz` readiness and `/healthz` liveness, rolling update with
zero unavailable), HPA, PodDisruptionBudgets, password-protected Redis, default-deny NetworkPolicies (only portal→auth,
→Redis, DNS and database/SMTP egress; narrow those to CIDRs), and an Ingress with TLS, body limit and SSE-safe buffering.
Every secret-bearing variable also accepts `NAME_FILE`, so use External Secrets / a Secret Manager CSI driver instead of
literal Secrets in production.

## Data protection and audit
- Face/ID scans, receipts and audit snapshots are Fernet-encrypted before they reach the database
  (`PORTAL_DATA_KEY`, comma-separated for rotation: first key encrypts, all decrypt; derived from `PORTAL_SECRET` if unset).
  Scans are deleted after the decision. The user record still holds only the five permitted fields.
- `PORTAL_SECRET` must be ≥32 chars. Responses carry HSTS, CSP (`frame-ancestors 'none'`), `X-Frame-Options`,
  `Referrer-Policy`, `Permissions-Policy` and `no-store` for API calls; requests above the 8 MB body limit get 413.
- Audit log: append-only, HMAC hash-chained (`prev`/`hash`, sequence claimed with atomic `create` so replicas can't fork it).
  Every admin request and every state-changing request is logged (actor, route, status, keyed-hash of IP; never bodies,
  query strings, tokens, OTPs or images), and key changes (registration, verification decisions, form draft/publish,
  submissions, club/transaction decisions, check-ins, preferred-name changes) also store compressed (zlib), encrypted
  before/after snapshots with secrets redacted. Admin API: `GET /api/admin/audit` (filter `actor`, `action`, `target`),
  `/{id}/diff` (two-column, word-level, Wikipedia-style revision diff plus a unified diff), `/compare?a=&b=` (diff any two
  entries, e.g. two form revisions), `/verify` (detects edited, removed or reordered entries), `/export` (gzip NDJSON).
  A tamper-evident chain cannot stop someone with both the database and `PORTAL_SECRET`; ship exports to write-once storage.

## Optimistic locking
Concurrent writers (several replicas, two admins, two check-in scanners) are handled with optimistic concurrency, not
locks. Documents written through `create`/`update` carry a `_rev` counter. `Store.replace(col, id, doc, expected_rev)` is an
atomic compare-and-swap (SQL: update guarded on the exact body read, so it works on every dialect with no extra column and no
migration; Firestore: a transaction; memory/SQLite: lock + guard), and `Store.update(col, id, fn)` is the read-modify-write
loop: `fn` is re-run on a fresh read if someone else won (bounded retries with jitter, then `Conflict`), so it must be
side-effect free and may raise `HTTPException` to abort. A new backend only has to implement `replace`.
Used for: one-shot decisions (verification, AGM check, club and expense approvals each count every vote and decide once;
receipts back one transaction), form drafts (on top of the editor's `base_rev` check, so a stale save gets 409),
publish/unpublish, submission edits vs. admin decisions, OTP attempt counting and single redemption, SSO authorization codes
(single redemption), account activation, and check-in. Blind `put` is kept only where the last writer may legitimately win.

## Auth audit ledger: monthly tables, checkpoints, anchoring

The identity service (`utsu_portal.authsvc`) records everything in a tamper-evident log. Design choices versus a "hash the whole DB nightly / log every query" approach, which is expensive and bloats the DB:

- **Monthly tables** `audit_YYYY_MM`; one HMAC hash chain spans all months. Registry: `audit_months` (hot → archived → dropped).
- **DB access is audited per request**, not per query: with `PORTAL_AUDIT_DB_ACCESS=1` (always on in the auth service) one `db.access` entry lists the requester, collections, ops, and capped ids (never values). All state changes carry redacted, compressed, encrypted before/after snapshots, so the log fully reconstructs history.
- **Nightly checkpoint** (`audit_checkpoints`): an incremental Merkle root over the entries since the last checkpoint, chained to the previous checkpoint digest; skipped when nothing meaningful happened. The checkpoint is also written to the log.
- **Blockchain anchoring** via a provider submodule, off by default (`PORTAL_ANCHOR=none`). Only the 32-byte checkpoint digest leaves the system. Providers: `local` (dev), `ots` (OpenTimestamps/Bitcoin, no keys/fees; proofs need a later upgrade step), `http` (`PORTAL_ANCHOR_URL`/`PORTAL_ANCHOR_TOKEN` gateway to any chain), or `pkg.module:factory`. Failures leave the checkpoint `pending` and are retried; the service never blocks on the chain.
- **Archive after ~90 days**: a month is verified, gzipped into `audit_archive` (sha256, Merkle root, boundary hashes). **Drop after ~180 days** only if the archive re-verifies and an anchored checkpoint covers it. The chain stays verifiable after a drop.
- **Weekly verify** walks the hot chain, checkpoint chain/roots, and archive checksums; results go to `audit_checks` and the log.
- **Scheduling**: `python -m utsu_portal.authsvc maintain [--force]` (k8s `deploy/k8s/maintenance-cronjob.yaml`, Cloud Scheduler, or `POST /api/admin/audit/maintenance/run`). Jobs claim themselves with optimistic locking, so duplicates are safe.
- Admin API: `/api/admin/audit/{verify,checkpoints,checks,tables,archive/{month},maintenance/run}`.

Limits: the HMAC chain cannot stop an attacker holding both the DB and `PORTAL_SECRET`; the external anchor is what covers that. The OTS/HTTP providers, the CronJob and non-SQLite stores are untested against real services.

## Roles (RBAC)

The auth service is the only issuer of roles (`modules/roles.py`). They are stored on the account (`roles`), exposed to the portal through the service-token-gated `/internal/accounts` projection (cached ~15 s, so a revocation reaches every service within that time), and are **not** embedded in session tokens.

| Role | Grants |
|---|---|
| `admin` | everything, plus granting roles and running audit maintenance |
| `verifier` | review face + ID verifications (portal and AGM) |
| `forms_editor` | form drafts, publish/unpublish, submissions |
| `checkin` | AGM check-in and attendance |
| `finance` | club transactions and grants |
| `clubs` | club registration decisions |
| `auditor` | read/verify/export the audit log |

Manage with `GET /api/admin/roles`, `GET /api/admin/roles/holders`, `PUT /api/admin/roles {username, roles}` (or the "Admin: roles" screen). Rules: admin only; no self-changes; the target must be a verified account; the last admin cannot be removed; each change is audited with before/after. `PORTAL_ADMIN_EMAILS` only bootstraps the first admin. Two admins demoting each other at the same instant could still leave none; recover by editing the account record directly. Legacy `role: admin` accounts keep working.

### Enforcing roles in the database too

The application checks above are the portable layer. Add database-side guards where the engine supports them (defence in depth if a service is compromised):

- **Separate DB identities per service.** The auth service connects as `auth_rw`, the portal as `portal_rw`. Grant `portal_rw` nothing on `accounts`, `users`, `otps`, `student_ids`, `emails`, `verifications`, `media`, `audit_*`; the portal then cannot read or alter identities even with a bug or stolen credentials. Better: a separate database/schema per service.
- **Append-only audit.** For `audit_*` tables grant the service `INSERT, SELECT` only (no `UPDATE/DELETE`), and run maintenance (archive/drop) under a separate `audit_maint` role. PostgreSQL/Supabase: `REVOKE UPDATE, DELETE ON ...`; Azure SQL: `DENY UPDATE, DELETE`.
- **Row-level security.** PostgreSQL/Supabase `CREATE POLICY` (and Azure SQL `SECURITY POLICY`) can restrict applicants to rows they own using a session setting such as `SET app.uid = ...`. The generic key-value `SQLStore` stores JSON documents, so this only fits if you map the high-value collections to real columns. Treat it as an optional hardening step, not something the portable store does.
- **Firestore.** Security rules apply only to client SDKs; server access is governed by IAM. Use one service account per service and a separate Firestore database (or project) for the auth service, since IAM cannot restrict individual collections.

## Sharing secrets between containers

Share as little as possible. What each container actually needs:

| Secret | Auth service | Portal |
|---|---|---|
| `PORTAL_AUTH_SIGNING_KEY` (Ed25519 private) | yes | **never** |
| `PORTAL_AUTH_PUBLIC_KEY` | no | yes (public, not secret) |
| `PORTAL_SERVICE_TOKEN` | accepts | presents (the only shared secret) |
| `PORTAL_SECRET` (pseudonymous ids, OTP hashing) | own value | own, different value |
| `PORTAL_DATA_KEY`, DB credentials | own | own |

Recommendations, strongest first:
1. **Don't share, derive trust.** Replace the shared service token with mTLS (service mesh such as Istio/Linkerd, or cert-manager issued certs) or platform identity (GCP Workload Identity + Cloud Run IAM `run.invoker`, Azure managed identity, Kubernetes projected service-account tokens with `TokenReview`). Then no static secret exists to leak.
2. **Secret manager, injected at start.** GCP Secret Manager, Azure Key Vault or HashiCorp Vault via External Secrets Operator or the Secrets Store CSI driver; mount as files and use the `*_FILE` variables so values never appear in `env`, logs or `docker inspect`. Scope each secret to one workload's identity (IAM/RBAC), enable audit logging on access.
3. **Kubernetes Secrets, if used directly:** one Secret per service (`auth-secrets`, `portal-secrets`), encryption at rest with a KMS provider, RBAC limiting `get secrets`, `automountServiceAccountToken: false`, NetworkPolicy so only the portal reaches `/internal`.
4. **Rotate without downtime.** `PORTAL_SERVICE_TOKEN` on the auth service accepts a comma-separated list: deploy `old,new` to the auth service, switch the portal to `new`, then drop `old`. Rotate `PORTAL_AUTH_SIGNING_KEY` by publishing the new public key beside the old (`PORTAL_AUTH_PUBLIC_KEY` accepts several PEMs) before signing with the new one. `PORTAL_SECRET` must never rotate (it derives identities).
5. Never put secrets in images, ConfigMaps, git, or build args; keep `deploy/k8s/secret.example.yaml` out of the kustomization.
