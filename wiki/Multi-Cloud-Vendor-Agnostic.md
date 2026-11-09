# Multi-Cloud Vendor-Agnostic Architecture

Designing and deploying to Azure Container Apps, GCP Cloud Run, and AWS Fargate **simultaneously** from a single codebase.

## Core Principles

### 1. **Abstraction Layers**

```
┌─────────────────────────────────────────┐
│     APPLICATION CODE (Vendor-agnostic)  │
│  (FastAPI, SQLAlchemy, asyncio)         │
└──────────────────┬──────────────────────┘
                   │
┌──────────────────▼──────────────────────┐
│  DEPLOYMENT ABSTRACTION LAYER            │
│  (Environment variables, ConfigMaps)     │
└──────────────────┬──────────────────────┘
                   │
    ┌──────────────┼──────────────┐
    ▼              ▼              ▼
┌────────────┐ ┌──────────┐ ┌──────────┐
│Azure Infra │ │GCP Infra │ │AWS Infra │
│(YAML/TF)   │ │(YAML/TF) │ │(YAML/TF) │
└────────────┘ └──────────┘ └──────────┘
```

### 2. **Unified Service Communication**

Instead of platform-specific mechanisms:
- ❌ Azure DAPR sidecar
- ❌ GCP IAM service accounts
- ❌ AWS X-Ray service map

Use:
- ✅ **Service Mesh** (Istio/Linkerd) - works on all clouds & on-prem
- ✅ **API Gateway** (Kong/Traefik) - unified ingress
- ✅ **OpenTelemetry** - vendor-agnostic observability

### 3. **Configuration Strategy**

```
# Environment-agnostic configuration
DATABASE_URL=postgresql://user:pass@db-service:5432/db
MAIL_SERVICE_URL=http://mail-service  # DNS name, not cloud-specific
AUTH_SERVICE_URL=http://auth-service
CACHE_URL=redis://cache-service:6379

# No cloud-specific settings:
# ❌ DAPR_ENDPOINT (Azure-specific)
# ❌ SERVICE_ACCOUNT_JSON (GCP-specific)
# ❌ ASSUME_ROLE_ARN (AWS-specific)
```

---

## Architecture Patterns

### Pattern 1: Service Mesh (Istio)

**Best for:** Multi-cloud, on-prem hybrid, complex routing

```
┌─────────────────────────────────────────────────────────────────┐
│                   Internet                                       │
└────────────────────────┬────────────────────────────────────────┘
                         │ HTTPS (TLS)
                         ↓
┌─────────────────────────────────────────────────────────────────┐
│                   Ingress Gateway (Istio)                       │
│                   (cloud load balancer)                         │
└────────────────────────┬────────────────────────────────────────┘
                         │ HTTP (internal mesh)
        ┌────────────────┼────────────────┐
        ↓                ↓                ↓
    ┌────────┐      ┌─────────┐      ┌──────────┐
    │ Portal │      │ AuthSvc │      │ MailSvc  │
    │   +    │      │   +     │      │    +     │
    │Envoy   │      │Envoy    │      │Envoy     │
    │sidecar │      │sidecar  │      │sidecar   │
    └────────┘      └─────────┘      └──────────┘
        │                │                │
        └────────────────┼────────────────┘
                         │
              ┌──────────┴──────────┐
              ↓                     ↓
          ┌────────┐           ┌──────────┐
          │Service │           │ Virtual  │
          │ Entry  │           │ Service  │
          │(config)│           │ (config) │
          └────────┘           └──────────┘
```

**Same Istio setup on Azure, GCP, AWS, Kubernetes**

```yaml
# VirtualService (works on all clouds with Istio)
apiVersion: networking.istio.io/v1beta1
kind: VirtualService
metadata:
  name: mailsvc
spec:
  hosts:
  - mailsvc
  http:
  - match:
    - uri:
        prefix: /internal
    route:
    - destination:
        host: mailsvc
        port:
          number: 8080
    timeout: 30s
    retries:
      attempts: 3
      perTryTimeout: 10s
```

### Pattern 2: API Gateway (Kong)

**Best for:** Simpler setup, API-first architecture

