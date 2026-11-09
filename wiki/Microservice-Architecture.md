# Microservice Architecture Guide: Email Service & RBAC

## Overview

This document explains how the microservice architecture works, the role of the reverse proxy, and the communication patterns between services.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        Internet / Users                          │
└────────────────────────────┬────────────────────────────────────┘
                             │ HTTPS
                             ↓
┌─────────────────────────────────────────────────────────────────┐
│              NGINX Ingress Controller (Reverse Proxy)            │
│  - Routes requests to appropriate microservices                 │
│  - Terminates TLS/SSL                                           │
│  - Rate limiting & load balancing                               │
│  - Request/response transformation                              │
│  - Caching & compression                                        │
└────────┬──────────────────┬─────────────────┬──────────────────┘
         │                  │                 │
    ┌────↓────┐         ┌───↓─────┐     ┌────↓────┐
    │ Portal  │         │ AuthSvc │     │ MailSvc │
    │ Service │         │         │     │         │
    └────┬────┘         └────┬────┘     └────┬────┘
         │                   │              │
         └─────────────────┬─┼──────────────┘
                           │ │
                    Service-to-Service Communication
                    (service token auth)
                           │ │
         ┌─────────────────┴─┴──────────────┐
         │                                  │
         ↓                                  ↓
    ┌──────────────┐                 ┌──────────────┐
    │  PostgreSQL  │                 │  PostgreSQL  │
    │  (accounts,  │                 │  (emails,    │
    │   sessions)  │                 │   queue)     │
    └──────────────┘                 └──────────────┘
         │
         ↓
    ┌──────────────────┐
    │ Redis            │
    │ (sessions, cache)│
    └──────────────────┘
```

## Core Components

### 1. **NGINX Ingress Controller (Reverse Proxy)**

The reverse proxy is **essential** for a production microservice architecture. It acts as:

#### **Functions:**
- **Routing**: Directs client requests to the correct service based on URL paths
- **TLS Termination**: Handles SSL/TLS encryption/decryption once, reducing overhead per service
- **Load Balancing**: Distributes traffic across multiple service replicas
- **Rate Limiting**: Prevents abuse by limiting requests per IP/user
- **Request Transformation**: Modifies headers, adds authentication headers
- **Health Checks**: Routes only to healthy service instances
- **Circuit Breaking**: Prevents cascading failures when a service is down

#### **Why You Need It:**
```
Without reverse proxy:
  Client → Service1:8080, Service2:8080, Service3:8080 (clients manage routing)

With reverse proxy:
  Client → Reverse Proxy (portal.example.org) → Services internally
  - Clients only need one endpoint
  - Services can be replaced/scaled transparently
  - Single point for security policies
