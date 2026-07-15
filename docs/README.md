# Domain Router LXC

Документация проекта LXC-контейнера, который принимает домены и прокидывает их на VM/LXC внутри Proxmox.

Цель: подключить сервер к интернету в любой сети, включая NAT/CGNAT, и сохранить рабочую публикацию доменов без перенастройки приложений внутри виртуальных машин.

## Ключевое решение

Для режима "работает за любым NAT" нужен внешний входной слой, потому что обычный DNS сам по себе не может доставить входящий HTTP/HTTPS-трафик к серверу за CGNAT.

Базовая архитектура проекта:

```text
Internet
  |
Cloudflare DNS / Cloudflare Tunnel
  |
outbound tunnel from Proxmox LAN
  |
LXC 100 domain-router
  |
Caddy reverse proxy
  |
VM/LXC services inside Proxmox
```

Внутри Proxmox создается отдельный инфраструктурный LXC:

```text
Proxmox host: 192.168.0.176
  |
  |-- LXC 100 domain-router
  |     |-- Caddy
  |     |-- cloudflared
  |     |-- management script
  |
  |-- VM 101 production
  |     |-- Laravel / Docker / API
  |
  |-- VM 102 wordpress
  |
  |-- LXC/VM others
```

## Документы

- [01-requirements.md](01-requirements.md) - цели, ограничения и критерии готовности.
- [02-architecture.md](02-architecture.md) - целевая архитектура и сетевые потоки.
- [03-domain-model.md](03-domain-model.md) - как домены, DNS и маршруты будут описываться.
- [04-implementation-plan.md](04-implementation-plan.md) - пошаговый план реализации.
- [05-operations.md](05-operations.md) - эксплуатация, бэкапы, диагностика и безопасность.
- [06-deployment-log.md](06-deployment-log.md) - фактическое состояние развертывания.
- [07-admin-panel-plan.md](07-admin-panel-plan.md) - архитектура, безопасность и этапы реализации веб-панели.
- [08-rebuild-from-zero.md](08-rebuild-from-zero.md) - три способа полного повторного развертывания.

## Текущее состояние

На Proxmox создан и запущен LXC `100` (`domain-router`). В контейнере работают
Caddy и CLI `domain-router`. Локальная маршрутизация проверена полным циклом
добавления, отключения, включения и удаления маршрута.

Cloudflare Tunnel, wildcard DNS и приватный доступ через Tailscale настроены.
Приватная веб-панель управления маршрутами развернута по адресу
`https://domain-router.tail6ace7f.ts.net`. Внешняя маршрутизация и восстановление
всех сервисов после перезагрузки проверены.

## Решение для первой версии

Первая версия должна быть простой и надежной:

- Debian 12 LXC.
- Caddy как reverse proxy.
- Cloudflare Tunnel как NAT-proof вход.
- Консольный скрипт `domain-router` для управления маршрутами.
- Конфигурация маршрутов в локальном YAML/JSON-файле.
- Автоматическая проверка и reload Caddy после изменения маршрутов.

Вторая итерация с веб-панелью реализована по плану `07-admin-panel-plan.md`.
