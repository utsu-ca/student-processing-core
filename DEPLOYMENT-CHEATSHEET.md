# Deployment Cheatsheet

## Local Development (Docker Compose)

```bash
# Start all services
docker-compose up

# Stop all services
docker-compose down

# View logs
docker-compose logs -f [SERVICE]
docker-compose logs -f authsvc
docker-compose logs -f mailsvc
docker-compose logs -f portal

# Restart a service
docker-compose restart authsvc

# Execute command in container
docker-compose exec authsvc bash
docker-compose exec mailsvc python -c "import os; print(os.environ['DATABASE_URL'])"

# View resource usage
docker stats

# Clean volumes (WARNING: deletes data)
docker-compose down -v
```

## Service URLs (Local)

| Service | URL | Port |
|---------|-----|------|
| Portal | http://localhost:8000 | 8000 |
| AuthSvc | http://localhost:8001 | 8001 |
| MailSvc | http://localhost:8002 | 8002 |
| NGINX | http://localhost:80 | 80/443 |
| MailHog | http://localhost:8025 | 8025 |
| PostgreSQL (Auth) | localhost:5432 | 5432 |
| PostgreSQL (Mail) | localhost:5433 | 5433 |
| Redis | localhost:6379 | 6379 |

## Common API Tests

```bash
# Signup
curl -X POST http://localhost:8001/api/auth/signup \
  -H "Content-Type: application/json" \
  -d '{"email": "test@example.com", "password": "SecurePass123!", "first_name": "Test", "last_name": "User"}'

# Login
curl -X POST http://localhost:8001/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "test@example.com", "password": "SecurePass123!"}'
# Returns: {"token": "eyJhbGc..."}

# Get current user
TOKEN="eyJhbGc..."
curl -X GET http://localhost:8001/api/me \
  -H "Authorization: Bearer $TOKEN"

# List roles
SERVICE_TOKEN="your-secret-token-32-chars"
curl -X GET http://localhost:8001/api/rbac/roles \
  -H "Authorization: Bearer $SERVICE_TOKEN"

# Assign role
curl -X POST http://localhost:8001/api/rbac/users/user-id-123/roles/admin \
  -H "Authorization: Bearer $SERVICE_TOKEN"

# Check permissions
curl -X GET http://localhost:8001/api/rbac/users/user-id-123/permissions \
  -H "Authorization: Bearer $SERVICE_TOKEN"

# Queue email
curl -X POST http://localhost:8002/mail/queue \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $SERVICE_TOKEN" \
  -d '{"to": "test@example.com", "subject": "Test", "body": "Hello"}'

# Check email status
MAIL_ID="email-id-from-queue-response"
curl -X GET http://localhost:8002/mail/status/$MAIL_ID \
  -H "Authorization: Bearer $SERVICE_TOKEN"

# List pending emails
curl -X GET http://localhost:8002/mail/pending \
  -H "Authorization: Bearer $SERVICE_TOKEN"
```

## Azure Deployment

```bash
# Create resource group
az group create --name utsu-portal-rg --location eastus

# Create container registry
az acr create --resource-group utsu-portal-rg --name utsuacr --sku Basic

# Login to registry
az acr login --name utsuacr

# Build and push images
docker build -t authsvc:1.0 --build-arg SERVICE=authsvc -f Dockerfile.microservices .
docker tag authsvc:1.0 utsuacr.azurecr.io/authsvc:1.0
docker push utsuacr.azurecr.io/authsvc:1.0

# List container apps
az containerapp list --resource-group utsu-portal-rg

# Get service URL
az containerapp show --name portal --resource-group utsu-portal-rg \
  --query properties.configuration.ingress.fqdn -o tsv

# View logs
az containerapp logs show --name authsvc --resource-group utsu-portal-rg --tail 50
```

## GCP Deployment

