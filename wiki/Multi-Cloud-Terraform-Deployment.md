# Multi-Cloud Deployment with Terraform

Guide to deploying UTSU Portal to Azure Container Apps, GCP Cloud Run, and AWS Fargate simultaneously using Terraform.

## Prerequisites

1. **Install Terraform** >= 1.0
   ```bash
   # macOS
   brew install terraform
   
   # Linux (Ubuntu/Debian)
   curl -fsSL https://apt.releases.hashicorp.com/gpg | sudo apt-key add -
   sudo apt-add-repository "deb [arch=amd64] https://apt.releases.hashicorp.com $(lsb_release -cs) main"
   sudo apt-get update && sudo apt-get install terraform
   
   # Verify
   terraform version
   ```

2. **Install Cloud CLIs**
   ```bash
   # Azure CLI
   curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
   az login
   
   # Google Cloud CLI
   curl https://sdk.cloud.google.com | bash
   gcloud auth login
   
   # AWS CLI
   curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
   unzip awscliv2.zip && sudo ./aws/install
   aws configure
   ```

3. **Docker Credentials** in all three cloud registries
   ```bash
   # Azure
   az acr login --name utsuacr
   
   # GCP
   gcloud auth configure-docker us-central1-docker.pkg.dev
   
   # AWS ECR
   aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin $ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com
   ```

## Setup

### Step 1: Prepare Terraform Configuration

```bash
cd /home/tony-utsu/Desktop/student-processing-core/infra/terraform

# Copy example variables
cp terraform.tfvars.example terraform.tfvars

# Edit with your values
nano terraform.tfvars
```

**terraform.tfvars:**
```hcl
deployment_targets = ["azure", "gcp", "aws"]
image_tag = "v1.0.0"
environment = "prod"
region = "us-east-1"

# Azure
azure_subscription_id = "your-sub-id"
azure_resource_group = "utsu-portal-rg"

# GCP
gcp_project = "utsu-portal-project"

# AWS
aws_account_id = "123456789012"
```

### Step 2: Terraform Init

```bash
# Initialize Terraform (downloads provider plugins)
terraform init

# Output:
# ✓ Initializing the backend...
# ✓ Initializing provider plugins...
# ✓ Terraform has been successfully configured!
```

### Step 3: Review Plan

```bash
# See what Terraform will create
terraform plan -out=tfplan

# Output shows resources to be created for all 3 clouds:
# Plan: 157 to add, 0 to change, 0 to destroy.
```

### Step 4: Deploy

```bash
# Apply the plan (creates resources in all clouds)
terraform apply tfplan

# This will:
# 1. Create Azure Container Apps environment + services + database
# 2. Create GCP Cloud Run services + Cloud SQL + service accounts
# 3. Create AWS Fargate cluster + ALB + RDS + ECS services

# Takes ~10-15 minutes for all resources to be ready

# Output:
# Apply complete! Resources: 157 added, 0 changed, 0 destroyed.
```

### Step 5: Get Endpoints

```bash
# Display all service URLs
terraform output endpoints

# Output:
# endpoints = {
#   azure = {
#     portal  = "portal-dev.happysand-1a2b3c4d.eastus.azurecontainerapps.io"
#     authsvc = "http://authsvc-dev.internal.azurecontainerapps.io"
#     mailsvc = "http://mailsvc-dev.internal.azurecontainerapps.io"
#   }
#   gcp = {
#     portal  = "https://portal-utsu-portal-project.run.app"
#     authsvc = "https://authsvc-utsu-portal-project.run.app"
#     mailsvc = "https://mailsvc-utsu-portal-project.run.app"
#   }
#   aws = {
#     portal  = "http://utsu-portal-alb-1234567890.us-east-1.elb.amazonaws.com"
#     authsvc = "http://authsvc-ecs.us-east-1.compute.internal"
#     mailsvc = "http://mailsvc-ecs.us-east-1.compute.internal"
#   }
# }
```

## Testing Multi-Cloud Deployment

### Test Azure Deployment

```bash
AZURE_URL=$(terraform output -json endpoints | jq -r '.value.azure.portal')

curl -v https://$AZURE_URL/health
# Should return 200 OK
```

### Test GCP Deployment

```bash
GCP_URL=$(terraform output -json endpoints | jq -r '.value.gcp.portal')

curl -v $GCP_URL/health
# Should return 200 OK
```

### Test AWS Deployment

```bash
AWS_URL=$(terraform output -json endpoints | jq -r '.value.aws.portal')

curl -v http://$AWS_URL/health
# Should return 200 OK
```

### Test Service Communication (Azure)

```bash
# From authsvc pod, call mailsvc via DAPR
curl http://localhost:3500/v1.0/invoke/mailsvc/method/health

# DAPR handles:
# - Service discovery
# - Automatic mTLS
# - Retries
# - Load balancing
```

### Test Service Communication (GCP)

```bash
# From authsvc, call mailsvc with IAM token
MAILSVC_URL=$(terraform output -json endpoints | jq -r '.value.gcp.mailsvc')
TOKEN=$(gcloud auth application-default print-access-token)

curl -H "Authorization: Bearer $TOKEN" $MAILSVC_URL/health
```

### Test Service Communication (AWS)

