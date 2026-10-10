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

На Proxmox установите инструменты для сценариев: `apt-get install git curl
dnsutils openssl python3`. На клиенте для проверок нужны Tailscale, curl, dig и
Python 3.11+. Команды далее выполняются от root на Proxmox, если место запуска
не указано отдельно. `.env` является доверенным shell-файлом, а не хранилищем секретов.

Базовый deploy создаёт только CT 100. Полная текущая схема дополнительно требует
этапов ниже. Расширения реестра/подключений рассчитаны на CT 100/107 и сеть
`10.77.0.0/24`; адреса `.1`, `.2`, `.3` заняты инфраструктурой, `.128-.239`
зарезервированы под виртуальные IP реестра. Параметры `.env` не превращают все
скрипты и nftables-шаблоны в универсальную поддержку произвольной топологии.

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
необходимости отдельно согласовать отключение expiry для ключа устройства.
По умолчанию не менять ACL и key expiry. Funnel включать нельзя.

Пароль `admin` вводится в интерактивном prompt и нигде не сохраняется в
открытом виде.

### 4. Проверка

В LXC:

```bash
/opt/domain-router-ui/source/scripts/verify-deployment.sh
cd /opt/domain-router-ui/source
python3 -m venv /tmp/domain-router-tests
/tmp/domain-router-tests/bin/pip install -e '.[dev]'
/tmp/domain-router-tests/bin/pytest
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

## Прямой Tailscale на Proxmox

После основной установки рекомендуется добавить независимый административный
канал непосредственно на Proxmox-хосте:

```bash
./scripts/install-proxmox-tailscale.sh
```

Открыть выведенную ссылку авторизации и подключить устройство `proxmox` к тому
же tailnet. Сценарий настроит приватный Tailscale Serve к `pveproxy:8006` и явно
сбросит Funnel. Auth key и состояние Tailscale в Git не сохраняются.

После перезагрузки проверить:

```bash
systemctl is-enabled tailscaled
systemctl is-active tailscaled
tailscale serve status
pct status 100
qm status 101
```

## Данные, которые восстанавливаются отдельно

Репозиторий восстанавливает код и конфигурацию, но не секретное состояние:

```text
CT 100: /etc/cloudflared/ целиком, включая router-managed/
CT 100: /etc/domain-router/, /etc/caddy/, /var/lib/domain-router-ui/
CT 100: /var/lib/domain-router-integrations/, /var/lib/tailscale/
CT 100: /etc/systemd/system/cloudflared.service.d/, domain-router-ui.service.d/
CT 107: /var/lib/tailscale/, /etc/tailscale-gateway.nft, /etc/systemd/system/
Proxmox: /var/lib/service-inventory/, /etc/pve/, /etc/network/interfaces
Proxmox: /var/lib/tailscale/, /etc/default/proxmox-nic-watchdog
Гостевые системы: диски приложений и их собственные конфигурации/данные
```

Для миграции текущей установки эти файлы нужно брать из защищенного backup LXC.
При чистой установке tunnel, пользователь и routes создаются заново.

Backup содержит ключи и пароли/хеши: храните шифрованно и вне репозитория.
Для работающей SQLite используйте SQLite backup API либо остановите UI на время
копирования всей `/var/lib/domain-router-ui/`, включая WAL/SHM. Предпочтительны
проверенные Proxmox backups CT 100/107 и всех VM/LXC плюс отдельный backup хоста.
Не запускайте одновременно оригинал и клон с одним Tailscale state. Восстановление
без state означает новую регистрацию, новые Tailscale IP и повторное одобрение routes.

## Полная Текущая Схема

Выполняйте в следующем порядке; основной deploy не выполняет эти шаги за вас:

1. Базовый CT 100 и первоначальные Cloudflare/Tailscale подключения: раздел A.
2. Независимый доступ к Proxmox: `bash scripts/install-proxmox-tailscale.sh`.
3. Ubuntu VM 101: `bash scripts/create-content-factory-vm.sh` (6 vCPU, 12 GiB,
   150 GiB). Для сохранения приложения восстановите его диск из backup, а не
   запускайте создание поверх существующей VM.
4. AdGuard CT 106: [этап 10](10-adguard-home.md), затем настройка DHCP DNS роутера.
   До запуска укажите существующий Debian template через `ADGUARD_CT_TEMPLATE`.
5. Восстановите Home Assistant VM 102, Meshtastic VM 103, media/Jellyfin CT 104 и
   TorrServer CT 105 из своих backups. Их установщики и медиа/прикладные данные
   в этот репозиторий не входят; скрипты сети предполагают уже работающие сервисы.
6. Шлюз CT 107, вторичные интерфейсы сервисов, trusted networks и одобрение routes:
   [этап 11](11-tailscale-gateway.md). Только после успешной проверки отзовите
   объявление subnet на CT 100. Для HA нужна его доступная LAN `.222/32`.
7. Автоматический реестр: [этап 12](12-internal-services.md). При миграции верните
   `inventory.json` до первого сканирования; на новой установке проверьте все разрешения.
8. Разрешите нужные корневые зоны и создайте DNS/маршруты по [этапу 13](13-cloudflare-domain-guide.md).
   Production использует три зоны: `content-factory-vps.win`, `mesh-kremen.stream`,
   `content-factory.win`. Действующие маршруты перечислены в журнале, не в шаблоне routes.
9. Агенты управления подключениями: [этап 14](14-integration-manager.md).
10. Watchdog физического NIC: `bash scripts/install-proxmox-nic-watchdog.sh`.
    Сначала проверьте interface/PCI/driver в его `.default.example`: они зависят от железа.
11. Проверьте baseline в CT 100 и `CHECK_LAN=1 bash scripts/verify-tailscale-services.sh`
    с отдельного Tailscale-клиента; затем проверьте доступ с мобильного интернета.
    Отдельно проверьте реальные публичные домены, OFF/ON сервисов и запуск после
    плановой перезагрузки. Тесты с переключением/откатом запускайте в окно обслуживания.

Переезд в другую LAN не меняет private IP, но AdGuard `.5`, HA `.222/32` и LAN-only
сервисы зависят от текущей `192.168.0.0/24`. Для другой подсети требуется их отдельная
миграция; обратитесь к ограничениям этапов 10–12. Статический LAN-IP Proxmox также
не переносится автоматически, используйте независимый Tailscale/локальную консоль.

## Обновление существующей установки

Отдельный шлюз доступа к сервисам и автоматическая вкладка внутренних адресов
восстанавливаются после основной панели по документам
[11-tailscale-gateway.md](11-tailscale-gateway.md) и
[12-internal-services.md](12-internal-services.md). После миграции на CT 107
старое объявление subnet route на CT 100 нужно отозвать. Для сохранения
переключателей восстановить `/var/lib/service-inventory/inventory.json` на хосте
из защищённого backup перед первым сканированием.

Не используйте `deploy-from-proxmox.sh`, `install-lxc.sh` и
`configure-integrations.sh` для обычного production-обновления: они предназначены
для начальной установки и могут перезаписать environment, nftables, DNS и Serve.
Сделайте backup CT 100/107 и состояния хоста. Сохраните список routes, затем
обновите исходники без удаления production state. На Proxmox из нового checkout:

```bash
bash scripts/package-source.sh /tmp/domain-router-source.tar.gz
pct push 100 /tmp/domain-router-source.tar.gz /root/domain-router-source.tar.gz
pct exec 100 -- tar -xzf /root/domain-router-source.tar.gz -C /opt/domain-router-ui/source
```

Перед заменой пакета проверьте, что `/etc/domain-router/environment` содержит
полный актуальный `DOMAIN_ROUTER_ALLOWED_DOMAIN` и upstream networks; список UI
drop-in не заменяет настройки CLI. Внутри CT 100:

```bash
/opt/domain-router-ui/venv/bin/pip install /opt/domain-router-ui/source
install -o root -g root -m 0755 /opt/domain-router-ui/source/config/domain-router-wrapper /usr/local/bin/domain-router
systemctl restart domain-router-ui
/opt/domain-router-ui/source/scripts/verify-deployment.sh
/usr/local/bin/domain-router list --json
```

После обновления UI на Proxmox синхронизируйте обе части расширений:

```bash
bash scripts/install-service-inventory.sh "$PWD"
bash scripts/install-integration-manager.sh "$PWD"
```

Повторите внешние smoke tests и сравните routes/решения с backup. Для отката
восстановите прежний пакет и оба root-агента вместе; rollback Tunnel описан в
этапе 14 и не заменяет rollback приложения. Не применяйте pending Tunnel snapshot
из старого backup автоматически: сначала проверьте, какой connector сейчас активен.
