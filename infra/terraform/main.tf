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
  image  = one([for i in data.cloudru_evolution_compute_image_collection.all.images : i if i.name == var.image])
  subnet = one([for s in data.cloudru_evolution_compute_subnet_collection.all.subnets : s if s.default && s.zone.name == var.zone])

  cloud_init = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    hostname       = var.name
    login          = var.login
    ssh_public_key = var.ssh_public_key
  })
}

# Наружу открыты только SSH и порт сервиса. Заглушка ТОиР, PostgreSQL и наблюдаемость
# слушают внутри сервера и в группу безопасности не попадают.
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
  port_range        = "22:22" # провайдер принимает только диапазон
  remote_ip_prefix  = var.ssh_cidr
}

resource "cloudru_evolution_compute_security_group_rule" "service" {
  security_group_id = cloudru_evolution_compute_security_group.stand.id
  description       = "Сервис ассистента"
  direction         = "TRAFFIC_DIRECTION_INGRESS"
  ether_type        = "ETHER_TYPE_IPV4"
  ip_protocol       = "IP_PROTOCOL_TCP"
  port_range        = "${var.service_port}:${var.service_port}"
  remote_ip_prefix  = "0.0.0.0/0"
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
  image      = { id = local.image.id }
}

resource "cloudru_evolution_compute_interface" "main" {
  project_id                 = var.project_id
  name                       = "${var.name}-if"
  zone                       = { name = var.zone }
  type                       = "INTERFACE_TYPE_REGULAR"
  interface_security_enabled = true
  subnet                     = { id = local.subnet.id }
  security_groups            = [{ id = cloudru_evolution_compute_security_group.stand.id }]
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

  depends_on = [
    cloudru_evolution_compute_security_group_rule.ssh,
    cloudru_evolution_compute_security_group_rule.egress,
  ]
}
