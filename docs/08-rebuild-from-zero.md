# Восстановление с нуля

Этот документ описывает повторное развертывание Domain Router на чистом
Proxmox. Он дополняет архитектуру и эксплуатационную документацию, но не
содержит секретов текущей установки.

## Что потребуется

- Proxmox VE с доступом `root`, рабочим `vmbr0` и интернетом;
- свободный CT ID, по умолчанию `100`;
- домен, добавленный в Cloudflare DNS;
- учетные записи Cloudflare и Tailscale;
- этот Git-репозиторий на Proxmox-хосте.

Перед началом сохранить backup существующего LXC и проверить, что сеть
`10.77.0.0/24` не используется другой инфраструктурой.

## Вариант A: Bash

### 1. Параметры

```bash
cp .env.example .env
nano .env
```

`.env` не отслеживается Git. В нем нет необходимости хранить пароли или API
tokens. Основные параметры: CT ID, storage, bridges, доменная зона и имена
интеграций.

### 2. Создание и локальная установка

На Proxmox:

```bash
chmod +x scripts/*.sh
./scripts/deploy-from-proxmox.sh
```

Сценарий:

1. добавляет `vmbr1` с адресом `10.77.0.1/24`, если bridge отсутствует;
2. скачивает Debian 12 template;
3. создает unprivileged LXC с WAN DHCP и `lan0=10.77.0.2/24`;
4. передает `/dev/net/tun` для Tailscale;
5. копирует исходники в `/opt/domain-router-ui/source`;
6. устанавливает Caddy, cloudflared, Tailscale, nftables, CLI и панель.

Существующий CT не удаляется и не пересоздается. При совпадении CT ID скрипт
использует его только как цель установки; его сеть следует проверить вручную.

### 3. Cloudflare, Tailscale и администратор

```bash
pct enter 100
/opt/domain-router-ui/source/scripts/configure-integrations.sh
```

Cloudflare покажет URL авторизации. После входа скрипт создаст named tunnel,
root и wildcard DNS routes. Затем Tailscale покажет свой URL авторизации,
включит SSH, анонсирует `10.77.0.0/24` и настроит Serve на панель.

В Tailscale Admin Console нужно подтвердить subnet route `10.77.0.0/24` и при
необходимости отключить expiry для ключа устройства. Funnel включать нельзя.

Пароль `admin` вводится в интерактивном prompt и нигде не сохраняется в
открытом виде.

### 4. Проверка

В LXC:

```bash
/opt/domain-router-ui/source/scripts/verify-deployment.sh
cd /opt/domain-router-ui/source
/opt/domain-router-ui/venv/bin/pip install -e '.[dev]'
/opt/domain-router-ui/venv/bin/pytest
```

На Proxmox перезагрузить только LXC и повторить проверку:

```bash
pct reboot 100
sleep 15
pct exec 100 -- /opt/domain-router-ui/source/scripts/verify-deployment.sh
```

Неизвестный поддомен должен вернуть `404`. После создания первой VM добавить
временный маршрут, проверить внешний HTTPS, затем удалить маршрут.

## Вариант B: вручную

Ручной порядок повторяет Bash-сценарии без скрытых шагов:

1. Создать `vmbr1` без физического порта, адрес `10.77.0.1/24`.
2. Создать Debian 12 unprivileged LXC: 1 CPU, 1 GB RAM, 8 GB disk.
3. Назначить `eth0` через `vmbr0`/DHCP и `lan0` через `vmbr1` с
   `10.77.0.2/24`.
4. Передать `/dev/net/tun` строками из `scripts/create-proxmox-lxc.sh`.
5. Скопировать репозиторий в `/opt/domain-router-ui/source`.
6. В LXC установить пакеты из `scripts/install-lxc.sh`.
7. Установить файлы из `config/` в пути, явно указанные в этом скрипте.
8. Выполнить `scripts/install-admin-panel.sh`.
9. Авторизовать cloudflared, создать tunnel и DNS routes root + wildcard.
10. Выполнить `tailscale up --ssh --advertise-routes=10.77.0.0/24`.
11. Выполнить `tailscale serve --bg http://127.0.0.1:8090`.
12. Создать администратора: `domain-router-ui-admin create admin`.
13. Выполнить приемочную проверку из предыдущего раздела.

Конкретные unit-файлы, nftables rules и настройки безопасности нужно брать из
репозитория, а не переписывать по памяти.

## Вариант C: Codex

1. Открыть каталог репозитория в Codex.
2. Сообщить адрес Proxmox и способ доступа, не добавляя пароль в файлы.
3. Попросить: `Выполни восстановление по CODEX.md`.
4. Подтвердить только необходимые подключения к Proxmox.
5. Пройти URL авторизации Cloudflare и Tailscale по запросу Codex.
6. Ввести пароль администратора в интерактивной консоли.
7. Получить отчет с результатами `verify-deployment.sh`, тестов и reboot-check.

Codex обязан остановиться перед разрушительными действиями над существующим CT,
bridge или backup и запросить отдельное подтверждение.

## Данные, которые восстанавливаются отдельно

Репозиторий восстанавливает код и конфигурацию, но не секретное состояние:

```text
/etc/cloudflared/<tunnel-id>.json
/var/lib/domain-router-ui/app.db
/etc/domain-router/routes.yml (если содержит production routes)
```

Для миграции текущей установки эти файлы нужно брать из защищенного backup LXC.
При чистой установке tunnel, пользователь и routes создаются заново.

## Обновление существующей установки

Сделать snapshot/backup LXC, обновить `/opt/domain-router-ui/source`, затем:

```bash
/opt/domain-router-ui/source/scripts/install-lxc.sh /opt/domain-router-ui/source
/opt/domain-router-ui/source/scripts/verify-deployment.sh
```

`install-lxc.sh` предназначен для идемпотентного обновления пакетов и файлов,
но перед обновлением production всегда нужен backup базы, routes и tunnel
credentials.

