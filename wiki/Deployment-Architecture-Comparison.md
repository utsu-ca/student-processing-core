# Deployment Architecture Comparison: Visual Guide

## Quick Reference Matrix

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                   DEPLOYMENT PLATFORM COMPARISON                                       │
├─────────────────────┬──────────────────────┬──────────────────────┬────────────────────┤
│ FEATURE             │ KUBERNETES           │ AZURE Container Apps │ GCP Cloud Run      │
├─────────────────────┼──────────────────────┼──────────────────────┼────────────────────┤
│ Reverse Proxy       │ NGINX Ingress        │ Built-in (Azure LB)  │ Cloud LB            │
│ Service Discovery   │ Cluster DNS          │ DAPR sidecar         │ Manual (URLs)       │
│ Auto-scaling        │ HPA (15-30s)         │ Native (concurrent)  │ Native (< 1s)      │
│ Cold Start          │ None (warm)          │ ~500ms               │ ~100-500ms         │
│ Networking          │ Pod CIDR blocks      │ VNET/NSG             │ VPC/Firewall       │
│ Cost per month      │ $310+ (3 nodes)      │ $160 (2 replicas)    │ $90 (serverless)   │
│ Complexity          │ ⭐⭐⭐⭐⭐          │ ⭐⭐⭐               │ ⭐⭐⭐              │
│ Multi-cloud ready   │ ✅ Yes               │ ❌ Azure only        │ ❌ GCP only         │
│ Startup time        │ Immediate            │ 30-60 seconds        │ 10-30 seconds      │
│ Secrets management  │ ETCD                 │ Key Vault            │ Secret Manager     │
│ Monitoring          │ Prometheus/Grafana   │ Application Insights │ Cloud Monitoring   │
│ Logging             │ ELK/Loki             │ Log Analytics        │ Cloud Logging      │
└─────────────────────┴──────────────────────┴──────────────────────┴────────────────────┘
```

---

## Architecture Overview by Platform

### Kubernetes (AKS / GKE / EKS)

```
                     Internet
                        │
        ┌───────────────┼───────────────┐
        │               │               │
        ↓               ↓               ↓
    ┌────────────────────────────────────────┐
    │       Ingress Controller (NGINX)       │
    │  ┌────────────────────────────────────┐│
    │  │ TLS Termination                   ││
    │  │ Path-based routing                ││
    │  │ Rate limiting & WAF                ││
    │  │ Load balancing                    ││
    │  └────────────────────────────────────┘│
    └────────────────────────────────────────┘
                     │
        ┌────────────┼────────────┐
        ↓            ↓            ↓
    ┌────────┐  ┌────────┐  ┌────────┐
    │Portal  │  │AuthSvc │  │MailSvc│
    │Pod ×3  │  │Pod ×2  │  │Pod ×2 │
    └────────┘  └────────┘  └────────┘
        │            │            │
        └────────────┼────────────┘
                     │
           ┌─────────┴─────────┐
           ↓                   ↓
        ┌──────────┐       ┌──────────┐
        │PostgreSQL│       │ Redis    │
        │ (RDS)    │       │ Cache    │
        └──────────┘       └──────────┘

Service Discovery: DNS (authsvc.default.svc.cluster.local:8080)
Inter-service auth: Mutual TLS (via service mesh like Istio)
Reverse Proxy: Required (NGINX Ingress)
```

**Pros:**
- ✅ Portable across cloud providers
- ✅ Most powerful (Kubernetes features)
- ✅ Largest ecosystem
- ✅ True multi-cloud support

**Cons:**
- ❌ Operational overhead
- ❌ Requires DevOps expertise
- ❌ High baseline cost
- ❌ Steep learning curve

---

### Azure Container Apps

```
                     Internet
                        │
        ┌───────────────────────────────┐
        │ Azure Load Balancer            │
        │ (SSL/TLS termination)          │
        │ (DDoS protection)              │
        └───────────────────────────────┘
                     │
    ┌────────────────────────────────────────┐
    │   Container Apps Environment           │
    │                                        │
    │  ┌─────────────┐ ┌──────────────────┐ │
    │  │ Portal      │ │ AuthSvc          │ │
    │  │ Container   │ │ Container        │ │
    │  │ ×2 replicas │ │ ×2 replicas      │ │
    │  └─────────────┘ └──────────────────┘ │
    │                                        │
    │  ┌──────────────────────────────────┐ │
    │  │ MailSvc Container                 │ │
    │  │ ×1-3 replicas (auto-scale)       │ │
    │  └──────────────────────────────────┘ │
    │                                        │
    │  ┌──────────────────────────────────┐ │
    │  │ DAPR Sidecars (each container)    │ │
    │  │ - Service invocation               │ │
    │  │ - Pub/Sub messaging                │ │
    │  │ - State management                 │ │
    │  │ - Secrets management               │ │
    │  │ - mTLS between services           │ │
    │  └──────────────────────────────────┘ │
    └────────────────────────────────────────┘
                     │
        ┌────────────┼────────────┐
        ↓            ↓            ↓
    ┌──────────┐  ┌────────┐  ┌─────────┐
    │PostgreSQL│  │Redis   │  │Key Vault│
    │(Azure DB)│  │(Cache) │  │(Secrets)│
    └──────────┘  └────────┘  └─────────┘

