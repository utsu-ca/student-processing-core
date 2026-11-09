# GCP Cloud Run Module

variable "project_id" {
  type = string
}

variable "region" {
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

# Service Accounts for inter-service auth
resource "google_service_account" "services" {
  for_each = toset(var.services)
  
  account_id   = "${each.value}-sa"
  display_name = "${each.value} service account"
}

# Grant services permission to call other services
resource "google_cloud_run_service_iam_member" "invoker" {
  for_each = toset(var.services)
  
  service  = google_cloud_run_service.services[each.key].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.services[each.key].email}"
}

# Cloud Run Services
resource "google_cloud_run_service" "services" {
  for_each = toset(var.services)
  
  name     = "${each.value}-${var.project_id}"
  location = var.region

  template {
    spec {
      containers {
        image = "us-central1-docker.pkg.dev/${var.project_id}/utsu-repo/${each.value}:${var.image_tag}"

        env {
          name  = "PORTAL_SERVICE_TOKEN"
          value = random_password.service_token.result
        }

        env {
          name  = "DATABASE_URL"
          value = "postgresql://${google_sql_database_instance.main.connection_name}/${google_sql_database.db.name}"
        }

        resources {
          limits = {
            cpu    = var.cpu
            memory = "${var.memory}Gi"
          }
        }
      }

      service_account_name = google_service_account.services[each.key].email
    }
  }

  traffic {
    percent         = 100
    latest_revision = true
  }

  autogenerate_revision_name = true
  depends_on                 = [google_cloud_run_service_iam_member.invoker]
}

# Cloud SQL (PostgreSQL)
resource "google_sql_database_instance" "main" {
  name             = "utsu-portal-db"
  database_version = "POSTGRES_14"
  region           = var.region

  settings {
    tier = "db-f1-micro"
    
    database_flags {
      name  = "cloudsql_iam_authentication"
      value = "on"
    }
  }

  deletion_protection = false
}

resource "google_sql_database" "db" {
  name     = "utsudb"
  instance = google_sql_database_instance.main.name
}

resource "random_password" "service_token" {
  length  = 32
  special = true
}

# Outputs
output "portal_url" {
  value = google_cloud_run_service.services["portal"].status[0].url
}

output "authsvc_url" {
  value = google_cloud_run_service.services["authsvc"].status[0].url
}

output "mailsvc_url" {
  value = google_cloud_run_service.services["mailsvc"].status[0].url
}