```
Cloud Provider Load Balancer
        ↓
    Kong API Gateway (container)
        ↓
    ┌───┬───┬───┐
    ↓   ↓   ↓
 Portal AuthSvc MailSvc
```

**Kong config (same across all clouds):**
```yaml
services:
  - name: portal
    url: http://portal:8000
    routes:
      - name: portal-route
        paths:
          - /
        strip_path: false

  - name: authsvc
    url: http://authsvc:8001
    routes:
      - name: auth-route
        paths:
          - /api/auth
        strip_path: false

  - name: mailsvc
    url: http://mailsvc:8002
    routes:
      - name: mail-route
        paths:
          - /mail
        strip_path: false
```

### Pattern 3: Ingress Controller (Traefik)

**Best for:** Lightweight, easier to manage than Istio

```yaml
# Same IngressRoute on all clouds
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: portal-route
spec:
  entryPoints:
    - websecure
  routes:
    - match: PathPrefix(`/`)
      kind: Rule
      services:
        - name: portal
          port: 8000
    - match: PathPrefix(`/api/auth`)
      kind: Rule
      services:
        - name: authsvc
          port: 8001
```

---

## Infrastructure as Code (IaC)

### Terraform: Single Configuration, Multiple Clouds

**Directory structure:**
```
infra/
├── terraform/
│   ├── main.tf              # Cloud-agnostic resources
│   ├── variables.tf         # Input variables
│   ├── outputs.tf           # Output values
│   ├── cloud/
│   │   ├── azure/           # Azure Container Apps provider
│   │   │   ├── provider.tf
│   │   │   ├── containers.tf
│   │   │   ├── variables.tf
│   │   │   └── terraform.tfvars
│   │   ├── gcp/             # GCP Cloud Run provider
│   │   │   ├── provider.tf
│   │   │   ├── cloud-run.tf
│   │   │   ├── variables.tf
│   │   │   └── terraform.tfvars
│   │   └── aws/             # AWS Fargate provider
│   │       ├── provider.tf
│   │       ├── fargate.tf
│   │       ├── variables.tf
│   │       └── terraform.tfvars
│   └── modules/
│       ├── service/         # Reusable service module
│       ├── database/        # Database module (works on all clouds)
│       └── cache/           # Cache layer module
└── kubernetes/              # For local K8s or EKS/AKS/GKE
    ├── base/
    │   ├── authsvc.yaml
    │   ├── mailsvc.yaml
    │   ├── portal.yaml
    │   └── service-mesh.yaml
    └── overlays/
        ├── azure/           # Azure-specific overlays
        ├── gcp/             # GCP-specific overlays
        └── aws/             # AWS-specific overlays
```

### Example: Service Module (Cloud-Agnostic)

**`infra/terraform/modules/service/main.tf`:**
```hcl
# This module works on all clouds
variable "cloud_provider" {
  type = string
  # "azure", "gcp", or "aws"
}

variable "service_name" {}
variable "image" {}
variable "port" {}
variable "environment_vars" {
  type = map(string)
}

# Deploy based on provider
module "azure_service" {
  count = var.cloud_provider == "azure" ? 1 : 0
  source = "../azure"
  
  service_name     = var.service_name
  image            = var.image
  port             = var.port
  environment_vars = var.environment_vars
}

module "gcp_service" {
  count = var.cloud_provider == "gcp" ? 1 : 0
  source = "../gcp"
  
  service_name     = var.service_name
  image            = var.image
  port             = var.port
  environment_vars = var.environment_vars
}

module "aws_service" {
  count = var.cloud_provider == "aws" ? 1 : 0
  source = "../aws"
  
  service_name     = var.service_name
  image            = var.image
  port             = var.port
  environment_vars = var.environment_vars
}

output "service_url" {
  value = var.cloud_provider == "azure" ? module.azure_service[0].url : (
    var.cloud_provider == "gcp" ? module.gcp_service[0].url :
    module.aws_service[0].url
  )
}
```

---

## CI/CD: Unified Pipeline

### GitHub Actions: Deploy to All Clouds Simultaneously

