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
| `PORTAL_ADMINS` | bootstrap admin usernames |
| `PORTAL_BASE_URL`, `PORTAL_SSO_KEY` | SSO issuer URL and RSA private key (PEM) |
| `PORTAL_SMTP_URL`, `PORTAL_MAIL_FROM` | OTP email over any SMTP service; without it codes are only logged |
| `PORTAL_ZOOM_*`, `PORTAL_AGM_VERIFY_MODE` | see the AGM section of [Applicant portal](Applicant-Portal.md) |

`GET /healthz` is the liveness probe. The container listens on `$PORT`.

## Database portability
Everything goes through the five-method `Store` interface (`get/put/delete/list/create`; `create` must be atomic). Built in:
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
