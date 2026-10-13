# Стенд: один сервер с публичным IP. Основание — решение 9 в docs/architecture/DECISIONS.md.

data "cloudru_evolution_compute_image_collection" "all" {
  project_id = var.project_id
  page_size  = 500
}

data "cloudru_evolution_compute_subnet_collection" "all" {
  project_id = var.project_id
  page_size  = 100
}

locals {
  images  = [for i in data.cloudru_evolution_compute_image_collection.all.images : i if i.name == var.image]
  subnets = [for s in data.cloudru_evolution_compute_subnet_collection.all.subnets : s if s.default && s.zone.name == var.zone]

  cloud_init = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    hostname = var.name
    login    = var.login
    # Строка в кавычках: комментарий ключа с двоеточием не сломает YAML.
    ssh_public_key_yaml = jsonencode(trimspace(var.ssh_public_key))
  })
}

# Наружу открыт SSH; порт сервиса — только если задан service_cidr. Заглушка ТОиР, PostgreSQL
# и наблюдаемость в группу безопасности не попадают. Docker публикует порты в обход экрана
# на самом сервере, поэтому группа безопасности — единственный внешний барьер.
resource "cloudru_evolution_compute_security_group" "stand" {
  project_id  = var.project_id
  name        = var.name
  description = "Стенд ассистента: SSH и порт сервиса"
  zone        = { name = var.zone }
}

resource "cloudru_evolution_compute_security_group_rule" "ssh" {
  security_group_id = cloudru_evolution_compute_security_group.stand.id
  description       = "SSH"
  direction         = "TRAFFIC_DIRECTION_INGRESS"
  ether_type        = "ETHER_TYPE_IPV4"
  ip_protocol       = "IP_PROTOCOL_TCP"
  port_range        = "22:22" # порт задаётся диапазоном: с одиночным значением apply падает
  remote_ip_prefix  = var.ssh_cidr
}

resource "cloudru_evolution_compute_security_group_rule" "service" {
  count = var.service_cidr == null ? 0 : 1

  security_group_id = cloudru_evolution_compute_security_group.stand.id
  description       = "Сервис ассистента"
  direction         = "TRAFFIC_DIRECTION_INGRESS"
  ether_type        = "ETHER_TYPE_IPV4"
  ip_protocol       = "IP_PROTOCOL_TCP"
  port_range        = "${var.service_port}:${var.service_port}"
  remote_ip_prefix  = var.service_cidr
}

resource "cloudru_evolution_compute_security_group_rule" "egress" {
  security_group_id = cloudru_evolution_compute_security_group.stand.id
  description       = "Исходящий трафик: модель, образы контейнеров, пакеты"
  direction         = "TRAFFIC_DIRECTION_EGRESS"
  ether_type        = "ETHER_TYPE_IPV4"
  ip_protocol       = "IP_PROTOCOL_ANY"
  port_range        = "any"
  remote_ip_prefix  = "0.0.0.0/0"
}

resource "cloudru_evolution_compute_disk" "boot" {
  project_id = var.project_id
  name       = "${var.name}-boot"
  zone       = { name = var.zone }
  size       = var.disk_size_gb
  bootable   = true
  disk_type  = { name = var.disk_type }
  image      = { id = one(local.images).id }

  lifecycle {
    # Обновление образа провайдером не должно пересоздавать диск работающего стенда.
    ignore_changes = [image]

    precondition {
      condition     = length(local.images) == 1
      error_message = "Образ с именем из переменной image должен быть ровно один; найдено ${length(local.images)}."
    }
  }
}

resource "cloudru_evolution_compute_interface" "main" {
  project_id                 = var.project_id
  name                       = "${var.name}-if"
  zone                       = { name = var.zone }
  type                       = "INTERFACE_TYPE_REGULAR"
  interface_security_enabled = true
  subnet                     = { id = one(local.subnets).id }
  security_groups            = [{ id = cloudru_evolution_compute_security_group.stand.id }]

  lifecycle {
    precondition {
      condition     = length(local.subnets) == 1
      error_message = "В зоне из переменной zone должна быть ровно одна подсеть по умолчанию; найдено ${length(local.subnets)}."
    }
  }
}

resource "cloudru_evolution_compute_external_ip" "main" {
  project_id        = var.project_id
  name              = "${var.name}-ip"
  zone              = { name = var.zone }
  network_interface = { id = cloudru_evolution_compute_interface.main.id }
}

resource "cloudru_evolution_compute_vm" "stand" {
  project_id  = var.project_id
  name        = var.name
  description = "Стенд ассистента сотрудника"
  zone        = { name = var.zone }
  flavor      = { name = var.flavor }

  disks              = [{ id = cloudru_evolution_compute_disk.boot.id }]
  network_interfaces = [{ id = cloudru_evolution_compute_interface.main.id }]

  # Пользователь, ключ и имя хоста задаются в cloud-init. Метаданные образа передавать нельзя:
  # API принимает либо их, либо cloud-init.
  cloud_init_userdata = base64encode(local.cloud_init)

  # Первая настройка ставит пакеты из интернета: к этому моменту у сервера должны быть
  # публичный адрес и правило исходящего трафика.
  depends_on = [
    cloudru_evolution_compute_external_ip.main,
    cloudru_evolution_compute_security_group_rule.ssh,
    cloudru_evolution_compute_security_group_rule.egress,
  ]

  lifecycle {
    # cloud-init выполняется один раз. Смена ключа или имени пользователя не должна
    # пересоздавать сервер: такие правки делаются на сервере или пересозданием стенда.
    ignore_changes = [cloud_init_userdata]
  }
}
