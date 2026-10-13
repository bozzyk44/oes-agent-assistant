variable "project_id" {
  description = "Идентификатор проекта Cloud.ru Evolution."
  type        = string
}

variable "ssh_public_key" {
  description = "Публичный SSH-ключ для входа на сервер."
  type        = string
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
}

variable "service_port" {
  description = "Порт сервиса ассистента, открытый наружу."
  type        = number
  default     = 8000
}
