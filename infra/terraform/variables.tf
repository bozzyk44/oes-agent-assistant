variable "project_id" {
  description = "Идентификатор проекта Cloud.ru Evolution."
  type        = string
}

variable "ssh_public_key" {
  description = "Публичный SSH-ключ для входа на сервер: одна строка в формате OpenSSH."
  type        = string

  validation {
    condition     = can(regex("^(ssh-|ecdsa-|sk-)[^\\n]+$", trimspace(var.ssh_public_key)))
    error_message = "Ожидается один публичный ключ в формате OpenSSH одной строкой."
  }
}

variable "name" {
  description = "Имя стенда; префикс имён всех ресурсов."
  type        = string
  default     = "oes-stand"
}

variable "zone" {
  description = "Зона доступности."
  type        = string
  default     = "ru.AZ-1"
}

variable "flavor" {
  description = "Конфигурация сервера. Лимиты — в docs/architecture/INFRASTRUCTURE.md."
  type        = string
  default     = "lowcost10-2-4"
}

variable "image" {
  description = "Имя образа операционной системы."
  type        = string
  default     = "Ubuntu-24.04"
}

variable "disk_size_gb" {
  description = "Размер загрузочного диска, ГБ."
  type        = number
  default     = 30
}

variable "disk_type" {
  description = "Тип диска."
  type        = string
  default     = "SSD"
}

variable "login" {
  description = "Имя пользователя на сервере."
  type        = string
  default     = "oes"
}

variable "ssh_cidr" {
  description = "Откуда разрешён SSH. По умолчанию отовсюду: вход только по ключу."
  type        = string
  default     = "0.0.0.0/0"

  validation {
    condition     = can(cidrhost(var.ssh_cidr, 0))
    error_message = "Ожидается адрес сети в формате CIDR, например 203.0.113.7/32."
  }
}

variable "service_cidr" {
  description = "Откуда доступен порт сервиса. По умолчанию порт закрыт: у сервиса нет аутентификации, заголовок X-User-ID считается доверенным."
  type        = string
  default     = null

  validation {
    condition     = var.service_cidr == null || can(cidrhost(var.service_cidr, 0))
    error_message = "Ожидается адрес сети в формате CIDR, например 203.0.113.7/32."
  }
}

variable "web_open" {
  description = "Открыть порты 80 и 443 для обратного прокси (профиль compose proxy). Отовсюду: Let's Encrypt проверяет имя со своих адресов; вход закрыт паролем на прокси."
  type        = bool
  default     = false
}

variable "service_port" {
  description = "Порт сервиса ассистента."
  type        = number
  default     = 8000

  validation {
    condition     = var.service_port >= 1024 && var.service_port <= 65535
    error_message = "Порт сервиса — от 1024 до 65535."
  }
}
