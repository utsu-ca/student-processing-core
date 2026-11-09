# Multi-Cloud Terraform Configuration
# Deploy to Azure, GCP, and AWS with single configuration

terraform {
  required_version = ">= 1.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# ===== VARIABLES =====
variable "deployment_targets" {
  description = "Which clouds to deploy to: azure, gcp, aws"
  type        = set(string)
  default     = ["azure", "gcp", "aws"]
}

variable "image_tag" {
  description = "Docker image tag to deploy"
  type        = string
}

variable "environment" {
  description = "Environment: dev, staging, prod"
  type        = string
  default     = "dev"
}

variable "region" {
  description = "Primary deployment region"
  type        = string
  default     = "us-east-1"
}

variable "cpu" {
  type    = string
  default = "0.5"
}

variable "memory" {
  type    = string
  default = "1"
}

# Azure variables
variable "azure_subscription_id" {
  type      = string
  sensitive = true
}

variable "azure_resource_group" {
  type = string
}

# GCP variables
variable "gcp_project" {
  type = string
}

# AWS variables
variable "aws_account_id" {
  type = string
}

# ===== LOCALS =====

locals {
  deploy_to_azure = contains(var.deployment_targets, "azure")
  deploy_to_gcp   = contains(var.deployment_targets, "gcp")
  deploy_to_aws   = contains(var.deployment_targets, "aws")
  
  services = ["portal", "authsvc", "mailsvc"]
}

# ===== AZURE CONTAINER APPS =====

provider "azurerm" {
  features {}
  subscription_id = var.azure_subscription_id
}

module "azure" {
  count = local.deploy_to_azure ? 1 : 0
  
  source = "./azure"
  
  resource_group_name = var.azure_resource_group
  location            = "eastus"
  environment_name    = var.environment
  image_tag           = var.image_tag
  services            = local.services
  cpu                 = var.cpu
  memory              = var.memory
}

# ===== GCP CLOUD RUN =====

provider "google" {
  project = var.gcp_project
  region  = var.region
}

module "gcp" {
  count = local.deploy_to_gcp ? 1 : 0
  
  source = "./gcp"
  
  project_id = var.gcp_project
  region     = var.region
  image_tag  = var.image_tag
  services   = local.services
  cpu        = var.cpu
  memory     = var.memory
}

# ===== AWS FARGATE =====

provider "aws" {
  region = var.region
}

module "aws" {
  count = local.deploy_to_aws ? 1 : 0
  
  source = "./aws"
  
  region           = var.region
  cluster_name     = "utsu-portal-${var.environment}"
  image_tag        = var.image_tag
  services         = local.services
  cpu              = var.cpu
  memory           = var.memory
  aws_account_id   = var.aws_account_id
}

# ===== OUTPUTS =====

output "endpoints" {
  description = "Service endpoints from deployed clouds"
  value = {
    azure = local.deploy_to_azure ? {
      portal  = try(module.azure[0].portal_url, null)
      authsvc = try(module.azure[0].authsvc_url, null)
      mailsvc = try(module.azure[0].mailsvc_url, null)
    } : null
    
    gcp = local.deploy_to_gcp ? {
      portal  = try(module.gcp[0].portal_url, null)
      authsvc = try(module.gcp[0].authsvc_url, null)
      mailsvc = try(module.gcp[0].mailsvc_url, null)
    } : null
    
    aws = local.deploy_to_aws ? {
      portal  = try(module.aws[0].portal_url, null)
      authsvc = try(module.aws[0].authsvc_url, null)
      mailsvc = try(module.aws[0].mailsvc_url, null)
    } : null
  }
  sensitive = false
}

output "deployed_to" {
  description = "Clouds deployed to"
  value       = var.deployment_targets
}
