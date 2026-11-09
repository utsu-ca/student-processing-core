# Microservice Architecture on Azure Container Apps & GCP Cloud Run

## Comparison Matrix

| Feature | Kubernetes | Azure Container Apps | GCP Cloud Run | Azure ACI |
|---------|-----------|----------------------|---------------|---------:|
| **Reverse Proxy** | NGINX Ingress | Built-in | Built-in | Manual (Container) |
| **Auto-scaling** | Horizontal Pod Autoscaler | Native (concurrent reqs) | Native (concurrent reqs) | Limited |
| **Inter-service comms** | Kubernetes DNS (cluster) | Built-in (DAPR/HTTP) | HTTP only (public URLs) | HTTP only |
| **Service discovery** | DNS (`authsvc:8080`) | DAPR service invocation | Cloud Run service URL | Manual |
| **Cost model** | Resource units (CPU/mem) | Pay-per-execution + reserved | Pay-per-execution | Pay-per-execution |
| **Cold starts** | None (always running) | Warm containers | ~100-500ms | Yes |
| **Environment** | On-prem/cloud/hybrid | Microsoft Azure only | Google Cloud only | Microsoft Azure only |
| **Networking** | VPC/SecurityPolicy | VNET/NSG | Private GKE/VPC | VNET/NSG |

---

## Option 1: Azure Container Apps (Recommended for Azure)

### Why Azure Container Apps?

✅ **Serverless Kubernetes** - container orchestration without managing K8s
✅ **DAPR support** - service-to-service communication with retries
✅ **Built-in ingress** - reverse proxy included
✅ **Cheap** - pay only for actual execution
✅ **Managed HTTPS** - automatic certificate provisioning

### Architecture on Azure Container Apps

```
┌─────────────────────────────────────────────────────────────────┐
│                   Internet / Users                              │
│                                                                 │
│  https://portal.example.org                                    │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ↓
        ┌────────────────────────────────────────────┐
        │ Azure Container Apps Environment           │
        │ (managed reverse proxy & load balancer)    │
        │                                            │
        │  ┌──────────┐  ┌──────────┐ ┌─────────┐   │
        │  │ Portal   │  │ AuthSvc  │ │MailSvc │   │
        │  │ Container│  │Container │ │Container│   │
        │  └──────────┘  └──────────┘ └─────────┘   │
        │  (3 replicas)  (2 replicas) (2 replicas)   │
        │                                            │
        │  ┌──────────────────────────────────────┐  │
        │  │      DAPR Sidecar (each pod)        │  │
        │  │  - Service discovery                 │  │
        │  │  - mTLS between containers           │  │
        │  │  - Pub/sub, state management         │  │
        │  └──────────────────────────────────────┘  │
        └────────────────────────────────────────────┘
                             │
        ┌────────────────────┼────────────────────┐
        ↓                    ↓                    ↓
    ┌─────────┐          ┌─────────┐         ┌──────────┐
    │PostgreSQL│          │ Redis   │         │ Storage  │
    │Database  │          │ Cache   │         │(Secrets) │
    └─────────┘          └─────────┘         └──────────┘
```

### Step-by-Step Deployment

#### 1. Setup Azure Environment

```bash
# Install Azure CLI
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash

# Login
az login

# Create resource group
az group create \
  --name utsu-portal-rg \
  --location eastus

# Create container registry
az acr create \
  --resource-group utsu-portal-rg \
  --name utsuacr \
  --sku Basic

# Login to registry
az acr login --name utsuacr
```

#### 2. Build & Push Docker Images

```bash
# From project root
cd /home/tony-utsu/Desktop/student-processing-core

# Build Portal service
docker build -t portal:1.0 \
  --build-arg SERVICE=portal \
  -f Dockerfile .

# Build AuthSvc
docker build -t authsvc:1.0 \
  --build-arg SERVICE=authsvc \
  -f Dockerfile .

# Build MailSvc
docker build -t mailsvc:1.0 \
  --build-arg SERVICE=mailsvc \
  -f Dockerfile .

# Tag for registry
docker tag portal:1.0 utsuacr.azurecr.io/portal:1.0
docker tag authsvc:1.0 utsuacr.azurecr.io/authsvc:1.0
docker tag mailsvc:1.0 utsuacr.azurecr.io/mailsvc:1.0

# Push to ACR
docker push utsuacr.azurecr.io/portal:1.0
docker push utsuacr.azurecr.io/authsvc:1.0
docker push utsuacr.azurecr.io/mailsvc:1.0
```

