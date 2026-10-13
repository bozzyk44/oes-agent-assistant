terraform {
  required_version = ">= 1.6"

  required_providers {
    cloudru = {
      source  = "cloudru/cloud"
      version = "2.1.4"
    }
  }
}

# Ключ сервисного аккаунта провайдер берёт из окружения: AUTH_KEY_ID и AUTH_SECRET.
# В конфигурации и в состоянии их нет.
provider "cloudru" {
  project_id = var.project_id
}