**`.github/workflows/deploy-multi-cloud.yml`:**
```yaml
name: Deploy to Multi-Cloud

on:
  push:
    branches:
      - main
    paths:
      - 'src/**'
      - 'infra/**'

env:
  IMAGE_TAG: ${{ github.sha }}

jobs:
  # Build once, push to all registries
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      
      - name: Build Docker image
        run: |
          docker build -t utsu-portal:${{ env.IMAGE_TAG }} \
            --build-arg SERVICE=authsvc -f Dockerfile .
      
      - name: Push to Docker Hub
        run: |
          echo "${{ secrets.DOCKER_PASSWORD }}" | docker login -u "${{ secrets.DOCKER_USERNAME }}" --password-stdin
          docker tag utsu-portal:${{ env.IMAGE_TAG }} utsuportal/authsvc:${{ env.IMAGE_TAG }}
          docker push utsuportal/authsvc:${{ env.IMAGE_TAG }}
      
      - name: Push to Azure ACR
        run: |
          az login --service-principal \
            -u "${{ secrets.AZURE_CLIENT_ID }}" \
            -p "${{ secrets.AZURE_CLIENT_SECRET }}" \
            --tenant "${{ secrets.AZURE_TENANT_ID }}"
          az acr login --name utsuacr
          docker tag utsu-portal:${{ env.IMAGE_TAG }} utsuacr.azurecr.io/authsvc:${{ env.IMAGE_TAG }}
          docker push utsuacr.azurecr.io/authsvc:${{ env.IMAGE_TAG }}
      
      - name: Push to GCP Artifact Registry
        run: |
          echo "${{ secrets.GCP_SERVICE_ACCOUNT_JSON }}" > /tmp/gcp-key.json
          gcloud auth activate-service-account --key-file=/tmp/gcp-key.json
          gcloud auth configure-docker us-central1-docker.pkg.dev
          docker tag utsu-portal:${{ env.IMAGE_TAG }} us-central1-docker.pkg.dev/utsu-portal-project/utsu-repo/authsvc:${{ env.IMAGE_TAG }}
          docker push us-central1-docker.pkg.dev/utsu-portal-project/utsu-repo/authsvc:${{ env.IMAGE_TAG }}
      
      - name: Push to AWS ECR
        run: |
          aws configure set aws_access_key_id ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws configure set aws_secret_access_key ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin ${{ secrets.AWS_ACCOUNT_ID }}.dkr.ecr.us-east-1.amazonaws.com
          docker tag utsu-portal:${{ env.IMAGE_TAG }} ${{ secrets.AWS_ACCOUNT_ID }}.dkr.ecr.us-east-1.amazonaws.com/authsvc:${{ env.IMAGE_TAG }}
          docker push ${{ secrets.AWS_ACCOUNT_ID }}.dkr.ecr.us-east-1.amazonaws.com/authsvc:${{ env.IMAGE_TAG }}

  # Deploy to all clouds in parallel
  deploy-azure:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Deploy to Azure Container Apps
        run: |
          az login --service-principal \
            -u "${{ secrets.AZURE_CLIENT_ID }}" \
            -p "${{ secrets.AZURE_CLIENT_SECRET }}" \
            --tenant "${{ secrets.AZURE_TENANT_ID }}"
          
          az containerapp update \
            --name authsvc \
            --resource-group utsu-portal-rg \
            --image utsuacr.azurecr.io/authsvc:${{ env.IMAGE_TAG }}

  deploy-gcp:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Deploy to GCP Cloud Run
        run: |
          echo "${{ secrets.GCP_SERVICE_ACCOUNT_JSON }}" > /tmp/gcp-key.json
          gcloud auth activate-service-account --key-file=/tmp/gcp-key.json
          gcloud config set project utsu-portal-project
          
          gcloud run deploy authsvc \
            --image us-central1-docker.pkg.dev/utsu-portal-project/utsu-repo/authsvc:${{ env.IMAGE_TAG }} \
            --platform managed \
            --region us-central1

  deploy-aws:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Deploy to AWS Fargate
        run: |
          aws configure set aws_access_key_id ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws configure set aws_secret_access_key ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws configure set region us-east-1
          
          # Update ECS service with new image
          aws ecs update-service \
            --cluster utsu-portal \
            --service authsvc \
            --force-new-deployment \
            --region us-east-1

  # Run integration tests on all clouds
  integration-tests:
    needs: [deploy-azure, deploy-gcp, deploy-aws]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Test Azure deployment
        run: |
          AZURE_URL=$(az containerapp show --name portal --resource-group utsu-portal-rg --query properties.configuration.ingress.fqdn -o tsv)
          curl -f https://$AZURE_URL/health || exit 1
      
      - name: Test GCP deployment
        run: |
          GCP_URL=$(gcloud run services describe portal --region us-central1 --format='value(status.url)')
          curl -f $GCP_URL/health || exit 1
      
      - name: Test AWS deployment
        run: |
          AWS_ALB=$(aws elbv2 describe-load-balancers --region us-east-1 --query 'LoadBalancers[0].DNSName' -o text)
          curl -f http://$AWS_ALB/health || exit 1
```