#### 3. Create Container Apps Environment

```bash
# Create environment (manages VNET, DAPR, etc.)
az containerapp env create \
  --name utsu-env \
  --resource-group utsu-portal-rg \
  --location eastus
```

#### 4. Deploy AuthSvc (with RBAC)

```bash
az containerapp create \
  --name authsvc \
  --resource-group utsu-portal-rg \
  --environment utsu-env \
  --image utsuacr.azurecr.io/authsvc:1.0 \
  --target-port 8080 \
  --ingress external \
  --registry-server utsuacr.azurecr.io \
  --cpu 0.5 \
  --memory 1Gi \
  --min-replicas 2 \
  --max-replicas 4 \
  --env-vars \
    "PORTAL_SERVICE_TOKEN=your-secret-token-32-chars" \
    "PORTAL_AUDIT_DB_ACCESS=true" \
    "DATABASE_URL=postgresql://user:pass@db.postgres.database.azure.com/authdb" \
  --secrets \
    "db-connection-string=DefaultEndpointProtocol=https;AccountName=utsudb;..." \
  --dapr-enabled \
  --dapr-app-id authsvc \
  --dapr-app-port 8080 \
  --dapr-http-port 3500
```

#### 5. Deploy MailSvc

```bash
az containerapp create \
  --name mailsvc \
  --resource-group utsu-portal-rg \
  --environment utsu-env \
  --image utsuacr.azurecr.io/mailsvc:1.0 \
  --target-port 8080 \
  --ingress internal \
  --registry-server utsuacr.azurecr.io \
  --cpu 0.5 \
  --memory 1Gi \
  --min-replicas 1 \
  --max-replicas 3 \
  --env-vars \
    "PORTAL_SERVICE_TOKEN=your-secret-token-32-chars" \
    "PORTAL_SMTP_URL=smtps://user:pass@smtp.sendgrid.net:465" \
    "PORTAL_MAIL_FROM=noreply@example.org" \
    "DATABASE_URL=postgresql://user:pass@db.postgres.database.azure.com/maildb" \
  --dapr-enabled \
  --dapr-app-id mailsvc \
  --dapr-app-port 8080 \
  --dapr-http-port 3500
```

#### 6. Deploy Portal Service

```bash
az containerapp create \
  --name portal \
  --resource-group utsu-portal-rg \
  --environment utsu-env \
  --image utsuacr.azurecr.io/portal:1.0 \
  --target-port 8080 \
  --ingress external \
  --registry-server utsuacr.azurecr.io \
  --cpu 1.0 \
  --memory 2Gi \
  --min-replicas 2 \
  --max-replicas 5 \
  --env-vars \
    "PORTAL_SERVICE_TOKEN=your-secret-token-32-chars" \
    "PORTAL_AUTH_ENDPOINT=http://authsvc:3500" \
    "PORTAL_MAIL_ENDPOINT=http://mailsvc:3500" \
  --dapr-enabled \
  --dapr-app-id portal \
  --dapr-app-port 8080
```

### Key Azure Container Apps Concepts

#### **Ingress Types**

```bash
# External: accessible from internet (reverse proxy)
--ingress external

# Internal: only from other containers in environment (service discovery)
--ingress internal
```

#### **DAPR Integration**

DAPR provides:
- **Service Invocation**: `http://authsvc:3500/v1.0/invoke/mailsvc/method/mail/queue`
- **Secrets Management**: Access Azure Key Vault secrets
- **Pub/Sub**: Async messaging between services
- **State Management**: Redis-like interface
- **mTLS**: Automatic certificate rotation between services

```python
# Instead of direct HTTP to mailsvc:8080, use DAPR:
import httpx

# Direct (old way)
# response = httpx.post("http://mailsvc:8080/mail/queue", ...)

# DAPR way (recommended for Azure Container Apps)
response = httpx.post(
    "http://localhost:3500/v1.0/invoke/mailsvc/method/mail/queue",
    json={"to": "user@example.com", "subject": "Hello", "body": "..."}
)
```

#### **Auto-scaling Configuration**

```bash
# Scale based on concurrent requests
az containerapp update \
  --name authsvc \
  --resource-group utsu-portal-rg \
  --scale-rule-name "http-requests" \
  --scale-rule-type http \
  --scale-rule-http-concurrency 100  # scale when > 100 concurrent reqs
```

### Accessing Services from Code

