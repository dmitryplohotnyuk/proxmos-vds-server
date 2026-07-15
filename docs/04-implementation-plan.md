# Implementation Plan

## Stage 1 - Подготовка Proxmox

Статус: выполнено 2026-07-15.

1. Скачать Debian 12 LXC template.
2. Создать LXC `domain-router`.
3. Настроить WAN-интерфейс контейнера по DHCP.
4. Создать независимую сеть `vmbr1` (`10.77.0.0/24`).
5. Назначить контейнеру стабильный LAN IP `10.77.0.2`.
6. Настроить NAT и watchdog обновления DHCP.
7. Включить запуск контейнера вместе с Proxmox.
8. Проверить доступ контейнера и тестового клиента к интернету.

Предлагаемые параметры:

```text
CT ID: 100
Name: domain-router
OS: Debian 12
CPU: 1
RAM: 512 MB or 1024 MB
Disk: 8 GB
WAN: DHCP on vmbr0
LAN: 10.77.0.2/24 on vmbr1
```

## Stage 2 - Установка базовых пакетов

Статус: выполнено 2026-07-15.

В контейнере:

```text
caddy
cloudflared
curl
jq
yq or python3-yaml
```

Для первой версии лучше писать CLI на Python, потому что YAML и шаблонизация будут проще и надежнее, чем shell-скриптом.

## Stage 3 - Настройка Caddy

Статус: выполнено 2026-07-15.

1. Создать каталог `/etc/domain-router`.
2. Создать начальный `/etc/domain-router/routes.yml`.
3. Создать генератор Caddyfile.
4. Настроить Caddy слушать локальный HTTP.
5. Проверить `caddy validate`.
6. Проверить `systemctl reload caddy`.

## Stage 4 - Настройка Cloudflare Tunnel

Статус: выполнено 2026-07-15.

1. Авторизовать `cloudflared` в Cloudflare.
2. Создать именованный tunnel, например `proxmox-domain-router`.
3. Сохранить credentials в `/etc/cloudflared`.
4. Настроить ingress на Caddy.
5. Запустить `cloudflared` как systemd service.
6. Создать DNS-записи для корня домена и wildcard-поддоменов.
7. Проверить внешний доступ.

## Stage 4.1 - Приватный административный доступ

Статус: выполнено 2026-07-15.

1. Установить Tailscale в LXC `domain-router`.
2. Передать контейнеру `/dev/net/tun`.
3. Включить Tailscale SSH.
4. Анонсировать и одобрить маршрут `10.77.0.0/24`.
5. Отключить истечение ключа для постоянно работающего subnet router.
6. Разрешить forwarding `tailscale0` -> `lan0` в nftables.
7. Проверить доступ с клиентского компьютера после установки Tailscale.

## Stage 5 - CLI domain-router

Статус: выполнено 2026-07-15.

Создать исполняемый файл:

```text
/usr/local/bin/domain-router
```

Функции первой версии:

- `list`
- `add`
- `set`
- `remove`
- `enable`
- `disable`
- `render`
- `reload`
- `status`
- `test`

Файлы:

```text
/etc/domain-router/routes.yml
/etc/domain-router/domain-router.yml
/var/log/domain-router.log
/etc/caddy/Caddyfile
```

## Stage 6 - Тестовый маршрут

Статус: локальная и внешняя маршрутизация проверены 2026-07-15.

Для проверки нужен простой внутренний HTTP-сервис.

Варианты:

- временный nginx в отдельном LXC;
- тестовая страница в production VM;
- временный Python HTTP server внутри отдельной VM.

Проверки:

```text
curl -H "Host: app.example.com" http://<domain-router-ip>
curl https://app.example.com
```

## Stage 7 - Бэкапы и восстановление

Нужно включить Proxmox backup для LXC `domain-router`.

Минимально важные файлы:

```text
/etc/domain-router/
/etc/caddy/Caddyfile
/etc/cloudflared/
/usr/local/bin/domain-router
```

Восстановление должно сводиться к:

1. Restore LXC backup.
2. Проверить `systemctl status caddy`.
3. Проверить `systemctl status cloudflared`.
4. Проверить `domain-router status`.

## Stage 8 - Приватная веб-панель

Статус: выполнено 2026-07-15.

Панель добавляется как вторая итерация поверх стабильного CLI. `routes.yml`
остается единым источником истины, а общая логика маршрутов выносится в Python
package с блокировкой конкурентных изменений.

Основные подэтапы:

1. Рефакторинг и тестирование общего core.
2. Авторизация, сессии, CSRF и SQLite.
3. Веб-интерфейс управления маршрутами и audit log.
4. Непривилегированный systemd-сервис и ограниченный sudoers.
5. Приватный HTTPS через Tailscale Serve.
6. Интеграционные, браузерные и reboot-тесты.

Полный план, модель угроз и критерии приемки описаны в
[`07-admin-panel-plan.md`](07-admin-panel-plan.md).

## Stage 9 - Воспроизводимое развертывание

Статус: выполнено 2026-07-15.

Создан отдельный Git-репозиторий, содержащий исходники, конфигурационные
шаблоны, документацию и сценарии восстановления. Поддерживаются три процесса:

1. автоматическое создание и установка через Bash;
2. ручная пошаговая установка с контрольными командами;
3. управляемое развертывание через Codex по `CODEX.md`.

Сценарии разделяют локальную установку и внешние авторизации. Cloudflare tunnel
credentials, Tailscale keys и пароль администратора не хранятся в Git. Повторный
запуск установщика сохраняет production routes и SQLite-базу панели.

Полная процедура: [`08-rebuild-from-zero.md`](08-rebuild-from-zero.md).