```

### 2. **Auth Service (authsvc)**

**Responsibilities:**
- User authentication (login, SSO, OTP)
- Identity verification
- Session management (signs JWT tokens)
- Account management
- **NEW: Role-Based Access Control (RBAC)**

**Key Features:**
- Runs independently with its own database
- Issues Ed25519-signed JWT tokens that other services can verify offline
- `/internal/*` endpoints protected by service token (only accessible from other services)
- Maintains audit log of all access

**Deployment:**
```bash
# Start auth service
uvicorn utsu_portal.authsvc:create_auth_app --host 0.0.0.0 --port 8080
```

### 3. **Email Service (mailsvc)** ← NEW

**Responsibilities:**
- Queue emails for sending
- Track delivery status
- Render email templates
- Retry failed emails with exponential backoff
- Provide email status API

**Key Features:**
- **Asynchronous**: Returns immediately, sends in background
- **Retryable**: Failed emails are retried up to 3 times
- **Traceable**: Every email has an ID for tracking
- **Template Support**: Jinja2 templates with variable substitution
- **Priority-based**: High-priority emails sent immediately

**Deployment:**
```bash
# Start email service
uvicorn utsu_portal.mailsvc:create_mail_app --host 0.0.0.0 --port 8080
```

### 4. **Portal Service (portal)**

The main web application that:
- Serves the UI
- Calls authsvc for authentication
- Calls mailsvc to send emails
- Routes requests through the reverse proxy

## Communication Patterns

### Pattern 1: Client → Reverse Proxy → Service

```
User Browser:
  POST https://portal.example.org/api/auth/login
  
NGINX Ingress:
  1. Receives HTTPS request
  2. Decrypts TLS (uses certificate)
  3. Examines path (/api/auth)
  4. Routes to: authsvc:8080 (internal Kubernetes DNS)
  5. Proxies request as HTTP (faster, no TLS overhead internally)
  6. Receives response from authsvc
  7. Encrypts with TLS
  8. Returns to user browser
```

### Pattern 2: Service → Service (Portal → AuthSvc)

```python
# Portal needs to verify a user
response = httpx.get(
    "http://authsvc:8080/internal/accounts/user-123",
    headers={"Authorization": f"Bearer {SERVICE_TOKEN}"}
)
```

**Why internal communication is different:**
- Uses **internal Kubernetes DNS** (authsvc:8080 instead of portal.example.org)
- **No TLS** (optional, internal traffic is secured by NetworkPolicy)
- Uses **service tokens** (HMAC-signed, different from user JWTs)
- Reverse proxy **doesn't intercept** (it only intercepts external traffic)

### Pattern 3: Service → Service (AuthSvc → MailSvc)

```python
# AuthSvc sends welcome email after signup
response = httpx.post(
    "http://mailsvc:8080/mail/queue",
    json={
        "to": "user@example.com",
        "template_id": "welcome",
        "template_vars": {"first_name": "Alice"}
    },
    headers={"Authorization": f"Bearer {SERVICE_TOKEN}"}
)
# Response: {"email_id": "abc123", "status": "pending", "scheduled_at": "2024..."}
```

## RBAC Implementation

The auth service now includes **Role-Based Access Control**:

```python
# Roles are defined in rbac.py
{
    "student": ["agm:view", "forms:create"],
    "admin": ["*"],  # all permissions
    "moderator": ["admin:view_audit", "agm:edit", "agm:verify"],
    "staff": ["admin:view_users", "forms:edit", "admin:export_data"]
}
```

### User Permission Check Flow

```
1. User makes request with JWT token
2. Service decodes JWT (offline, no network call needed)
3. Service gets user ID from JWT
4. Service calls authsvc: GET /rbac/users/{uid}/permissions
5. AuthSvc returns: {"permissions": ["agm:view", "forms:create"]}
6. Service checks if required permission is in list
7. Allows/denies request

# Or use dependency injection for automatic checks:
@app.get("/api/admin/audit")
def view_audit(
    uid: str = Depends(require_permission(Permission.ADMIN_VIEW_AUDIT))
):
    return audit_logs()
```

### Database Schema for RBAC

```sql
-- accounts table (modified)
CREATE TABLE accounts (
    uuid VARCHAR(36) PRIMARY KEY,
    email VARCHAR(255) NOT NULL UNIQUE,
    status VARCHAR(20) NOT NULL,
    email_verified BOOLEAN DEFAULT FALSE,
    roles JSON DEFAULT '[]'  -- NEW: ["admin", "moderator"]
);

-- Role assignments are audited
CREATE TABLE audit_log (
    id BIGINT PRIMARY KEY,
    timestamp DATETIME,
    requester_id VARCHAR(36),
    action VARCHAR(255),
    resource VARCHAR(255),
    details JSON
);
-- Example: 
-- {"action": "role_assigned", "resource": "user:123", "role": "admin"}
-- {"action": "permission_check_denied", "resource": "/api/admin", "user": "456"}
```

## Service Token Authentication

**What is a service token?**
A long, cryptographically secure string shared between services for API authentication.

```python
# Environment: PORTAL_SERVICE_TOKEN=your-secret-token-here (32+ chars)

# Portal → AuthSvc
headers = {"Authorization": f"Bearer {os.environ['PORTAL_SERVICE_TOKEN']}"}

# AuthSvc validates using HMAC constant-time comparison
got = (authorization or "").removeprefix("Bearer ").strip()
if not hmac.compare_digest(got.encode(), want.encode()):
    raise HTTPException(status_code=403)  # Rejected
```

**Why not use JWT for service-to-service?**
- JWTs have expiration times (need refresh logic)
- JWTs are user-specific
- Service tokens are simpler, long-lived, and symmetric

## Scaling the Architecture

### Horizontal Scaling

```yaml
# Deploy multiple replicas
authsvc:
  replicas: 3  # 3 instances for high availability
mailsvc:
  replicas: 2  # 2 instances

# NGINX automatically load-balances across replicas
# Kubernetes DNS (authsvc:8080) returns all 3 IPs
# Clients rotate through them
```

### Vertical Scaling

```yaml
resources:
  requests:
    cpu: 200m      # Reserved
    memory: 512Mi
  limits:
    cpu: 1000m     # Maximum
    memory: 1Gi
```

### Database Scaling

```
Single PostgreSQL → Replication → Sharding
  - Use connection pooling (pgBouncer)
  - Separate read replicas
  - Eventually: shard by user_id for massive scale
```

## Failure Scenarios & Recovery

### Scenario 1: MailSvc is down

```
Portal tries to send email → Connection refused
↓
With retry logic (exponential backoff)
↓
MailSvc comes back up, queue drains, emails sent
↓
No data loss (emails were queued in DB)
```

### Scenario 2: AuthSvc crashes

```
Portal: "I need to check permissions" → Connection refused
↓
If cached token info: serve from cache
↓
If no cache: fail-open (deny access) or fail-closed (allow access)
↓
Kubernetes auto-restarts authsvc pod
↓
Requests succeed again
```

### Scenario 3: Database connection pool exhausted

```
Portal: "I need data" → Max connections reached
↓
New requests queue or timeout
↓
Old connections close (60s timeout)
↓
New connections become available
```

## Security: NetworkPolicy Isolation

```yaml
# Pods can only talk to:
# 1. Ingress controller (external traffic)
# 2. Other pods in same namespace (via DNS)
# 3. DNS (port 53)
# 4. External SMTP (ports 25, 465, 587)

# Prevents:
# - Rogue processes accessing unauthorized services
# - Data exfiltration to unknown destinations
# - Lateral movement of compromise
```

## Observability & Debugging

### View Service Logs

```bash
kubectl logs -f deployment/authsvc -n utsu-portal
kubectl logs -f deployment/mailsvc -n utsu-portal
```

### Test Internal Communication

```bash
# From portal pod, test authsvc
kubectl exec -it pod/portal-xxx -n utsu-portal -- \
  curl -H "Authorization: Bearer $PORTAL_SERVICE_TOKEN" \
  http://authsvc:8080/internal/ping

# Should return {"ok": true}
```

### Monitor Email Queue

```bash
curl -H "Authorization: Bearer $SERVICE_TOKEN" \
  http://portal.example.org/mail/pending

# Returns pending emails waiting to be sent
```

## Testing & Development

### Local Development (no Kubernetes)

```bash
# Terminal 1: Auth service
uvicorn utsu_portal.authsvc:create_auth_app --reload --port 8001

# Terminal 2: Mail service
uvicorn utsu_portal.mailsvc:create_mail_app --reload --port 8002

# Terminal 3: Portal service
uvicorn utsu_portal.app:app --reload --port 8000

# Test auth → mail communication
curl -X POST http://localhost:8002/mail/queue \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-test-token" \
  -d '{"to": "test@example.com", "subject": "Test", "body": "Hello"}'
```

### Docker Compose (simulates K8s)

```yaml
version: '3.8'
services:
  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf
    depends_on:
      - authsvc
      - mailsvc
      - portal

  authsvc:
    build: .
    command: uvicorn utsu_portal.authsvc:create_auth_app --host 0.0.0.0
    environment:
      PORTAL_SERVICE_TOKEN: "test-token-32-chars-minimum---"
    
  mailsvc:
    build: .
    command: uvicorn utsu_portal.mailsvc:create_mail_app --host 0.0.0.0
    environment:
      PORTAL_SERVICE_TOKEN: "test-token-32-chars-minimum---"
      PORTAL_SMTP_URL: "smtp://localhost:1025"

  portal:
    build: .
    command: uvicorn utsu_portal.app:app --host 0.0.0.0
    environment:
      PORTAL_SERVICE_TOKEN: "test-token-32-chars-minimum---"
```

## Summary: Do You Need a Reverse Proxy?

| Scenario | Need Reverse Proxy? | Why |
|----------|-------------------|-----|
| **Local dev** | No | Direct service access fine |
| **Docker Compose** | Optional | Simplifies multi-service routing |
| **Kubernetes** | **YES** | Mandatory for external routing, load balancing, TLS |
| **Production** | **YES** | Essential for security, scalability, observability |
| **Multiple endpoints** | **YES** | Clients shouldn't manage service discovery |

---

**Next Steps:**
1. Deploy authsvc with RBAC module
2. Deploy mailsvc alongside authsvc
3. Configure NGINX Ingress as reverse proxy
4. Test inter-service communication
5. Monitor with Prometheus + Grafana