```python
# authsvc.py - call mailsvc
import os
import httpx

DAPR_HTTP_PORT = os.getenv("DAPR_HTTP_PORT", "3500")

async def send_welcome_email(user_email: str, first_name: str):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"http://localhost:{DAPR_HTTP_PORT}/v1.0/invoke/mailsvc/method/mail/queue",
            json={
                "to": user_email,
                "template_id": "welcome",
                "template_vars": {"first_name": first_name}
            }
        )
    return response.json()
```

### Azure Key Vault Integration

```bash
# Store secrets in Key Vault
az keyvault create --name utsu-vault --resource-group utsu-portal-rg

# Add secrets
az keyvault secret set \
  --vault-name utsu-vault \
  --name "service-token" \
  --value "your-32-char-minimum-token"

# Reference in container app
az containerapp create \
  --name authsvc \
  ... \
  --secrets \
    "service-token=keyvaultRef:$serviceTokenSecretId" \
  --env-vars \
    "PORTAL_SERVICE_TOKEN=service-token"  # references the secret
```

---

## Option 2: GCP Cloud Run (Recommended for Google Cloud)

### Why GCP Cloud Run?

✅ **Truly serverless** - no infrastructure management
✅ **Fast cold starts** - ~100-500ms
✅ **Auto-scaling** - scales to zero when no traffic
✅ **Cheap** - pay per 100ms of execution
✅ **Developer friendly** - deploy from source or container

### Architecture on GCP Cloud Run

```
┌──────────────────────────────────────────────────────────┐
│                   Internet / Users                       │
│  https://portal-abcd1234.run.app                        │
└───────────────────────┬──────────────────────────────────┘
                        │
            ┌───────────┴────────────┐
            │                        │
            ↓                        ↓
     ┌────────────┐          ┌────────────┐
     │ Cloud Run  │          │ Cloud Run  │
     │ Portal SVC │          │ AuthSvc    │
     │ (public)   │          │ (internal) │
     └────────────┘          └────────────┘
            │                        │
            │        ┌───────────────┤
            │        │               │
            ↓        ↓               ↓
     ┌─────────────────────────┐   ┌──────────┐
     │ Cloud Load Balancer     │   │MailSvc  │
     │ (reverse proxy/routing) │   │(internal)│
     └─────────────────────────┘   └──────────┘
            │
    ┌───────┼───────┬──────────┐
    ↓       ↓       ↓          ↓
  ┌────┐┌────┐┌────┐      ┌─────────┐
  │Auto│Cloud│Cloud│      │ Firestore
  │scal│SQL  │Stor│      │(optional)
  │ing │     │ age│      └─────────┘
  └────┘└────┘└────┘
```

### Step-by-Step Deployment

#### 1. Setup GCP Project

```bash
# Install gcloud CLI
curl https://sdk.cloud.google.com | bash

# Initialize
gcloud init

# Set project
gcloud config set project utsu-portal-project

# Enable required APIs
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  sql-component.googleapis.com \
  compute.googleapis.com
```

#### 2. Create Docker Images & Push to Artifact Registry

```bash
# Create repository
gcloud artifacts repositories create utsu-repo \
  --repository-format=docker \
  --location=us-central1

# Configure Docker authentication
gcloud auth configure-docker us-central1-docker.pkg.dev

# Build and push authsvc
docker build -t us-central1-docker.pkg.dev/utsu-portal-project/utsu-repo/authsvc:1.0 \
  --build-arg SERVICE=authsvc \
  -f Dockerfile .

docker push us-central1-docker.pkg.dev/utsu-portal-project/utsu-repo/authsvc:1.0

# Build and push mailsvc
docker build -t us-central1-docker.pkg.dev/utsu-portal-project/utsu-repo/mailsvc:1.0 \
  --build-arg SERVICE=mailsvc \
  -f Dockerfile .

docker push us-central1-docker.pkg.dev/utsu-portal-project/utsu-repo/mailsvc:1.0

# Build and push portal
docker build -t us-central1-docker.pkg.dev/utsu-portal-project/utsu-repo/portal:1.0 \
  --build-arg SERVICE=portal \
  -f Dockerfile .

docker push us-central1-docker.pkg.dev/utsu-portal-project/utsu-repo/portal:1.0
```

#### 3. Deploy AuthSvc (Internal)

```bash
gcloud run deploy authsvc \
  --image us-central1-docker.pkg.dev/utsu-portal-project/utsu-repo/authsvc:1.0 \
  --platform managed \
  --region us-central1 \
  --memory 1Gi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 10 \
  --set-env-vars \
    PORTAL_SERVICE_TOKEN="your-secret-token-32-chars" \
    DATABASE_URL="postgresql://user:pass@cloudsqlproxy:5432/authdb" \
    PORTAL_AUDIT_DB_ACCESS="true" \
  --no-allow-unauthenticated \
  --vpc-connector utsu-vpc-connector \
  --clear-vpc-connector \
  --ingress internal
```

