# Azure Container Apps Module
# Deploys services to Azure Container Apps with DAPR

variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
}

variable "environment_name" {
  type = string
}

variable "image_tag" {
  type = string
}

variable "services" {
  type = list(string)
}

variable "cpu" {
  type = string
}

variable "memory" {
  type = string
}

# Data source for resource group
data "azurerm_resource_group" "rg" {
  name = var.resource_group_name
}

# Container App Environment
resource "azurerm_container_app_environment" "env" {
  name                = "utsu-portal-env-${var.environment_name}"
  location            = data.azurerm_resource_group.rg.location
  resource_group_name = data.azurerm_resource_group.rg.name
}

# Container Apps for each service
resource "azurerm_container_app" "services" {
  for_each = toset(var.services)
  
  name                         = "${each.value}-${var.environment_name}"
  container_app_environment_id = azurerm_container_app_environment.env.id
  resource_group_name          = data.azurerm_resource_group.rg.name
  revision_mode                = "Single"

  template {
    container {
      name   = each.value
      image  = "utsuacr.azurecr.io/${each.value}:${var.image_tag}"
      cpu    = var.cpu
      memory = var.memory

      env {
        name  = "PORTAL_SERVICE_TOKEN"
        value = random_password.service_token.result
      }

      env {
        name  = "DATABASE_URL"
        value = "postgresql://${azurerm_postgresql_flexible_server.db[0].administrator_login}:${random_password.db_password.result}@${azurerm_postgresql_flexible_server.db[0].fqdn}:5432/utsudb"
      }
    }
  }

  ingress {
    allow_insecure_connections = false
    external_enabled           = true
    target_port                = 8000
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  dapr {
    enabled = true
    app_id  = each.value
  }
}

# PostgreSQL Database
resource "azurerm_postgresql_flexible_server" "db" {
  count               = 1
  name                = "utsu-portal-db-${var.environment_name}"
  location            = data.azurerm_resource_group.rg.location
  resource_group_name = data.azurerm_resource_group.rg.name

  administrator_login    = "utsuadmin"
  administrator_password = random_password.db_password.result

  sku_name   = "B_Standard_B1ms"
  storage_mb = 32768
  version    = "14"
}

# Random secrets
resource "random_password" "service_token" {
  length  = 32
  special = true
}

resource "random_password" "db_password" {
  length  = 24
  special = true
}

# Outputs
output "portal_url" {
  value = azurerm_container_app.services["portal"].ingress[0].fqdn
}

output "authsvc_url" {
  value = "http://authsvc-${var.environment_name}.internal.azurecontainerapps.io"
}

output "mailsvc_url" {
  value = "http://mailsvc-${var.environment_name}.internal.azurecontainerapps.io"
}
