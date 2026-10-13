output "public_ip" {
  description = "Публичный адрес стенда."
  value       = cloudru_evolution_compute_external_ip.main.ip_address
}

output "ssh" {
  description = "Команда входа на сервер."
  value       = "ssh ${var.login}@${cloudru_evolution_compute_external_ip.main.ip_address}"
}

output "service_url" {
  description = "Адрес сервиса ассистента. Снаружи доступен, только если задан service_cidr."
  value       = "http://${cloudru_evolution_compute_external_ip.main.ip_address}:${var.service_port}"
}
