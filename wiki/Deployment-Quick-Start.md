# Quick Start: Deploy Microservices

This guide covers deploying the auth, email, and portal microservices across different environments.

## Table of Contents

- [Local Development (Docker Compose)](#local-development)
- [Azure Container Apps](#azure-container-apps)
- [GCP Cloud Run](#gcp-cloud-run)
- [Testing & Verification](#testing--verification)

---

## Local Development

### Prerequisites

```bash
# Install Docker & Docker Compose
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

# Verify
docker --version
docker-compose --version
```

### Start All Services

```bash
cd /home/tony-utsu/Desktop/student-processing-core

# Start services (logs merged)
docker-compose up

# Or start in background
docker-compose up -d

# View logs
docker-compose logs -f

# View specific service logs
docker-compose logs -f authsvc
docker-compose logs -f mailsvc
docker-compose logs -f portal
```

### Services Available

| Service | URL | Purpose |
|---------|-----|---------|
| Portal | http://localhost:8000 | Main application |
| AuthSvc | http://localhost:8001 | Authentication & RBAC |
| MailSvc | http://localhost:8002 | Email microservice |
| NGINX | http://localhost:80 | Reverse proxy |
| MailHog | http://localhost:8025 | Email testing UI |
| Redis | localhost:6379 | Cache |
| PostgreSQL (Auth) | localhost:5432 | Auth database |
| PostgreSQL (Mail) | localhost:5433 | Mail database |

### Database Initialization

```bash
# Connect to auth database
psql -h localhost -U authuser -d authdb -p 5432

# Connect to mail database
psql -h localhost -U mailuser -d maildb -p 5433

# Run migrations (if needed)
docker-compose exec authsvc python -m alembic upgrade head
docker-compose exec mailsvc python -m alembic upgrade head
```

### Stop & Cleanup

```bash
# Stop all containers
docker-compose down

# Stop and remove volumes (WARNING: deletes data)
docker-compose down -v

# Restart specific service
docker-compose restart authsvc

# Rebuild images
docker-compose up --build
```

---

## Azure Container Apps

### Prerequisites

```bash
# Install Azure CLI
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash

# Login to Azure
az login

# Set default subscription
az account set --subscription "YOUR-SUBSCRIPTION-ID"
```

### Quick Deploy Script

```bash
#!/bin/bash
set -e

PROJECT_NAME="utsu-portal"
REGION="eastus"
RESOURCE_GROUP="${PROJECT_NAME}-rg"
REGISTRY_NAME="utsuacr"
ENV_NAME="${PROJECT_NAME}-env"

echo "Creating resource group..."
az group create --name $RESOURCE_GROUP --location $REGION

echo "Creating container registry..."
az acr create --resource-group $RESOURCE_GROUP --name $REGISTRY_NAME --sku Basic

echo "Building and pushing Docker images..."
cd /path/to/student-processing-core

# Build and push images
for SERVICE in authsvc mailsvc portal; do
    echo "Building $SERVICE..."
    docker build -t $SERVICE:1.0 --build-arg SERVICE=$SERVICE -f Dockerfile.microservices .
    docker tag $SERVICE:1.0 ${REGISTRY_NAME}.azurecr.io/$SERVICE:1.0
    docker push ${REGISTRY_NAME}.azurecr.io/$SERVICE:1.0
done

echo "Creating container apps environment..."
az containerapp env create \
  --name $ENV_NAME \
  --resource-group $RESOURCE_GROUP \
  --location $REGION

echo "Deploying AuthSvc..."
az containerapp create \
  --name authsvc \
  --resource-group $RESOURCE_GROUP \
  --environment $ENV_NAME \
  --image ${REGISTRY_NAME}.azurecr.io/authsvc:1.0 \
  --target-port 8080 \
  --ingress internal \
  --cpu 0.5 \
  --memory 1Gi \
  --min-replicas 2 \
  --max-replicas 4 \
  --env-vars \
    "PORTAL_SERVICE_TOKEN=your-secret-token-32-chars" \
    "PORTAL_AUDIT_DB_ACCESS=true"

echo "Deploying MailSvc..."
az containerapp create \
  --name mailsvc \
  --resource-group $RESOURCE_GROUP \
  --environment $ENV_NAME \
  --image ${REGISTRY_NAME}.azurecr.io/mailsvc:1.0 \
  --target-port 8080 \
  --ingress internal \
  --cpu 0.5 \
  --memory 1Gi \
  --env-vars \
    "PORTAL_SERVICE_TOKEN=your-secret-token-32-chars" \
    "PORTAL_SMTP_URL=smtps://user:pass@smtp.sendgrid.net:465"

echo "Deploying Portal..."
az containerapp create \
  --name portal \
  --resource-group $RESOURCE_GROUP \
  --environment $ENV_NAME \
  --image ${REGISTRY_NAME}.azurecr.io/portal:1.0 \
  --target-port 8080 \
  --ingress external \
  --cpu 1.0 \
  --memory 2Gi \
  --min-replicas 2 \
  --max-replicas 5

echo "✅ Deployment complete!"
az containerapp show --name portal --resource-group $RESOURCE_GROUP --query properties.configuration.ingress.fqdn
```

Save as `deploy-azure.sh` and run:

```bash
chmod +x deploy-azure.sh
./deploy-azure.sh
```

### View Status

```bash
# List container apps
az containerapp list --resource-group utsu-portal-rg

# Get service URL
az containerapp show --name portal --resource-group utsu-portal-rg --query properties.configuration.ingress.fqdn

# View logs
az containerapp logs show --name authsvc --resource-group utsu-portal-rg --tail 50
```

---

## GCP Cloud Run

### Prerequisites

```bash
# Install gcloud CLI
curl https://sdk.cloud.google.com | bash

# Initialize
gcloud init

# Enable required APIs
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com
```

### Quick Deploy Script

```bash
#!/bin/bash
set -e

PROJECT_ID="utsu-portal-project"
REGION="us-central1"
REGISTRY_REGION="${REGION}-docker.pkg.dev"
REPO_NAME="utsu-repo"

echo "Setting GCP project..."
gcloud config set project $PROJECT_ID

echo "Creating artifact registry..."
gcloud artifacts repositories create $REPO_NAME \
  --repository-format=docker \
  --location=$REGION || true

echo "Configuring Docker auth..."
gcloud auth configure-docker $REGISTRY_REGION

cd /path/to/student-processing-core

echo "Building and pushing images..."
for SERVICE in authsvc mailsvc portal; do
    IMAGE_URL="$REGISTRY_REGION/$PROJECT_ID/$REPO_NAME/$SERVICE:1.0"
    echo "Building $SERVICE → $IMAGE_URL..."
    docker build -t $IMAGE_URL --build-arg SERVICE=$SERVICE -f Dockerfile.microservices .
    docker push $IMAGE_URL
done

echo "Deploying AuthSvc (internal)..."
gcloud run deploy authsvc \
  --image $REGISTRY_REGION/$PROJECT_ID/$REPO_NAME/authsvc:1.0 \
  --platform managed \
  --region $REGION \
  --memory 1Gi \
  --cpu 1 \
  --no-allow-unauthenticated \
  --ingress internal \
  --set-env-vars="PORTAL_SERVICE_TOKEN=your-secret-token-32-chars"

echo "Deploying MailSvc (internal)..."
gcloud run deploy mailsvc \
  --image $REGISTRY_REGION/$PROJECT_ID/$REPO_NAME/mailsvc:1.0 \
  --platform managed \
  --region $REGION \
  --memory 512Mi \
  --cpu 1 \
  --no-allow-unauthenticated \
  --ingress internal \
  --set-env-vars="PORTAL_SERVICE_TOKEN=your-secret-token-32-chars"

echo "Deploying Portal (public)..."
gcloud run deploy portal \
  --image $REGISTRY_REGION/$PROJECT_ID/$REPO_NAME/portal:1.0 \
  --platform managed \
  --region $REGION \
  --memory 2Gi \
  --cpu 2 \
  --allow-unauthenticated \
  --ingress all

echo "✅ Deployment complete!"
gcloud run services describe portal --platform managed --region $REGION --format='value(status.url)'
```

Save as `deploy-gcp.sh` and run:

```bash
chmod +x deploy-gcp.sh
./deploy-gcp.sh
```

### View Status

```bash
# List services
gcloud run services list

# Get service URL
gcloud run services describe portal --region us-central1 --format='value(status.url)'

# View logs
gcloud run services describe portal --region us-central1 --format='value(status.latestReadyRevision.name)' | xargs -I {} gcloud logging read "resource.labels.service_name=portal AND resource.labels.revision_name={}" --limit 50
```

---

## Testing & Verification

### Test Local Deployment (Docker Compose)

```bash
# 1. Create test user
curl -X POST http://localhost:8001/api/auth/signup \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test@example.com",
    "password": "SecurePassword123!",
    "first_name": "Test",
    "last_name": "User"
  }'

# 2. Login
curl -X POST http://localhost:8001/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test@example.com",
    "password": "SecurePassword123!"
  }'
# Response: {"token": "eyJhbGc..."}

# 3. Get current user
TOKEN="eyJhbGc..."
curl -X GET http://localhost:8001/api/me \
  -H "Authorization: Bearer $TOKEN"

# 4. Assign role
SERVICE_TOKEN="your-secret-token-32-chars"
curl -X POST http://localhost:8001/api/rbac/users/user-id-123/roles/admin \
  -H "Authorization: Bearer $SERVICE_TOKEN"

# 5. Check permissions
curl -X GET http://localhost:8001/api/rbac/users/user-id-123/permissions \
  -H "Authorization: Bearer $SERVICE_TOKEN"

# 6. Queue email
curl -X POST http://localhost:8002/mail/queue \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $SERVICE_TOKEN" \
  -d '{
    "to": "test@example.com",
    "subject": "Welcome!",
    "body": "Hello Test User!"
  }'

# 7. View emails (MailHog)
# Go to http://localhost:8025

# 8. Check email status
curl -X GET http://localhost:8002/mail/status/email-id-xyz \
  -H "Authorization: Bearer $SERVICE_TOKEN"
```

### Test Azure Deployment

```bash
# Get portal URL
PORTAL_URL=$(az containerapp show --name portal --resource-group utsu-portal-rg --query properties.configuration.ingress.fqdn -o tsv)

# Test signup
curl -X POST https://$PORTAL_URL/api/auth/signup \
  -H "Content-Type: application/json" \
  -d '{"email": "azure@test.com", "password": "SecurePassword123!"}'

# Check logs
az containerapp logs show --name authsvc --resource-group utsu-portal-rg --tail 100
az containerapp logs show --name mailsvc --resource-group utsu-portal-rg --tail 100
```

### Test GCP Deployment

```bash
# Get portal URL
PORTAL_URL=$(gcloud run services describe portal --region us-central1 --format='value(status.url)')

# Test signup (Cloud Run requires authentication)
curl -X POST $PORTAL_URL/api/auth/signup \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -d '{"email": "gcp@test.com", "password": "SecurePassword123!"}'

# View logs
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=authsvc" --limit 50
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=mailsvc" --limit 50
```

---

## Monitoring & Debugging

### Local (Docker Compose)

```bash
# View all containers
docker-compose ps

# Execute command in container
docker-compose exec authsvc bash

# View container logs with timestamps
docker-compose logs --timestamps -f

# Monitor resource usage
docker stats
```

### Azure

```bash
# Stream live logs
az containerapp logs show --name authsvc --resource-group utsu-portal-rg --stream

# View revision history
az containerapp revision list --name authsvc --resource-group utsu-portal-rg

# Check ingress settings
az containerapp ingress show --name authsvc --resource-group utsu-portal-rg
```

### GCP

```bash
# Stream logs in real-time
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=authsvc" --stream

# Get service details
gcloud run services describe authsvc --region us-central1

# Set up Cloud Monitoring dashboard
gcloud monitoring dashboards create --config-from-file=- < dashboards/cloud-run.json
```

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| **Service won't start** | Check logs: `docker-compose logs authsvc` or `az containerapp logs show` |
| **Can't reach other services** | Verify `--ingress internal` is set, check networking configuration |
| **Database connection fails** | Verify DATABASE_URL env var, check network connectivity |
| **Email not sending** | Check SMTP credentials, verify PORTAL_SMTP_URL is correct |
| **High latency** | Check resource limits (CPU/memory), increase replicas |
| **Authentication fails** | Regenerate PORTAL_SERVICE_TOKEN, ensure it's 32+ chars |

---

## Next Steps

1. ✅ Deploy to local environment first (Docker Compose)
2. ✅ Test all microservices work together
3. ✅ Deploy to cloud (Azure Container Apps or GCP Cloud Run)
4. ✅ Setup monitoring (Datadog, New Relic, or cloud-native)
5. ✅ Configure custom domain & SSL
6. ✅ Setup CI/CD pipeline (GitHub Actions, Cloud Build)

Happy deploying! 🚀
