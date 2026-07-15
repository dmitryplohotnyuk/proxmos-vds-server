# Задача для Codex: восстановить Domain Router с нуля

Разверни этот репозиторий на указанном пользователем Proxmox-хосте. Целевое
состояние и ограничения описаны в `docs/02-architecture.md`, а точная процедура
в `docs/08-rebuild-from-zero.md`.

## Правила выполнения

1. Сначала прочитай `README.md`, `.env.example`, `docs/02-architecture.md` и
   `docs/08-rebuild-from-zero.md`.
2. Не записывай в репозиторий пароли, Tailscale auth keys, Cloudflare API tokens
   или tunnel credentials.
3. До изменений проверь существующие bridges, CT ID, storage и template.
4. Если CT или `vmbr1` уже существуют, не удаляй и не пересоздавай их без
   отдельного подтверждения пользователя.
5. Для нового хоста используй `scripts/deploy-from-proxmox.sh`.
6. На интерактивной авторизации Cloudflare/Tailscale передай пользователю URL и
   дождись завершения. Не публикуй панель через Tailscale Funnel.
7. Попроси пользователя ввести пароль администратора интерактивно; не передавай
   пароль аргументом командной строки и не цитируй его в итоговом отчете.
8. Запусти `scripts/verify-deployment.sh`, затем тесты Python.
9. Проверь после перезагрузки LXC: сеть, NAT, Caddy, cloudflared, Tailscale,
   панель и HTTPS через Tailscale Serve.
10. Зафиксируй фактические параметры и результаты в `docs/06-deployment-log.md`,
    не добавляя секреты.

## Критерии готовности

- LXC получает WAN через DHCP и сохраняет `10.77.0.2/24` на `lan0`.
- VM/LXC из `10.77.0.0/24` используют `10.77.0.2` как gateway.
- неизвестный публичный домен возвращает `404`;
- `domain-router` управляет маршрутами и откатывает ошибочные изменения;
- панель слушает только loopback и доступна через Tailscale Serve HTTPS;
- Funnel выключен;
- все обязательные systemd units активны после reboot;
- в Git нет credentials и паролей.