#### 4. Deploy MailSvc (Internal)

```bash
gcloud run deploy mailsvc \
  --image us-central1-docker.pkg.dev/utsu-portal-project/utsu-repo/mailsvc:1.0 \
  --platform managed \
  --region us-central1 \
  --memory 512Mi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 5 \
  --set-env-vars \
    PORTAL_SERVICE_TOKEN="your-secret-token-32-chars" \
    PORTAL_SMTP_URL="smtps://sendgrid_user:sendgrid_pass@smtp.sendgrid.net:465" \
    DATABASE_URL="postgresql://user:pass@cloudsqlproxy:5432/maildb" \
  --no-allow-unauthenticated \
  --vpc-connector utsu-vpc-connector \
  --ingress internal
```

#### 5. Deploy Portal Service (Public)

```bash
gcloud run deploy portal \
  --image us-central1-docker.pkg.dev/utsu-portal-project/utsu-repo/portal:1.0 \
  --platform managed \
  --region us-central1 \
  --memory 2Gi \
  --cpu 2 \
  --min-instances 1 \
  --max-instances 20 \
  --set-env-vars \
    PORTAL_SERVICE_TOKEN="your-secret-token-32-chars" \
    PORTAL_AUTH_ENDPOINT="https://authsvc-abcd1234.run.app" \
    PORTAL_MAIL_ENDPOINT="https://mailsvc-efgh5678.run.app" \
  --allow-unauthenticated \
  --vpc-connector utsu-vpc-connector \
  --ingress all
```

#### 6. Setup Cloud Load Balancer (Reverse Proxy)

```bash
# Create backend services
gcloud compute backend-services create portal-backend \
  --global \
  --protocol=HTTPS \
  --custom-request-header=X-Client-IP:{client_ip} \
  --load-balancing-scheme=EXTERNAL

# Create URL map (routing rules)
gcloud compute url-maps create utsu-lb \
  --default-service=portal-backend

# Create HTTP(S) proxy
gcloud compute target-https-proxies create utsu-https-proxy \
  --url-map=utsu-lb \
  --ssl-policy=modern-ssl-policy

# Create public IP
gcloud compute addresses create utsu-ip \
  --global \
  --ip-version IPV4

# Create forwarding rule
gcloud compute forwarding-rules create utsu-https \
  --global \
  --target-https-proxy=utsu-https-proxy \
  --address=utsu-ip \
  --ports=443
```

### Accessing Services from Code

#### **Cloud Run Service-to-Service Authentication**

Unlike Kubernetes, Cloud Run doesn't have automatic service discovery. Services must authenticate using **Google Cloud Service Accounts**.

```python
# authsvc calling mailsvc
import httpx
from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials

# Get credentials for calling other Cloud Run services
credentials = Credentials.from_service_account_file(
    "/var/secrets/google/key.json"
)

async def send_email(to_email: str):
    # Get access token
    request = Request()
    credentials.refresh(request)
    
    # Call mailsvc with authentication
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://mailsvc-efgh5678.run.app/mail/queue",
            json={
                "to": to_email,
                "template_id": "welcome",
                "template_vars": {"first_name": "Alice"}
            },
            headers={
                "Authorization": f"Bearer {credentials.token}"
            }
        )
    return response.json()
```

#### **Using Environment Variables for Service URLs**

```python
# main.py
import os
import httpx

MAILSVC_URL = os.getenv("PORTAL_MAIL_ENDPOINT")
SERVICE_TOKEN = os.getenv("PORTAL_SERVICE_TOKEN")

async def queue_email(to_email: str, subject: str):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{MAILSVC_URL}/mail/queue",
            json={"to": to_email, "subject": subject, "body": "..."},
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"}
        )
    return response.json()
```

### VPC Connector (Private Network)

Cloud Run services are isolated by default. To access Cloud SQL or other private resources:

```bash
# Create VPC connector
gcloud compute networks vpc-access connectors create utsu-vpc-connector \
  --region=us-central1 \
  --network=default \
  --range=10.8.0.0/28 \
  --min-instances=2 \
  --max-instances=10

# Deploy service with VPC connector
gcloud run deploy authsvc \
  --vpc-connector utsu-vpc-connector \
  --vpc-connector-egress-settings all \
  ...
```