```bash
# Services on same ECS cluster communicate via service discovery
# authsvc can reach mailsvc via:
curl http://mailsvc:8000/health
```

## Managing Multi-Cloud Deployment

### View Current State

```bash
terraform show

# Shows all resources created in all clouds
```

### Update Configuration

```bash
# Change image tag
terraform apply -var="image_tag=v1.0.1"

# Roll out across all clouds simultaneously
```

### Scale Services

```bash
# Increase CPU for all services
terraform apply -var="cpu=1"

# Deployed to all clouds in parallel
```

### Destroy Everything

```bash
# Remove all resources from all clouds
terraform destroy

# Removes:
# - All Azure Container Apps
# - All GCP Cloud Run services
# - All AWS Fargate clusters and RDS databases
```

### Destroy Specific Cloud

```bash
# Remove only AWS deployment
terraform apply -var='deployment_targets=["azure", "gcp"]'

# Keeps Azure and GCP running, removes AWS
```

## Cost Tracking

### Azure Container Apps

```bash
# Estimated monthly cost
az containerapp billing show --resource-group utsu-portal-rg
```

### GCP Cloud Run

```bash
# View Cloud Run costs
gcloud billing accounts list
gcloud billing budgets list

# Typical: $0 for dev, $50-200 for prod with traffic
```

### AWS Fargate

```bash
# View Fargate costs
aws ce get-cost-and-usage \
  --time-period Start=2024-01-01,End=2024-01-31 \
  --granularity MONTHLY \
  --metrics BlendedCost
```

## Troubleshooting

### Services Not Starting

```bash
# Check Azure Container Apps logs
az containerapp logs show --name authsvc --resource-group utsu-portal-rg

# Check GCP Cloud Run logs
gcloud run services describe authsvc --region us-central1 --format='value(status.url)'
gcloud logging read "resource.type=cloud_run_revision"

# Check AWS ECS logs
aws logs tail /ecs/utsu-portal-dev --follow
```

### Database Connection Failed

```bash
# Azure: Check PostgreSQL firewall rules
az postgres flexible-server firewall-rule list --resource-group utsu-portal-rg --name utsu-portal-db-dev

# GCP: Check Cloud SQL proxy
gcloud sql connect utsu-portal-db --user=utsuadmin

# AWS: Check security groups
aws ec2 describe-security-groups --filters "Name=group-name,Values=utsu-portal-*"
```

### Service-to-Service Communication Failing

```bash
# Azure: Verify DAPR sidecars running
kubectl get pods -n dapr-system

# GCP: Verify IAM bindings
gcloud run services get-iam-policy authsvc

# AWS: Verify security group ingress rules
aws ec2 describe-security-groups --group-ids sg-xxx
```

## Monitoring Deployments

### Real-time Status

```bash
# Watch resource creation
watch -n 5 'terraform show | grep -E "status|state"'
```

### Cost Monitoring

```bash
# Set up cost alerts in each cloud
# Azure: Cost Management + Billing > Alerts
# GCP: Billing > Budgets > Create Budget
# AWS: AWS Budgets > Create Budget
```

### Health Checks

```bash
# Create health check script
cat > check-health.sh << 'EOF'
#!/bin/bash

echo "=== Multi-Cloud Health Check ==="

# Azure
echo "Azure Portal: $(curl -s https://$(terraform output -json endpoints | jq -r '.value.azure.portal')/health | jq .status)"

# GCP
echo "GCP Portal: $(curl -s $(terraform output -json endpoints | jq -r '.value.gcp.portal')/health | jq .status)"

# AWS
echo "AWS Portal: $(curl -s http://$(terraform output -json endpoints | jq -r '.value.aws.portal')/health | jq .status)"
EOF

chmod +x check-health.sh
./check-health.sh
```

## Next Steps

1. **Set up Terraform Remote State** (prevent state drift)
   ```bash
   # Store state in S3 for team collaboration
   # Update main.tf backend configuration
   ```

2. **Implement GitOps** (automatic deployment on git push)
   ```bash
   # Add GitHub Actions to run terraform apply on merge
   ```

3. **Add Monitoring/Alerting**
   ```bash
   # Integrate with Prometheus + Grafana
   # Set up Datadog or New Relic cross-cloud monitoring
   ```

4. **Database Replication** (sync across clouds)
   ```bash
   # Set up bi-directional PostgreSQL replication
   # Or use application-level sync (Kafka, Pub/Sub)
   ```

5. **Load Testing**
   ```bash
   # Test deployment with k6 or Locust
   # Verify scaling works on all clouds
   ```

---

## Quick Reference

| Task | Command |
|------|---------|
| Initialize | `terraform init` |
| Validate | `terraform validate` |
| Plan | `terraform plan -out=tfplan` |
| Apply | `terraform apply tfplan` |
| Destroy | `terraform destroy` |
| View endpoints | `terraform output endpoints` |
| Change target | `terraform apply -var='deployment_targets=["azure"]'` |
| View state | `terraform show` |
| Refresh state | `terraform refresh` |
| Import resource | `terraform import aws_ecs_service.main module.aws[0].aws_ecs_service.main[\"portal\"]` |

---

That's it! Your infrastructure is now defined as code and deployable to any cloud. 🚀