---

## Service Communication Across Clouds

### Option 1: Service Mesh (Recommended)

**Works on any Kubernetes or managed container platform with Istio support:**

```bash
# Install Istio on each cloud platform
istioctl install --set profile=demo -y

# Deploy to all clouds
kubectl apply -f k8s/base/
```

**Service calls remain unchanged:**
```python
# Same code, any cloud
response = requests.get(
    "http://authsvc:8080/api/verify-token",
    headers={"Authorization": f"Bearer {service_token}"}
)
```

### Option 2: API Gateway Pattern

**Kong/Traefik deployed as sidecar container:**

```yaml
# Azure Container Apps
{
  "containerName": "kong",
  "image": "kong:latest",
  "resources": {
    "cpu": 0.5,
    "memory": "1Gi"
  }
}

# GCP Cloud Run
apiVersion: v1
kind: Service
metadata:
  name: kong
spec:
  type: ClusterIP
  ports:
  - port: 8000
    targetPort: 8000

# AWS Fargate (ECS task definition)
{
  "name": "kong",
  "image": "kong:latest",
  "cpu": 512,
  "memory": 1024
}
```

### Option 3: Service Registry Pattern

**Central service registry (Consul, Eureka) queried by all services:**

```python
# Service discovery
from consul import Consul

consul = Consul(host='consul-server')

# Register this service
consul.agent.service.register(
    name='authsvc',
    service_id='authsvc-1',
    address='localhost',
    port=8001,
    check=consul.Check.http('http://localhost:8001/health', interval='10s')
)

# Discover another service
mailsvc = consul.catalog.service('mailsvc')[1][0]
mailsvc_url = f"http://{mailsvc['ServiceAddress']}:{mailsvc['ServicePort']}"
```

---

## Database Strategy for Multi-Cloud

### Option 1: Managed Database Per Cloud (Recommended)

```
Azure:  Azure Database for PostgreSQL
GCP:    Cloud SQL for PostgreSQL
AWS:    Amazon RDS for PostgreSQL

All use same PostgreSQL version & schema
All accessed via service mesh or VPC peering
```

**Connection from any service to any cloud database:**
```python
# Environment variable points to correct database
DATABASE_URL = os.getenv('DATABASE_URL')
# Azure:  postgresql://user:pass@utsu-db.postgres.database.azure.com:5432/authdb
# GCP:    postgresql://user:pass@1.2.3.4:5432/authdb
# AWS:    postgresql://user:pass@utsu-db.c123abc.us-east-1.rds.amazonaws.com:5432/authdb
```

### Option 2: Multi-Cloud Database Replication

**Active-active across clouds:**
```
┌──────────────────────────┐
│  Azure PostgreSQL        │
│  (us-east)              │
└────────┬─────────────────┘
         │ WAL replication
         ↓
┌──────────────────────────┐
│  GCP Cloud SQL           │
│  (us-central)           │
└────────┬─────────────────┘
         │ bidirectional sync
         ↓
┌──────────────────────────┐
│  AWS RDS                 │
│  (us-east-1)            │
└──────────────────────────┘
```