```bash
# Set project
gcloud config set project utsu-portal-project

# Enable APIs
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com

# Create artifact repository
gcloud artifacts repositories create utsu-repo \
  --repository-format=docker \
  --location=us-central1

# Configure Docker auth
gcloud auth configure-docker us-central1-docker.pkg.dev

# Build and push
docker build -t us-central1-docker.pkg.dev/utsu-portal-project/utsu-repo/authsvc:1.0 \
  --build-arg SERVICE=authsvc -f Dockerfile.microservices .
docker push us-central1-docker.pkg.dev/utsu-portal-project/utsu-repo/authsvc:1.0

# Deploy to Cloud Run
gcloud run deploy authsvc \
  --image us-central1-docker.pkg.dev/utsu-portal-project/utsu-repo/authsvc:1.0 \
  --platform managed \
  --region us-central1 \
  --memory 1Gi \
  --no-allow-unauthenticated

# List services
gcloud run services list

# Get service URL
gcloud run services describe authsvc --region us-central1 --format='value(status.url)'

# View logs (streaming)
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=authsvc" --stream
```

## Kubernetes Deployment

```bash
# Create namespace
kubectl create namespace utsu-portal

# Apply all manifests
kubectl apply -f deploy/k8s/

# Check deployment status
kubectl get deployments -n utsu-portal
kubectl get pods -n utsu-portal
kubectl get services -n utsu-portal

# View logs
kubectl logs -f deployment/authsvc -n utsu-portal
kubectl logs -f deployment/mailsvc -n utsu-portal

# Port forward to test
kubectl port-forward -n utsu-portal svc/authsvc 8001:8080
# Now: curl http://localhost:8001/docs

# Scale deployment
kubectl scale deployment authsvc --replicas=3 -n utsu-portal

# Check ingress
kubectl get ingress -n utsu-portal
kubectl describe ingress portal -n utsu-portal

# Update image
kubectl set image deployment/authsvc authsvc=utsuacr.azurecr.io/authsvc:1.1 -n utsu-portal

# Delete deployment
kubectl delete namespace utsu-portal
```

## Environment Variables

### AuthSvc
```
PORTAL_SERVICE_TOKEN=your-32-character-minimum-token
PORTAL_AUDIT_DB_ACCESS=true
PORTAL_MAIL_ENDPOINT=http://mailsvc:8080
DATABASE_URL=postgresql://user:pass@postgres:5432/authdb
REDIS_URL=redis://redis:6379
```

### MailSvc
```
PORTAL_SERVICE_TOKEN=your-32-character-minimum-token
PORTAL_SMTP_URL=smtps://user:pass@smtp.sendgrid.net:465
PORTAL_MAIL_FROM=noreply@example.org
DATABASE_URL=postgresql://user:pass@postgres:5432/maildb
```

### Portal
```
PORTAL_SERVICE_TOKEN=your-32-character-minimum-token
PORTAL_AUTH_ENDPOINT=http://authsvc:8080
PORTAL_MAIL_ENDPOINT=http://mailsvc:8080
DATABASE_URL=postgresql://user:pass@postgres:5432/authdb
REDIS_URL=redis://redis:6379
```

## Database Connection Strings

### PostgreSQL Local
```
postgresql://authuser:authpass@localhost:5432/authdb
postgresql://mailuser:mailpass@localhost:5433/maildb
```

### PostgreSQL Azure
```
postgresql://username@servername:password@servername.postgres.database.azure.com:5432/databasename
```

### PostgreSQL GCP
```
postgresql://username:password@cloudsqlproxy:5432/databasename
# Using Cloud SQL Proxy sidecar
```

### SQLite (Development)
```
sqlite:///./emails.db
sqlite:////tmp/auth.db
```

## Troubleshooting Commands

