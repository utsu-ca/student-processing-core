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
  - Supabase / any PostgreSQL: `PORTAL_STORE=sql:******host:5432/db` (psycopg is in the image).
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