### Option 3: Application-Level Sync (Kafka/Pub-Sub)

**Each cloud reads/writes its own database, keeps in sync:**

```
Azure authsvc writes to Azure DB
  ↓ publishes event to Azure Event Hubs
  ↓ subscribed by GCP service (reads via Pub/Sub)
  ↓ GCP service writes to GCP database
```

---

## Environment Configuration

### `.env` Strategy: Single Configuration, All Clouds

**Root `.env`:**
```bash
# Cloud-agnostic configuration
SERVICE_NAME=authsvc
CONTAINER_PORT=8080
DATABASE_URL=${DATABASE_URL}  # Set per-deployment
CACHE_URL=${CACHE_URL}        # Set per-deployment
AUTH_SERVICE_URL=http://authsvc:8080
MAIL_SERVICE_URL=http://mailsvc:8080
PORTAL_SERVICE_URL=http://portal:8000

# Observability
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
LOG_LEVEL=INFO
TRACE_SAMPLE_RATE=0.1
```

**Azure `.env.azure`:**
```bash
# Import base config
source .env

# Azure-specific: provided by Container Apps
# (no need to specify DAPR endpoints, Azure handles it)
```

**GCP `.env.gcp`:**
```bash
# Import base config
source .env

# GCP-specific: IAM is handled by Cloud Run
# (no need to specify service account, injected automatically)
GOOGLE_CLOUD_PROJECT=utsu-portal-project
```

**AWS `.env.aws`:**
```bash
# Import base config
source .env

# AWS-specific: IAM role assumed by task
AWS_REGION=us-east-1
```

---

## Observability: Vendor-Agnostic

### OpenTelemetry (Works on All Clouds)

```python
# Same instrumentation code on all clouds
from opentelemetry import trace, metrics
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

# Setup
trace.set_tracer_provider(TracerProvider())
trace.get_tracer_provider().add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter())
)

# Auto-instrument
FastAPIInstrumentor.instrument_app(app)
SQLAlchemyInstrumentor().instrument()
```

**Same OTEL collector works on all clouds:**
```yaml
# Local or cloud deployment
exporters:
  prometheus:
    endpoint: "0.0.0.0:8888"
  jaeger:
    endpoint: "jaeger-collector:14250"

processors:
  batch:

receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
```

---

## Deployment Script: Deploy to All Clouds at Once

**`scripts/deploy-multi-cloud.sh`:**

```bash
#!/bin/bash

set -e

# Configuration
IMAGE_TAG="${1:-$(git rev-parse --short HEAD)}"
CLOUDS="${2:-azure gcp aws}"  # Default: all clouds

echo "🚀 Deploying UTSU Portal to multi-cloud"
echo "   Image tag: $IMAGE_TAG"
echo "   Clouds: $CLOUDS"
echo ""

# Build image
echo "📦 Building Docker image..."
docker build -t utsu-portal:$IMAGE_TAG \
  --build-arg SERVICE=authsvc -f Dockerfile .

# Deploy to each cloud in parallel
for cloud in $CLOUDS; do
  case $cloud in
    azure)
      echo "🔵 Deploying to Azure Container Apps..."
      az login --service-principal \
        -u "$AZURE_CLIENT_ID" \
        -p "$AZURE_CLIENT_SECRET" \
        --tenant "$AZURE_TENANT_ID" &
      ;;
    gcp)
      echo "🔴 Deploying to GCP Cloud Run..."
      echo "$GCP_SERVICE_ACCOUNT_JSON" | gcloud auth activate-service-account --key-file=- &
      ;;
    aws)
      echo "🟠 Deploying to AWS Fargate..."
      aws configure set aws_access_key_id "$AWS_ACCESS_KEY_ID"
      aws configure set aws_secret_access_key "$AWS_SECRET_ACCESS_KEY" &
      ;;
  esac
done

wait

# Parallel deployments
for cloud in $CLOUDS; do
  (
    case $cloud in
      azure)
        az acr login --name utsuacr
        docker tag utsu-portal:$IMAGE_TAG utsuacr.azurecr.io/authsvc:$IMAGE_TAG
        docker push utsuacr.azurecr.io/authsvc:$IMAGE_TAG
        az containerapp update \
          --name authsvc \
          --resource-group utsu-portal-rg \
          --image utsuacr.azurecr.io/authsvc:$IMAGE_TAG
        ;;
      gcp)
        gcloud auth configure-docker us-central1-docker.pkg.dev
        docker tag utsu-portal:$IMAGE_TAG us-central1-docker.pkg.dev/utsu-portal-project/utsu-repo/authsvc:$IMAGE_TAG
        docker push us-central1-docker.pkg.dev/utsu-portal-project/utsu-repo/authsvc:$IMAGE_TAG
        gcloud run deploy authsvc \
          --image us-central1-docker.pkg.dev/utsu-portal-project/utsu-repo/authsvc:$IMAGE_TAG \
          --platform managed \
          --region us-central1 \
          --no-allow-unauthenticated
        ;;
      aws)
        aws ecr get-login-password --region us-east-1 | \
          docker login --username AWS --password-stdin "$AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com"
        docker tag utsu-portal:$IMAGE_TAG "$AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/authsvc:$IMAGE_TAG"
        docker push "$AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/authsvc:$IMAGE_TAG"
        aws ecs update-service \
          --cluster utsu-portal \
          --service authsvc \
          --force-new-deployment
        ;;
    esac
  ) &
done

wait

echo "✅ Deployment complete to all clouds!"
```

