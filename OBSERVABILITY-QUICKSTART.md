# Quick Start: Unified Observability, File Handling, and Audit Compliance

This guide walks through setting up the three new production-grade systems.

## Table of Contents

1. [Local Development Setup](#local-development-setup)
2. [Multi-Cloud Configuration](#multi-cloud-configuration)
3. [Using the Systems](#using-the-systems)
4. [Deployment Checklist](#deployment-checklist)

---

## Local Development Setup

### 1. Start Observability Stack

```bash
# Start all services including Jaeger, Prometheus, Grafana, ELK stack, and audit DB
docker-compose -f docker-compose-observability.yml up -d

# Verify services are running
docker ps | grep -E "jaeger|prometheus|grafana|elasticsearch|kibana|audit-db"
```

### 2. Access Monitoring Dashboards

- **Jaeger UI (Distributed Tracing):** http://localhost:16686
  - Shows full request traces across all services
  - Useful for: Finding slow services, tracing errors

- **Prometheus UI (Metrics):** http://localhost:9090
  - Raw metrics and alerts
  - Useful for: Checking specific metrics, writing queries

- **Grafana (Dashboards):** http://localhost:3000
  - Pre-built dashboards for service health
  - Username: `admin`, Password: `admin`
  - Useful for: Overview dashboards, alert thresholds

- **Kibana (Log Search):** http://localhost:5601
  - Centralized log search
  - Useful for: Finding logs by trace ID, searching for errors

- **pgAdmin (Database Admin):** http://localhost:5050
  - PostgreSQL admin UI for audit logs
  - Username: `admin@utsu.ca`, Password: `admin`
  - Useful for: Querying audit logs, compliance reports

### 3. Install Python Dependencies

```bash
pip install -e ".[observability,filesvc,authsvc,portal,sql]"
```

**What each extra installs:**
- `observability`: OpenTelemetry, Jaeger, Prometheus
- `filesvc`: AWS SDK, Google Cloud SDK, Azure SDK
- `authsvc`: JWT library
- `portal`: FastAPI, SQLAlchemy
- `sql`: Database drivers

---

## Multi-Cloud Configuration

### AWS (S3 for File Storage)

```bash
# Set environment variables
export FILE_STORAGE=s3
export AWS_S3_BUCKET=utsu-portal-files-prod
export AWS_S3_REGION=us-east-1
# AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are usually set by IAM role

# Or using MinIO (S3-compatible):
export AWS_S3_ENDPOINT=https://minio.example.com
```

### Google Cloud (GCS for File Storage)

```bash
# Set environment variables
export FILE_STORAGE=gcs
export GCS_BUCKET=utsu-portal-files-prod
export GCS_PROJECT=utsu-ca-prod

# Set up authentication
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account-key.json
```

### Azure (Blob Storage for File Storage)

```bash
# Set environment variables
export FILE_STORAGE=azure
export AZURE_CONTAINER=files
export AZURE_CONNECTION_STRING=DefaultEndpointsProtocol=https;AccountName=...;AccountKey=...
```

### Jaeger on Each Cloud

**Kubernetes:**
```bash
# Jaeger is already discoverable via DNS
export JAEGER_AGENT_HOST=jaeger.monitoring.svc.cluster.local
export JAEGER_AGENT_PORT=6831
```

**Azure Container Apps:**
```bash
# Use Container Apps HTTP service invocation
export JAEGER_AGENT_HOST=localhost
export JAEGER_AGENT_PORT=6831
# Configure DAPR sidecar to route to jaeger service
```

**GCP Cloud Run:**
```bash
# Deploy Jaeger as a Cloud Run service or GKE pod
export JAEGER_AGENT_HOST=jaeger-collector-abc123.run.app
export JAEGER_AGENT_PORT=6831
```

**AWS Fargate:**
```bash
# Deploy Jaeger in ECS or use AWS X-Ray instead
export JAEGER_AGENT_HOST=jaeger.internal.utsu.ca
export JAEGER_AGENT_PORT=6831
```

---

## Using the Systems

### 1. Observability in Your Service

```python
from fastapi import FastAPI
from utsu_portal.observability import (
    setup_observability,
    get_tracer,
    get_logger,
    instrument_fastapi,
)

# Initialize
setup_observability(service_name="mailsvc")
app = FastAPI()
instrument_fastapi(app)

# Use in endpoints
tracer = get_tracer(__name__)
logger = get_logger(__name__)

@app.post("/send-email")
async def send_email(email: str):
    with tracer.start_as_current_span("send_email"):
        logger.info(f"Sending email to {email}")
        # Implementation
        return {"status": "sent"}
```

**Check Jaeger:**
1. Open http://localhost:16686
2. Select "mailsvc" from Service dropdown
3. Click "Find Traces"
4. Click on any trace to see full request flow

### 2. File Storage in Your Service

```python
from utsu_portal.filesvc import FileHandler

# Auto-detects backend from FILE_STORAGE environment variable
handler = FileHandler.from_env()

# Upload
with open("report.pdf", "rb") as f:
    file_key = handler.upload(f, "reports/2024/fall")

# Get signed URL for direct access
url = handler.get_signed_url(file_key, expires_in=3600)

# Download
with handler.download(file_key) as f:
    content = f.read()

# Delete
handler.delete(file_key)
```

### 3. Service-to-Service Authentication

**In authsvc (central auth service):**

```python
from utsu_portal.authsvc.service_auth import ServiceAuthModule, ServicePermission

auth = ServiceAuthModule()

# Issue token to mailsvc
token = auth.issue_token("mailsvc")
print(f"Token: {token}")
```

**In mailsvc (calling another service):**

```python
import httpx
from utsu_portal.authsvc.service_auth import ServiceAuthModule

auth = ServiceAuthModule()
token = auth.issue_token("mailsvc")

# Call filesvc with service token
async def upload_to_filesvc(file_data):
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://filesvc:8080/api/upload",
            content=file_data,
            headers=headers,
        )
        return response.json()
```

**In filesvc (validating token):**

```python
from fastapi import Header, HTTPException
from utsu_portal.authsvc.service_auth import ServiceAuthModule, ServicePermission

auth = ServiceAuthModule()

@app.post("/api/upload")
async def upload(authorization: str = Header()):
    token = auth.validate_token(authorization.replace("Bearer ", ""))
    
    if ServicePermission.WRITE_FILES not in token.permissions:
        raise HTTPException(403, "Insufficient permissions")
    
    # mailsvc is authenticated and authorized
    return {"status": "uploaded"}
```

### 4. Audit Logging for Compliance

```python
from utsu_portal.authsvc.audit_compliance import (
    build_audit_entry_read,
    build_audit_entry_write,
    AuditAction,
)

# Log a read
audit_entry = build_audit_entry_read(
    actor_id="user-123",
    actor_type="user",
    resource_type="user",
    resource_id="user-456",
    permission_granted=True,
    actor_ip="192.168.1.1",
)
# await store_audit_log(audit_entry)

# Log a write
audit_entry = build_audit_entry_write(
    actor_id="admin-789",
    actor_type="user",
    resource_type="user",
    resource_id="user-456",
    action=AuditAction.UPDATE,
    data_before={"email": "old@utsu.ca"},
    data_after={"email": "new@utsu.ca"},
)
# await store_audit_log(audit_entry)
```

**Query audit logs:**

```python
import psycopg
from utsu_portal.authsvc.audit_compliance import COMPLIANCE_QUERIES

async with await psycopg.AsyncConnection.connect("postgresql://...") as conn:
    # Get all access to user-123's data
    result = await conn.execute(
        COMPLIANCE_QUERIES["user_access_history"],
        ("user-123", "2024-01-01")
    )
    for row in result:
        print(f"{row['timestamp']}: {row['action']} on {row['resource_type']}")
```

---

## Deployment Checklist

### Before Production Deployment

- [ ] **Observability:**
  - [ ] Deploy Jaeger to production cluster
  - [ ] Configure Prometheus scraping
  - [ ] Set up Grafana dashboards
  - [ ] Configure alerting rules
  - [ ] Test distributed tracing across all services

- [ ] **File Handling:**
  - [ ] Configure cloud storage credentials (AWS IAM, GCP service account, Azure MSI)
  - [ ] Create storage buckets/containers
  - [ ] Set up lifecycle policies (auto-delete old files)
  - [ ] Enable versioning for audit trail
  - [ ] Test uploads/downloads on each cloud

- [ ] **Service Authentication:**
  - [ ] Generate secrets for each service (32+ characters)
  - [ ] Store secrets in Vault/Key Management System
  - [ ] Configure SERVICE_REGISTRY_JSON
  - [ ] Test token validation
  - [ ] Set up token rotation cron job

- [ ] **Audit Compliance:**
  - [ ] Create audit database schema
  - [ ] Configure retention policies per regulation
  - [ ] Test audit logging in each service
  - [ ] Verify immutability of audit logs
  - [ ] Set up compliance report generation
  - [ ] Test GDPR right-to-delete process

### Production Configuration

```bash
# Kubernetes secrets
kubectl create secret generic utsu-observability \
  --from-literal=JAEGER_AGENT_HOST=jaeger.monitoring.svc.cluster.local \
  --from-literal=JAEGER_AGENT_PORT=6831

kubectl create secret generic utsu-filesvc \
  --from-literal=FILE_STORAGE=s3 \
  --from-literal=AWS_S3_BUCKET=utsu-prod-files

kubectl create secret generic utsu-authsvc \
  --from-literal=SERVICE_REGISTRY_JSON='...'

# Or environment file for Azure/AWS
export FILE_STORAGE=s3
export AWS_S3_BUCKET=utsu-prod-files
export JAEGER_AGENT_HOST=jaeger.monitoring.svc.cluster.local
```

### Monitoring Health

```bash
# Check Jaeger is receiving traces
curl http://jaeger:16686/api/services

# Check Prometheus is scraping metrics
curl http://prometheus:9090/api/v1/targets

# Check audit logs are being written
psql audit_logs -U audit_service -c "SELECT COUNT(*) FROM audit.audit_log WHERE timestamp > NOW() - INTERVAL '1 hour';"
```

---

## Troubleshooting

### Jaeger traces not showing up

```bash
# Check Jaeger is running
docker logs $(docker ps -q -f "ancestor=jaegertracing/all-in-one")

# Check service can reach Jaeger agent
curl http://localhost:6831/

# Check service is initialized with observability
# Add: logger.info(f"Jaeger configured: {os.getenv('JAEGER_AGENT_HOST')}")
```

### File uploads failing

```bash
# For S3: check credentials
aws s3 ls s3://utsu-prod-files/

# For GCS: check credentials
gcloud auth list
gsutil ls gs://utsu-prod-files/

# For Azure: check connection string
az storage account list
```

### Service-to-service calls failing

```bash
# Check service token is issued
python -c "from utsu_portal.authsvc.service_auth import ServiceAuthModule; auth = ServiceAuthModule(); print(auth.issue_token('mailsvc'))"

# Check service is registered
python -c "from utsu_portal.authsvc.service_auth import ServiceAuthModule; auth = ServiceAuthModule(); print(list(auth.services.keys()))"

# Check token can be validated
token = auth.issue_token('mailsvc')
auth.validate_token(token)
```

### Audit logs not being written

```bash
# Check audit database is running
psql -h localhost -p 5433 -U audit_service -d audit_logs -c "SELECT COUNT(*) FROM audit.audit_log;"

# Check connection string in code
# print(os.getenv('AUDIT_DB_URL'))
```

---

## Performance Expectations

| Component | Latency | Notes |
|-----------|---------|-------|
| Jaeger tracing | +5-10ms per request | Minimal overhead, auto-batched |
| Prometheus metrics | +1-2ms | Asynchronous collection |
| Audit logging | +10-20ms | Depends on database distance |
| S3 file upload | +500-2000ms | Network-dependent |
| GCS file upload | +400-1500ms | Network-dependent |
| Azure file upload | +400-1500ms | Network-dependent |
| Service token validation (cached) | <1ms | After first validation |
| Service token validation (uncached) | +50-100ms | Calls authsvc |

---

## Next Steps

1. **Try Local Setup:** Run `docker-compose -f docker-compose-observability.yml up` and test tracing
2. **Add Observability to Portal:** See `wiki/Example-Integration.py`
3. **Configure File Storage:** Test uploads to each cloud
4. **Set Up Service Auth:** Register mailsvc and filesvc
5. **Enable Audit Logging:** Add audit table writes to each service
6. **Deploy to Production:** Follow [Production Deployment Checklist](#before-production-deployment)

