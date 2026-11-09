# Unified Observability, File Handling, and Audit Compliance

This document explains the three new production-grade systems added to the UTSU Portal microservice architecture:

1. **Unified Observability** - Distributed tracing, metrics, and structured logging across all clouds
2. **Cloud-Agnostic File Handler** - Unified file storage interface supporting multiple backends
3. **Centralized Authentication** - All service-to-service and user authentication under authsvc
4. **Audit Compliance** - Database patterns for compliance and regulatory requirements

## Table of Contents

1. [Unified Observability](#unified-observability)
2. [Cloud-Agnostic File Handler](#cloud-agnostic-file-handler)
3. [Centralized Authentication](#centralized-authentication)
4. [Audit Compliance Patterns](#audit-compliance-patterns)
5. [Architectural Decisions](#architectural-decisions)

---

## Unified Observability

### Overview

The observability module provides distributed tracing, metrics collection, and structured logging using **OpenTelemetry** - a vendor-agnostic standard that works identically across Kubernetes, Azure Container Apps, GCP Cloud Run, and AWS Fargate.

### Architecture

```
Application Code
    ↓
OpenTelemetry SDK (automatic instrumentation)
    ↓
┌─────────────────────────────────────────┐
│  Jaeger (distributed tracing)           │  Trace context propagated
│  Prometheus (metrics)                   │  across all services
│  JSON Structured Logs (logs)            │
└─────────────────────────────────────────┘
    ↓
Jaeger UI (trace visualization)
Prometheus UI (metrics dashboards)
Cloud logging (ELK, Datadog, Cloud Logging)
```

### Key Components

#### 1. Distributed Tracing (Jaeger)

Every request is assigned a unique `trace_id` that propagates across all services:

```python
from utsu_portal.observability import get_tracer, get_logger

tracer = get_tracer(__name__)
logger = get_logger(__name__)

# Start a span (traces a unit of work)
with tracer.start_as_current_span("send_email_to_students"):
    logger.info("Starting email campaign")
    
    # Logs automatically include trace_id and span_id
    # {"timestamp": "2024-11-09...", "trace_id": "abc123...", "span_id": "def456...", "message": "Starting..."}
```

**Benefits:**
- Follow a user request through all 4 microservices (portal → authsvc → mailsvc → filesvc)
- Identify which service is slow
- Correlate errors across services

#### 2. Metrics (Prometheus)

Automatic collection of:
- HTTP request latency and error rates
- Database query performance
- Service-specific business metrics
- Memory/CPU usage

```
# Example Prometheus query (in Grafana dashboard):
histogram_quantile(0.95, rate(http_request_duration_ms_bucket[5m]))
# Shows p95 latency of HTTP requests over last 5 minutes
```

#### 3. Structured JSON Logging

All logs are JSON for easy ingestion by centralized logging systems:

```json
{
  "timestamp": "2024-11-09T23:25:00.123Z",
  "level": "INFO",
  "logger": "utsu_portal.mailsvc",
  "message": "Email sent",
  "trace_id": "abc123def456...",
  "span_id": "def456ghi789...",
  "user_id": "user-42",
  "email_address": "student@utsu.ca",
  "module": "mailsvc",
  "function": "send_email"
}
```

**Compatible with:**
- ELK Stack (Elasticsearch, Logstash, Kibana)
- Datadog
- GCP Cloud Logging
- Azure Monitor
- Splunk

### Configuration

Environment variables (same across all clouds):

```bash
# Jaeger configuration
export JAEGER_AGENT_HOST=jaeger-collector.monitoring.svc.cluster.local
export JAEGER_AGENT_PORT=6831

# Logging level
export OTEL_LOG_LEVEL=INFO

# Service metadata
export OTEL_SERVICE_NAMESPACE=utsu-portal
```

### Usage in Your Services

**In authsvc:**
```python
from utsu_portal.observability import setup_observability, instrument_fastapi

app = FastAPI()
setup_observability(service_name="authsvc")
instrument_fastapi(app)
```

**In mailsvc:**
```python
from utsu_portal.observability import setup_observability

setup_observability(service_name="mailsvc")
tracer = get_tracer(__name__)

async def send_email(to: str, subject: str):
    with tracer.start_as_current_span("send_email"):
        # Implementation
```

### Monitoring Dashboards

In production, you'll have:

1. **Service Health Dashboard** - Latency, error rates, throughput per service
2. **Distributed Trace Explorer** - Follow requests through all services
3. **Performance Analysis** - Which operations are slow?
4. **Error Analysis** - Which services are failing and why?

---

## Cloud-Agnostic File Handler

### Overview

The file handler provides a unified interface for file storage that works with:
- **Local filesystem** (development)
- **AWS S3** (and S3-compatible systems like MinIO)
- **Google Cloud Storage** (GCS)
- **Azure Blob Storage**
- **FTP** (legacy systems)

Your application code doesn't care which backend is used—just switch an environment variable.

### Architecture

```
Application Code
    ↓
FileHandler (abstraction layer)
    ↓
┌────────────────────────────────────────────────┐
│ LocalFileStorage  S3  GCS  AzureBlob  FTP      │
│ (file system)                                  │
└────────────────────────────────────────────────┘
    ↓
┌────────────────────────────────────────────────┐
│ Actual storage (S3 bucket, GCS bucket, etc)    │
└────────────────────────────────────────────────┘
```

### Usage

```python
from utsu_portal.filesvc import FileHandler

# Automatic detection from environment
handler = FileHandler.from_env()

# Upload
with open("report.pdf", "rb") as f:
    file_key = handler.upload(f, "reports/2024/fall-semester")
    # Returns: "reports/2024/fall-semester"

# Download
with handler.download(file_key) as f:
    pdf_data = f.read()

# Delete
handler.delete(file_key)

# Get direct access URL (useful for frontend)
signed_url = handler.get_signed_url(file_key, expires_in=3600)
# URL expires in 1 hour
```

### Configuration

**Development (local):**
```bash
export FILE_STORAGE=local
export FILE_STORAGE_PATH=/tmp/utsu-files
```

**AWS S3:**
```bash
export FILE_STORAGE=s3
export AWS_S3_BUCKET=utsu-portal-files-prod
export AWS_S3_REGION=us-east-1
# AWS credentials from environment or IAM role
```

**Google Cloud Storage:**
```bash
export FILE_STORAGE=gcs
export GCS_BUCKET=utsu-portal-files-prod
export GCS_PROJECT=utsu-ca-prod
# Credentials from GOOGLE_APPLICATION_CREDENTIALS or GCP metadata server
```

**Azure Blob Storage:**
```bash
export FILE_STORAGE=azure
export AZURE_CONTAINER=files
export AZURE_CONNECTION_STRING=DefaultEndpointsProtocol=https;...
```

**FTP (legacy):**
```bash
export FILE_STORAGE=ftp
export FTP_HOST=legacy-server.utsu.ca
export FTP_USERNAME=serviceaccount
export FTP_PASSWORD=secret
export FTP_PORT=21
```

### Per-Cloud Deployment

The same code deploys to all clouds without changes:

```
┌──────────────────────────────────────────────────────┐
│ SAME APPLICATION CODE (mailsvc, filesvc, etc)        │
└──────────────────────────────────────────────────────┘
           ↓              ↓              ↓
      Kubernetes      Azure CA      GCP Cloud Run
           ↓              ↓              ↓
   FILE_STORAGE=local FILE_STORAGE=azure FILE_STORAGE=gcs
      Local files     Azure Blob      GCS Bucket
```

### Signed URLs for Direct Access

Instead of proxying large files through your service:

```python
# Give user direct access with expiring link
signed_url = handler.get_signed_url("reports/2024/fall-semester.pdf", expires_in=1800)
# Returns: https://s3.amazonaws.com/bucket/...?Signature=...&Expires=1234567890

# User can download directly from cloud storage without hitting your service
# URL expires in 30 minutes
```

**Benefits:**
- Reduces bandwidth on your services
- Faster downloads (direct from cloud storage)
- Secure (signed URLs can't be forged)
- Audit-trail (cloud storage logs who downloaded what)

---

## Centralized Authentication

### The Problem with Distributed Auth

Without centralization, you get:
- Different auth mechanisms in different services
- Hard to revoke a compromised service
- No audit trail of service-to-service calls
- Difficult to add new services

### The Solution: Centralized Auth under authsvc

**All** authentication happens in authsvc:
- User authentication (login, OTP, password reset)
- Service authentication (service-to-service calls)
- RBAC and permission checks
- Token validation

### Architecture

```
┌─────────────────────────────────────────────┐
│           AUTHSVC (source of truth)         │
│  - Verify user tokens                       │
│  - Issue service tokens                     │
│  - Validate permissions                     │
│  - Log all auth decisions                   │
└─────────────────────────────────────────────┘
    ↑        ↑        ↑        ↑
    │        │        │        │
   Portal  Mailsvc  Filesvc  External
  (users)  (svc-2   (svc-2   APIs
           svc)     svc)
```

### Service-to-Service Authentication

#### 1. Services Register Themselves

At startup, each service registers with authsvc via environment variables:

```bash
# In mailsvc startup
export SERVICE_REGISTRY_JSON='[
  {
    "name": "mailsvc",
    "secret": "random-32-char-secret-key-here",
    "permissions": ["service:send:email"],
    "max_requests_per_minute": 1000
  },
  {
    "name": "filesvc",
    "secret": "another-secret-key",
    "permissions": ["service:read:files", "service:write:files"],
    "max_requests_per_minute": 500
  }
]'
```

#### 2. Service Gets a Token

```python
from utsu_portal.authsvc.service_auth import ServiceAuthModule

auth = ServiceAuthModule()
token = auth.issue_token("mailsvc")
# Returns: JWT token that expires in 1 hour
```

#### 3. Service Uses Token for Service-to-Service Calls

```python
# mailsvc calling filesvc
import httpx

headers = {"Authorization": f"Bearer {token}"}
response = httpx.get(
    "http://filesvc:8080/internal/upload",
    headers=headers,
    files={"file": pdf_data}
)
```

#### 4. Called Service Validates Token

```python
# In filesvc
from utsu_portal.authsvc.service_auth import ServiceAuthModule

auth = ServiceAuthModule()

@app.post("/internal/upload")
async def upload(authorization: str = Header()):
    token = auth.validate_token(authorization.replace("Bearer ", ""))
    
    if "service:write:files" not in token.permissions:
        raise HTTPException(403, "Insufficient permissions")
    
    # mailsvc is authenticated and authorized to upload files
    logger.info(f"Upload authorized for {token.service_name}")
```

### Permission Model for Services

Services have **specific** permissions (not global):

```python
class ServicePermission(str, Enum):
    READ_USER = "service:read:user"
    WRITE_USER = "service:write:user"
    READ_FILES = "service:read:files"
    WRITE_FILES = "service:write:files"
    SEND_EMAIL = "service:send:email"
    READ_AUDIT_LOG = "service:read:audit"
    WRITE_AUDIT_LOG = "service:write:audit"
```

**Example:** mailsvc can only send emails, can't read files:
```json
{
  "name": "mailsvc",
  "permissions": ["service:send:email"]
}
```

### Token Caching and Validation

Tokens are cached in-memory to avoid repeated validation:

```python
# First call: validates with authsvc
token = auth.validate_token(jwt_token, service_name="mailsvc")

# Subsequent calls: uses cache (instant)
token = auth.validate_token(jwt_token, service_name="mailsvc")
```

**Cache invalidation:** Tokens expire naturally (1-hour TTL), or manually revoke:

```python
auth.services["mailsvc"].enabled = False
# Now all tokens from mailsvc are rejected
```

### Multi-Cloud Service Discovery

How do services find each other?

**Kubernetes:**
```python
# Direct DNS: authsvc:8080
response = httpx.get("http://authsvc:8080/service-auth/validate")
```

**Azure Container Apps:**
```python
# DAPR service invocation at localhost:3500
response = httpx.get("http://localhost:3500/v1.0/invoke/authsvc/method/service-auth/validate")
```

**GCP Cloud Run:**
```python
# URL from environment variable
auth_url = os.getenv("AUTHSVC_URL")  # https://authsvc-abc123.run.app
response = httpx.get(f"{auth_url}/service-auth/validate")
```

**AWS Fargate:**
```python
# Internal DNS via ECS service discovery
response = httpx.get("http://authsvc.local:8080/service-auth/validate")
```

---

## Audit Compliance Patterns

### The Problem

Compliance regulations require:
- **GDPR:** Track who accessed personal data, why, and allow data deletion
- **HIPAA:** Audit logs for all medical data access (immutable, 7 years retention)
- **SOC 2:** Demonstrate access controls are enforced and audited

### The Solution: Multi-Layer Audit Logging

```
User Action
    ↓
┌──────────────────────────────────────────────┐
│ 1. Access Control Check (did we allow it?)   │
│    → access_control_log table                │
└──────────────────────────────────────────────┘
    ↓
┌──────────────────────────────────────────────┐
│ 2. Data Change Audit (what changed?)         │
│    → audit_log table                         │
│    - Before/after data                       │
│    - Who did it                              │
│    - When it happened                        │
└──────────────────────────────────────────────┘
    ↓
┌──────────────────────────────────────────────┐
│ 3. Data Lineage (where did it go?)           │
│    → data_lineage table                      │
│    - Was data exported?                      │
│    - Who downloaded it?                      │
│    - Has PII involved?                       │
└──────────────────────────────────────────────┘
```

### Database Tables

#### 1. audit_log - Core Compliance Record

```sql
SELECT
    audit_id,
    timestamp,
    actor_id,          -- user-123 or mailsvc
    actor_type,        -- "user" or "service"
    resource_type,     -- "user" or "file"
    resource_id,       -- what was accessed
    action,            -- "read" or "write"
    result,            -- "success" or "denied"
    reason,            -- why was it denied?
    data_before,       -- original data (JSON)
    data_after         -- modified data (JSON)
FROM audit_log
WHERE resource_type = 'user' AND resource_id = 'user-42';
```

#### 2. access_control_log - Permission Decisions

```sql
SELECT
    check_id,
    timestamp,
    requester_id,      -- who tried?
    resource_type,     -- what did they want?
    action_requested,
    decision,          -- "allow" or "deny"
    policy_matched,    -- which rule blocked them?
    roles,             -- what roles did they have?
    permissions        -- what permissions were checked?
FROM access_control_log
WHERE decision = 'deny' AND timestamp > NOW() - INTERVAL '7 days';
```

#### 3. data_lineage - Track Data Movement

```sql
SELECT
    source_service,    -- mailsvc
    source_resource_id,-- email-456
    dest_service,      -- portal (exported)
    dest_resource_id,  -- spreadsheet-789
    operation,         -- "export"
    pii_involved       -- was personal data exported?
FROM data_lineage
WHERE pii_involved = true AND timestamp > NOW() - INTERVAL '30 days';
```

#### 4. retention_policy - Compliance Rules

```sql
SELECT * FROM retention_policy WHERE resource_type = 'user';

Results:
- retention_days: 2555 (7 years for HIPAA)
- hard_delete_days: 3650 (can't recover after)
- anonymize_after_days: 365 (remove PII after 1 year)
- regulation: HIPAA
```

### Using Audit Logging in Your Code

**Example: Log a user data read**

```python
from utsu_portal.authsvc.audit_compliance import (
    build_audit_entry_read,
    store_audit_log
)

# User tries to read another user's profile
permission_granted = check_permission(user_id, "read:user", target_user_id)

entry = build_audit_entry_read(
    actor_id=user_id,
    actor_type="user",
    resource_type="user",
    resource_id=target_user_id,
    permission_granted=permission_granted,
    permission_required="read:user",
    reason="User profile access" if permission_granted else "Insufficient role",
    actor_ip=request.client.host,
)

# Store in database
await store_audit_log(entry)

# If denied, security team gets alerted
if not permission_granted:
    logger.warning(f"Access denied: {user_id} tried to read {target_user_id}")
```

**Example: Log a data modification**

```python
from utsu_portal.authsvc.audit_compliance import (
    build_audit_entry_write,
    AuditAction
)

# Admin updates a student's record
old_data = {"name": "John Doe", "email": "john@utsu.ca", "status": "active"}
new_data = {"name": "John Doe", "email": "john.doe@utsu.ca", "status": "active"}

entry = build_audit_entry_write(
    actor_id=admin_id,
    actor_type="user",
    resource_type="user",
    resource_id=student_id,
    action=AuditAction.UPDATE,
    data_before=old_data,
    data_after=new_data,
    permission_granted=True,
    actor_ip=request.client.host,
)

await store_audit_log(entry)
```

### Compliance Queries

**"Show me all access to user-42's data in the last 7 days"**

```sql
SELECT
    timestamp,
    actor_id,
    action,
    result
FROM audit_log
WHERE resource_type = 'user'
  AND resource_id = 'user-42'
  AND timestamp > NOW() - INTERVAL '7 days'
ORDER BY timestamp DESC;
```

**"Did anyone deny trying to access restricted files?"**

```sql
SELECT
    timestamp,
    actor_id,
    resource_id,
    reason
FROM audit_log
WHERE action = 'read'
  AND result = 'denied'
  AND resource_type = 'file'
  AND timestamp > NOW() - INTERVAL '24 hours';
```

**"Show me all PII that was exported in the last 30 days"**

```sql
SELECT
    source_service,
    source_resource_id,
    dest_service,
    actor_id,
    timestamp
FROM data_lineage
WHERE pii_involved = true
  AND timestamp > NOW() - INTERVAL '30 days'
ORDER BY timestamp DESC;
```

### Data Retention and Deletion (GDPR Right to be Forgotten)

**Retention Policy:**
- Default: Keep all audit logs for 7 years (HIPAA requirement)
- User PII: Anonymize after 1 year
- Deleted users: Hard delete after 7 years

**Safe Deletion Process:**

```sql
-- Step 1: Find all data for user-42
SELECT audit_id, data_after FROM audit_log WHERE resource_id = 'user-42';

-- Step 2: Anonymize sensitive fields
UPDATE audit_log
SET data_after = jsonb_set(
    data_after,
    '{email}',
    '"[redacted]"'::jsonb
)
WHERE resource_id = 'user-42' AND timestamp < NOW() - INTERVAL '1 year';

-- Step 3: Soft delete (set status to deleted, don't remove)
UPDATE users SET status = 'deleted_gdpr' WHERE id = 'user-42';

-- Step 4: After retention period, hard delete
DELETE FROM audit_log WHERE resource_id = 'user-42' AND retention_days_passed;
```

---

## Architectural Decisions

### Why Centralize Authentication in authsvc?

**Pros:**
- ✅ Single source of truth for all auth decisions
- ✅ Easy to audit (all auth decisions logged in one place)
- ✅ Easy to revoke compromised services
- ✅ Consistent policy across all services
- ✅ Scales horizontally (replicate authsvc)

**Cons:**
- ❌ authsvc becomes critical (must be highly available)
- ❌ Token validation adds ~50ms latency per service-to-service call
- ❌ Token caching mitigates this (cached tokens are instant)

**Mitigation:**
- Run 3+ replicas of authsvc in production
- Cache tokens locally in each service (1-hour TTL)
- Implement circuit breakers (fail open if authsvc is down for <5min)

### Why Not Use Service Mesh (Istio) for Service-to-Service Auth?

Service mesh handles mTLS automatically, but adds complexity:
- **Pros:** Automatic encryption, certificate rotation, no code changes
- **Cons:** Adds 100-200ms latency, requires extra resources (sidecars), steeper learning curve

**Our choice:** JWT tokens are simpler and faster for this use case.

**If you need mutual TLS:** Use Istio on Kubernetes, Azure Service Mesh on Azure, or Google Service Mesh on GCP.

### Why OpenTelemetry for Observability?

- **Vendor-agnostic:** Works with Jaeger, Datadog, GCP Cloud Trace, Azure Application Insights
- **Standard:** CNCF project, adopted by major cloud providers
- **No vendor lock-in:** Switch monitoring platforms without code changes

### Why Multiple File Storage Backends?

- **Dev:** Local filesystem (fast, no cloud costs)
- **Prod Azure:** Azure Blob Storage (integrate with Azure ecosystem)
- **Prod GCP:** GCS (integrate with GCP ecosystem)
- **Prod AWS:** S3 (integrate with AWS ecosystem)
- **Legacy:** FTP (for systems that haven't migrated)

All use the same code—just change `FILE_STORAGE` environment variable.

### Database Retention and Compliance

Why separate tables for audit, access control, and lineage?

1. **Performance:** Audit logs can be archived, but access control must be fast
2. **Compliance:** Different retention requirements (GDPR 30 days, HIPAA 7 years)
3. **Privacy:** Different anonymization rules per table
4. **Queryability:** Each table optimized for its query patterns

---

## Next Steps

### Immediate (1-2 weeks)

- [ ] Deploy Jaeger collector to production (Kubernetes: Helm, Azure: Container Apps, GCP: Run, AWS: ECS)
- [ ] Add observability to all services
- [ ] Configure centralized logging (ELK, Datadog, or Cloud Logging)
- [ ] Add audit logging database (PostgreSQL audit_log tables)

### Short-term (1-2 months)

- [ ] Migrate file uploads to use FileHandler (portal, filesvc)
- [ ] Register all services in SERVICE_REGISTRY_JSON
- [ ] Implement service-to-service token validation
- [ ] Add compliance dashboards (who accessed what, when, why)

### Medium-term (3-6 months)

- [ ] Implement data retention policies (GDPR deletion, HIPAA 7-year retention)
- [ ] Add automated compliance reports
- [ ] Implement secure file sharing (signed URLs with expiration)
- [ ] Set up data lineage for GDPR "right to access" requests

### Long-term (6-12 months)

- [ ] Implement end-to-end encryption for sensitive files
- [ ] Add field-level encryption for sensitive user data
- [ ] Implement advanced threat detection (anomalous access patterns)
- [ ] Get SOC 2 Type II certification with audit logs as evidence

---

## Summary Table

| Component | Current | Next Phase |
|-----------|---------|-----------|
| **Observability** | Added (OpenTelemetry) | Deploy Jaeger, configure ELK |
| **File Handling** | Added (5 backends) | Migrate file uploads to use it |
| **Service Auth** | Added (JWT tokens) | Register all services, enable token validation |
| **Audit Compliance** | Schema defined, builders ready | Implement database tables, add logging calls |

---

## Files Added

| File | Purpose |
|------|---------|
| `src/utsu_portal/observability/__init__.py` | Unified observability stack (Jaeger, Prometheus, JSON logging) |
| `src/utsu_portal/filesvc/__init__.py` | Cloud-agnostic file handler (S3, GCS, Azure, FTP, local) |
| `src/utsu_portal/authsvc/service_auth.py` | Service-to-service authentication (JWT tokens, rate limiting) |
| `src/utsu_portal/authsvc/audit_compliance.py` | Audit logging schema and compliance query builders |