---

## Multi-Cloud Deployment Patterns

### Pattern 1: Same Deployment, Different Clouds (Failover)

```
┌─────────────┐
│   DNS CDN   │
└──────┬──────┘
       │ Routes based on latency
    ┌──┴──┬─────┬──────┐
    ▼     ▼     ▼      ▼
 Azure   GCP   AWS   On-Prem
  (Primary) (Secondary) (Tertiary) (Fallback)
```

### Pattern 2: Distributed Architecture (Load Balancing)

```
┌──────────────────────────────────┐
│  Global Load Balancer (Cloudflare)│
└──────────────────┬───────────────┘
       ┌───────────┼───────────┐
       ▼           ▼           ▼
    Azure     GCP Cloud      AWS
    (30%)     Run (40%)      Fargate (30%)
```

### Pattern 3: Regional Redundancy

```
Azure US-East → GCP US-Central → AWS US-West
(Primary)        (Secondary)      (Disaster Recovery)

All sync via database replication
```

---

## Checklist: Making Your Service Vendor-Agnostic

- [ ] All code uses only standard libraries (FastAPI, SQLAlchemy, asyncio)
- [ ] Configuration via environment variables (no hardcoded cloud references)
- [ ] Service discovery via DNS/service mesh (not platform-specific mechanisms)
- [ ] Observability via OpenTelemetry (not cloud-specific APM)
- [ ] IaC using Terraform (defines infrastructure once)
- [ ] CI/CD targets multiple registries
- [ ] Database schema identical across clouds
- [ ] Secret management abstraction (not cloud-specific vaults)
- [ ] Reverse proxy/API gateway abstracts services
- [ ] All tests pass on local, Azure, GCP, AWS
- [ ] Disaster recovery plan documented
- [ ] Cost tracking per cloud set up
- [ ] Load testing simulates multi-cloud routing

---

## Trade-Offs

| Approach | Complexity | Portability | Performance | Cost |
|----------|-----------|------------|-------------|------|
| Service Mesh (Istio) | High | Excellent | Good | Medium |
| API Gateway (Kong) | Medium | Excellent | Very Good | Low-Medium |
| Service Registry (Consul) | Medium | Excellent | Good | Low |
| Direct HTTP + DNS | Low | Good | Excellent | Low |

---

## Next Steps

1. **Choose your service communication pattern** (mesh, gateway, or registry)
2. **Set up Terraform modules** for each cloud provider
3. **Create GitHub Actions pipeline** for multi-cloud deployment
4. **Implement OpenTelemetry** for unified observability
5. **Test failover** between clouds
6. **Document runbooks** for multi-cloud operations
7. **Set up cost monitoring** per cloud

See next file: `Multi-Cloud-Deployment-Guide.md`