Service Discovery: DAPR service invocation (http://authsvc:3500/v1.0/invoke/mailsvc/method/...)
Inter-service auth: DAPR mTLS (automatic)
Reverse Proxy: Built-in (Azure Load Balancer)
```

**Pros:**
- ✅ Serverless (no node management)
- ✅ DAPR integration (built-in patterns)
- ✅ Automatic mTLS
- ✅ Azure Key Vault integration
- ✅ Azure-native monitoring

**Cons:**
- ❌ Azure-only (vendor lock-in)
- ❌ Cold starts (~500ms)
- ❌ DAPR adds complexity
- ❌ Limited to Azure SDK tools

---

### GCP Cloud Run

```
                     Internet
                        │
        ┌───────────────────────────────┐
        │ Google Cloud Load Balancer     │
        │ - CDN integration              │
        │ - DDoS protection              │
        │ - SSL/TLS termination          │
        │ - Path-based routing           │
        └───────────────────────────────┘
                     │
        ┌────────────┼────────────┐
        │            │            │
        ↓            ↓            ↓
    ┌─────────┐ ┌─────────┐  ┌─────────┐
    │Portal   │ │AuthSvc  │  │MailSvc │
    │Service  │ │Service  │  │Service  │
    │(public) │ │(private)│  │(private)│
    │1-20 rev │ │0-10 rev │  │0-5 rev  │
    └─────────┘ └─────────┘  └─────────┘
        │            │            │
        │ Service-to-Service Auth │
        │ (Google Cloud IAM)       │
        │                          │
        └──────────┬───────────────┘
                   │
        ┌──────────┼──────────┐
        ↓          ↓          ↓
    ┌─────────┐ ┌────────┐ ┌─────────────┐
    │Cloud SQL│ │Memstore│ │Secret Manager
    │         │ │(Redis) │ │(Secrets)    │
    └─────────┘ └────────┘ └─────────────┘

Service Discovery: Manual (URLs: https://authsvc-abc123.run.app)
Inter-service auth: Google Cloud IAM service accounts
Reverse Proxy: Built-in (Cloud Load Balancer)
Networking: VPC connector for private databases
```

**Pros:**
- ✅ Truly serverless (scales to zero)
- ✅ Fast deployment (from source or container)
- ✅ Cheapest execution model
- ✅ Best for variable workloads
- ✅ Native GCP integrations

**Cons:**
- ❌ GCP-only (vendor lock-in)
- ❌ Cold starts (~100-500ms)
- ❌ Manual service discovery
- ❌ Complex inter-service auth

---

## Request Flow Comparison

### Scenario: User Signs Up (AuthSvc → MailSvc)

#### Kubernetes Path

```
1. Browser: POST /api/auth/signup → NGINX Ingress
2. NGINX: Route to authsvc:8080 (Kubernetes DNS resolves to Pod IP)
3. AuthSvc: Create account, emit "user.created" event
4. AuthSvc: Direct HTTP call to http://mailsvc:8080/mail/queue
   (AuthSvc finds mailsvc via cluster DNS)
5. MailSvc: Queue email in database
6. MailSvc: Background job sends via SMTP
7. Return: 200 OK (email queued)

Latency: ~200-500ms
Failures: Pod crash → automatic restart
```

#### Azure Container Apps Path

```
1. Browser: POST /api/auth/signup → Azure Load Balancer
2. ALB: Route to authsvc container (load balanced)
3. AuthSvc: Create account
4. AuthSvc: DAPR call to http://localhost:3500/v1.0/invoke/mailsvc/method/mail/queue
   (DAPR sidecar in same pod routes to mailsvc container via DAPR)
5. DAPR: Adds mTLS, retries, traces
6. MailSvc: Queue email
7. MailSvc: Background task sends email
8. Return: 200 OK

Latency: ~300-600ms (DAPR adds overhead)
Failures: Container restart (handled by Container Apps)
Retries: Built-in via DAPR
```

#### GCP Cloud Run Path

```
1. Browser: POST /api/auth/signup → Cloud Load Balancer
2. CLB: Route to portal-abc123.run.app
3. AuthSvc: Create account
4. AuthSvc: HTTP call to https://mailsvc-xyz789.run.app/mail/queue
   (Direct HTTPS URL, not via LB)
5. AuthSvc: Adds Google Cloud ID token in Authorization header
6. Cloud Run: Validates IAM permission (authsvc → mailsvc)
7. MailSvc: Queue email
8. MailSvc: Background task sends email
9. Return: 200 OK

Latency: ~400-800ms (HTTPS + IAM auth adds overhead)
Failures: Container revision crash → automatic restart
Scaling: MailSvc scales from 0 to N instances (cold start ~100ms)
```

---

## Cost Analysis (Example Setup)

### Monthly Cost Estimate: 1M users, 10M API calls/month

#### Kubernetes (AKS 3-node cluster)

```
Compute (3 × Standard_B2s):     $150/month
Disk (100GB):                    $10/month
Load Balancer:                   $20/month
Public IP:                       $5/month
Bandwidth (egress):              $15/month
──────────────────────────────────────────
Total: ~$200/month (+ DevOps overhead)
```

#### Azure Container Apps

```
Portal (2 replicas, 1 vCPU):    $80/month
AuthSvc (2 replicas, 0.5 vCPU): $60/month
MailSvc (1-3 replicas, 0.5 vCPU): $20/month
Data transfer:                   $5/month
──────────────────────────────────────────
Total: ~$165/month
```

#### GCP Cloud Run

```
Portal: 2GB × 100M reqs × $0.00001667 = $330
        + requests (1M × $0.40/M) = $0.40 = ~$330/month
AuthSvc: 1GB × 50M reqs = ~$80/month
MailSvc: 512MB × 20M reqs = ~$20/month
──────────────────────────────────────────
Total: ~$430/month (but scales to $0 with no traffic)
```

#### AWS Lambda (for reference)

```
Portal: 2GB × 100M × $0.0000166667 = $3,333
AuthSvc: 1GB × 50M = ~$833
MailSvc: 512MB × 20M = ~$200
──────────────────────────────────────────
Total: ~$4,366/month
(Functions not optimized for long-running services)
```

---

## Service-to-Service Communication Patterns

### Pattern 1: Request/Response (AuthSvc → MailSvc)

| Platform | Method | Example |
|----------|--------|---------|
| **K8s** | Direct HTTP | `httpx.post("http://mailsvc:8080/mail/queue", ...)` |
| **Azure CA** | DAPR | `httpx.post("http://localhost:3500/v1.0/invoke/mailsvc/method/mail/queue", ...)` |
| **GCP Run** | HTTPS + IAM | `httpx.post("https://mailsvc-abc.run.app/mail/queue", headers={"Authorization": f"Bearer {id_token}"})` |

### Pattern 2: Async Messaging (Event-driven)

| Platform | Method | Example |
|----------|--------|---------|
| **K8s** | RabbitMQ / Kafka | External message broker |
| **Azure CA** | DAPR Pub/Sub | `dapr.publish("user-events", "user.created", {...})` |
| **GCP Run** | Cloud Pub/Sub | `publisher.publish("users/created", Message(...))` |

### Pattern 3: Service Discovery

| Platform | Method | Example |
|----------|--------|---------|
| **K8s** | Cluster DNS | `authsvc.default.svc.cluster.local` |
| **Azure CA** | DAPR sidecar | `localhost:3500` (sidecar routes to service) |
| **GCP Run** | Service URLs | `https://authsvc-abc123.run.app` (hardcoded or env var) |

---

## Decision Tree: Which Platform to Choose?

```
                    Start Here
                        │
        ┌───────────────┴───────────────┐
        │                               │
   Are you already          Multi-cloud / Want
   on AWS/Azure/GCP?        maximum portability?
        │                               │
      YES                             YES
        │                               │
        ↓                               ↓
   ┌─────────────┐              ┌──────────────┐
   │Your Cloud   │              │Kubernetes    │
   │Provider     │              │(AKS/GKE/EKS) │
   │Preferred?   │              └──────────────┘
   └────┬────────┘
        │
        ├─ AWS? → Use ECS/Fargate or EKS
        │
        ├─ Azure? → Container Apps
        │          (or AKS if complex)
        │
        └─ GCP? → Cloud Run
                 (or GKE if complex)

    Are you building          Do you want to scale
    production software       to ZERO when idle?
    with multiple teams?          │
            │                      YES
           YES                     │
            │                      ↓
            ↓                 ┌──────────┐
        Kubernetes          Cloud Run
        (mature ops)
```

---

## Final Recommendation

| Use Case | Recommendation | Why |
|----------|---|---|
| **Startup** | GCP Cloud Run | Cheapest, simplest, scales to zero |
| **Enterprise Azure** | Azure Container Apps | Serverless K8s, DAPR for patterns |
| **Enterprise AWS** | ECS Fargate | Managed, simple, good cost |
| **Multi-cloud** | Kubernetes | Portable, run anywhere |
| **Complex microservices** | Kubernetes + Istio | Service mesh, traffic management |
| **High throughput** | Kubernetes (on-prem) | Full control, lowest latency |

---

## Migration Path

```
Local Dev
(Docker Compose)
        │
        ├─→ Staging (Cloud Run / Container Apps)
        │   └─→ Production (same platform)
        │
        └─→ Staging (Kubernetes)
            └─→ Production (Kubernetes)
```

Choose your entry point based on your cloud provider and operational expertise!