### Cloud SQL Proxy

```bash
# In Docker, run Cloud SQL Proxy sidecar
FROM python:3.11

RUN wget https://dl.google.com/cloudsql/cloud_sql_proxy.linux.amd64 -O /cloud_sql_proxy && \
    chmod +x /cloud_sql_proxy

COPY . /app
WORKDIR /app

# Run proxy + app
CMD /cloud_sql_proxy -instances=utsu:us-central1:authdb & \
    uvicorn utsu_portal.authsvc:create_auth_app --host 0.0.0.0
```

---

## Option 3: Azure ACI (Containers Only, No Orchestration)

### When to Use ACI

⚠️ **Only for single containers or simple scenarios**
- No service discovery (manual endpoints)
- No auto-scaling (manual replica management)
- No load balancing (need external LB)

```bash
# Deploy individual container
az container create \
  --resource-group utsu-portal-rg \
  --name authsvc-container \
  --image utsuacr.azurecr.io/authsvc:1.0 \
  --ports 8080 \
  --cpu 0.5 \
  --memory 1.0 \
  --dns-name-label authsvc \
  --registry-login-server utsuacr.azurecr.io \
  --registry-username <username> \
  --registry-password <password> \
  --environment-variables \
    PORTAL_SERVICE_TOKEN="token-here" \
    DATABASE_URL="postgresql://..." \
  --ip-address public

# Access via: http://authsvc.eastus.azurecontainer.io:8080
```

**❌ Problems:**
- Services can't find each other (no DNS)
- No built-in load balancing
- Manual scaling required
- Need external reverse proxy

---

## Comparison: Which Should You Choose?

### Azure Container Apps ✅ **BEST for Azure**
- ✅ Kubernetes-like (but serverless)
- ✅ Built-in reverse proxy
- ✅ Service-to-service mTLS
- ✅ Auto-scaling out-of-box
- ✅ DAPR for async patterns
- ❌ Azure only
- ❌ Slightly more expensive than ACI

### GCP Cloud Run ✅ **BEST for Google Cloud**
- ✅ Truly serverless (scales to zero)
- ✅ Faster deployment (from source)
- ✅ Cheapest cold execution
- ✅ Built-in observability (Stackdriver)
- ❌ GCP only
- ❌ More complex service-to-service auth
- ❌ No built-in service discovery

### Kubernetes ✅ **BEST for Portability**
- ✅ Cloud-agnostic (any provider)
- ✅ Most powerful features
- ✅ Standard DevOps tooling
- ✅ Largest community
- ❌ Operational complexity
- ❌ Higher baseline costs
- ❌ Requires infrastructure management

---

## Migration Path

```
Local Development (Docker Compose)
            ↓
Test Environment (Kubernetes/Kind)
            ↓
Production Choice:
  ├─ Azure Cloud → Azure Container Apps + DAPR
  ├─ Google Cloud → GCP Cloud Run + Cloud SQL Proxy
  ├─ Multi-cloud → Kubernetes (AKS/GKE)
  └─ Simple → Docker Compose on VM
```

## Cost Estimation (Monthly)

### Azure Container Apps
- Portal: 2 replicas × $20/month base + $0.000011/vCPU-second = ~$80
- AuthSvc: 2 replicas × $20/month + $0.000008/vCPU-second = ~$60
- MailSvc: 1 replica × $20/month = ~$20
- **Total: ~$160/month**

### GCP Cloud Run
- Portal: 1-20 replicas, 2GB, scales to zero = ~$50/month
- AuthSvc: 0-10 replicas, 1GB = ~$30/month
- MailSvc: 0-5 replicas, 512MB = ~$10/month
- **Total: ~$90/month**

### Kubernetes (Azure AKS / GCP GKE)
- Managed cluster: $0.15/hour = ~$110/month
- Node pools: 3 nodes × $50/month = ~$150/month
- Disk/networking: ~$50/month
- **Total: ~$310+/month**

---

## Summary Table

| Requirement | K8s | Azure CA | GCP Run | ACI |
|------------|-----|----------|---------|-----|
| Service discovery | DNS | DAPR | HTTP URLs | Manual |
| Reverse proxy | NGINX | Built-in | Cloud LB | Manual |
| Auto-scaling | HPA | Native | Native | No |
| mTLS | NetworkPolicy | DAPR | Manual | No |
| Cost | High | Medium | Low | Low |
| Complexity | Very High | Medium | Medium | Low |
| Cold starts | None | ~500ms | ~100ms | ~10s |

Choose based on your cloud provider and operational preferences!