```bash
# Check if service is running
curl http://localhost:8001/docs

# Test service-to-service communication
curl -H "Authorization: Bearer $SERVICE_TOKEN" http://authsvc:8080/internal/ping

# Check database connection
docker-compose exec authsvc python -c "import psycopg2; psycopg2.connect('postgresql://...')"

# View environment variables
docker-compose exec authsvc env | grep PORTAL

# Check network connectivity
docker-compose exec authsvc curl http://mailsvc:8080/docs

# Inspect running container
docker-compose exec authsvc bash
# Inside container: ps aux, ls -la, cat /app/src/...

# View process logs in real-time
docker-compose logs --timestamps -f authsvc

# Test reverse proxy
curl -v http://localhost:80/api/auth/
# Should show routing to authsvc

# Check email queue (MailHog)
curl http://localhost:8025/api/v2/messages
```

## Performance Testing

```bash
# Install locust
pip install locust

# Run load test
locust -f tests/load_test.py --host=http://localhost:8000 -u 100 -r 10

# Monitor resource usage during test
watch docker stats

# View latency percentiles
curl http://localhost:8025/api/v2/stats  # MailHog stats
```

## Backup & Restore

```bash
# Backup PostgreSQL (Docker)
docker-compose exec postgres-auth pg_dump -U authuser authdb > backup.sql

# Restore PostgreSQL
docker-compose exec -T postgres-auth psql -U authuser authdb < backup.sql

# Backup volumes
docker run --rm -v utsu-portal-core_auth-db-data:/data -v $(pwd):/backup \
  alpine tar czf /backup/auth-db-backup.tar.gz -C /data .

# Restore volumes
docker run --rm -v utsu-portal-core_auth-db-data:/data -v $(pwd):/backup \
  alpine tar xzf /backup/auth-db-backup.tar.gz -C /data
```

## Debugging

```bash
# Enable verbose logging
RUST_LOG=debug docker-compose up

# Attach debugger
# Add to container: python -m pdb -c continue
docker-compose exec authsvc python -m pdb app.py

# View network traffic
docker-compose exec authsvc tcpdump -i eth0 -A

# Check DNS resolution
docker-compose exec authsvc nslookup mailsvc
docker-compose exec authsvc getent hosts mailsvc

# Monitor system resources
docker stats --no-stream

# Profile memory usage
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
  calicoctl/calicoctl node diags
```

## Quick Reference: File Locations

| Component | Location |
|-----------|----------|
| AuthSvc | `src/utsu_portal/authsvc/__init__.py` |
| RBAC Module | `src/utsu_portal/authsvc/rbac.py` |
| MailSvc | `src/utsu_portal/mailsvc/__init__.py` |
| Docker Compose | `docker-compose.yml` |
| NGINX Config | `nginx.conf` |
| K8s AuthSvc | `deploy/k8s/authsvc.yaml` |
| K8s MailSvc | `deploy/k8s/mailsvc.yaml` |
| K8s Ingress | `deploy/k8s/ingress-with-reverse-proxy.yaml` |
| Architecture Docs | `wiki/Microservice-Architecture.md` |
| Cloud Docs | `wiki/Cloud-Deployment-Options.md` |
| Quick Start | `wiki/Deployment-Quick-Start.md` |

## One-Liners

```bash
# Start everything, wait for health checks
docker-compose up --wait

# Test all services at once
for svc in authsvc mailsvc portal; do curl http://localhost:800$((${svc: -1} == 'svc' ? 1 : 2))/docs && echo "$svc OK"; done

# Check all environment variables
docker-compose config | grep environment

# Generate service token
openssl rand -hex 32

# Test JWT decode
jwt decode $TOKEN  # requires jwt CLI tool

# Count running containers
docker-compose ps | grep running | wc -l

# Export logs to file
docker-compose logs > all-logs.txt

# Find all TODO comments in code
grep -r "TODO\|FIXME" src/utsu_portal/

# Format Python code
black src/utsu_portal/

# Type check Python
mypy src/utsu_portal/ --ignore-missing-imports
```

---

**Pro Tip:** Bookmark this cheatsheet for quick reference during development and deployment! 📌
