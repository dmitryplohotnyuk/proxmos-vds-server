# Operations

## Базовые проверки

Проверить контейнер:

```bash
pct status 100
pct enter 100
```

Текущий DHCP-адрес контейнера: `192.168.0.173`. Постоянный внутренний адрес:
`10.77.0.2`.

Проверить переносимую сетевую схему:

```bash
ip -br addr show vmbr1
pct exec 100 -- ip -br addr
pct exec 100 -- sysctl -n net.ipv4.ip_forward
pct exec 100 -- nft list ruleset
pct exec 100 -- systemctl status domain-router-network-watchdog.timer
```

## Watchdog физического Ethernet Proxmox

На Proxmox установлен отдельный watchdog для встроенного Intel I219-V. Он не
проверяет обычную доступность интернета и не реагирует на выключенный роутер.
Срабатывание происходит только при появлении в журнале ядра точной ошибки
`e1000e ... nic0: Detected Hardware Unit Hang`.

Проверка выполняется каждые 30 секунд. После обнаружения ошибки watchdog:

1. сохраняет диагностику в systemd journal;
2. проверяет доступность локального шлюза через `vmbr0`;
3. пробует reset через `ethtool`;
4. пробует выключить и включить физический link;
5. переподключает PCI-устройство к драйверу `e1000e` и применяет ifupdown-конфигурацию.

Между попытками восстановления действует cooldown 5 минут. Автоматическая
перезагрузка Proxmox по умолчанию выключена.

Проверить состояние и журнал:

```bash
systemctl status proxmox-nic-watchdog.timer
systemctl list-timers proxmox-nic-watchdog.timer
journalctl -u proxmox-nic-watchdog.service --no-pager
```

Безопасно проверить запуск без сброса сети:

```bash
WATCHDOG_FORCE=1 WATCHDOG_DRY_RUN=1 /usr/local/sbin/proxmox-nic-watchdog
```

Повторная установка на Proxmox:

```bash
sudo ./scripts/install-proxmox-nic-watchdog.sh
```

Параметры находятся в `/etc/default/proxmox-nic-watchdog`. Включать
`WATCHDOG_AUTO_REBOOT=1` следует только после отдельной проверки процедуры
восстановления и штатного завершения VM/LXC.

Проверить Caddy:

```bash
systemctl status caddy
caddy validate --config /etc/caddy/Caddyfile
journalctl -u caddy -n 100 --no-pager
```

Проверить Cloudflare Tunnel:

```bash
systemctl status cloudflared
journalctl -u cloudflared -n 100 --no-pager
```

Проверить Tailscale:

```bash
tailscale status
tailscale ip -4
systemctl status tailscaled
```

С компьютера, подключенного к тому же tailnet:

```bash
ssh root@domain-router
```

После установки Tailscale на клиенте доступны постоянные внутренние адреса,
не зависящие от DHCP текущего роутера:

```text
domain-router:                100.103.127.113
Proxmox UI:                   https://10.77.0.1:8006
VM/LXC в приватной сети:      10.77.0.10-10.77.0.127 (свободные адреса)
Виртуальные IP реестра:       10.77.0.128-10.77.0.239 (не выдавать гостям)
```

На Linux-клиенте может потребоваться включить прием subnet routes:

```bash
tailscale set --accept-routes=true
```

Проверить маршруты:

```bash
domain-router list
domain-router status
```

С хоста Proxmox команды можно выполнять без входа в контейнер:

```bash
pct exec 100 -- /usr/local/bin/domain-router status
pct exec 100 -- /usr/local/bin/domain-router list
```

## Веб-панель

Панель доступна только внутри tailnet:

```text
https://domain-router.tail6ace7f.ts.net
```

## Прямой доступ к Proxmox через Tailscale

Tailscale также установлен непосредственно на Proxmox. Этот путь не зависит от
работоспособности LXC `domain-router`:

```text
Hostname:       proxmox
Tailscale IP:   100.93.12.80
Web UI:         https://proxmox.tail6ace7f.ts.net
SSH:            ssh root@100.93.12.80
```

Tailscale Serve принимает приватный HTTPS на `443` и проксирует его в локальный
`pveproxy` на `https+insecure://127.0.0.1:8006`. Внешний сертификат выпущен для
MagicDNS-имени и корректно проверяется браузером. `insecure` относится только к
локальной проверке self-signed сертификата Proxmox внутри того же хоста.

Funnel отключен, поэтому панель не доступна из публичного интернета. На Proxmox
установлено `--accept-routes=false`: хост не принимает анонсируемую LXC subnet
route, так как сам напрямую подключен к `vmbr1`.

Проверить:

```bash
systemctl status tailscaled
tailscale status
tailscale ip -4
tailscale serve status
curl https://proxmox.tail6ace7f.ts.net/
```

Повторная установка или восстановление:

```bash
./scripts/install-proxmox-tailscale.sh
```

Проверить сервис и приватный HTTPS:

```bash
systemctl status domain-router-ui
journalctl -u domain-router-ui -n 100 --no-pager
curl http://127.0.0.1:8090/healthz
tailscale serve status --json
```

Проверить количество администраторов:

```bash
domain-router-ui-admin status
```

Создать администратора или сбросить пароль можно только из консоли. Пароль
вводится интерактивно и не попадает в аргументы процесса или shell history:

```bash
domain-router-ui-admin create admin
domain-router-ui-admin reset-password admin
```

Панель запускается от пользователя `domain-router-ui`. Доступ к изменению
маршрутов ограничен `/etc/sudoers.d/domain-router-ui`; файл нужно проверять
через `visudo -cf` после каждого изменения.

## Добавление нового домена

DNS уже настроен для корня `content-factory-vps.win` и wildcard-записи
`*.content-factory-vps.win`. Для нового поддомена отдельная DNS-запись не нужна.

В панели нажать `Добавить маршрут`, указать домен и upstream из сети
`10.77.0.0/24`, затем сохранить. Альтернативный консольный способ описан ниже.

1. Добавить маршрут:

```bash
domain-router add app.content-factory-vps.win http://10.77.0.10:80
```

Изменение применяется сразу: CLI сначала проверяет новый Caddyfile, затем
атомарно сохраняет маршруты и перезагружает Caddy. При ошибке перезагрузки
предыдущая конфигурация восстанавливается.

2. Проверить:

```bash
curl https://app.content-factory-vps.win
```

Для корневого домена используется та же команда:

```bash
domain-router add content-factory-vps.win http://10.77.0.10:80
```

## VM content-factory

Текущая первая application VM:

```text
VM ID:       101
Name:        content-factory
OS:          Ubuntu Server 24.04 LTS
CPU:         6 vCPU
RAM:         12288 MB
Disk:        150 GB thin SSD
Bridge:      vmbr1
IP:          10.77.0.10/24
Gateway:     10.77.0.2
Domains:     content-factory.win, www.content-factory.win
```

SSH доступен через Tailscale subnet route:

```bash
ssh root@10.77.0.10
```

Для повторного создания на Proxmox:

```bash
cd domain-router-deployment
./scripts/create-content-factory-vm.sh
```

Сценарий откажется работать, если VM ID уже занят. Root-пароль генерируется
заново и выводится один раз; в документацию и Git он не записывается.

Текущий root-раздел имеет около `145 GiB` полезного пространства. После
уменьшения VM незарезервированный логический запас `local-lvm` составляет около
`190 GiB`; фактическое свободное место thin pool нужно проверять через
`pvesm status`.

Сценарий создаёт чистую Ubuntu без веб-сервера. Приложение на существующей VM
и текущие routes являются отдельным состоянием. Не считайте старую запись
`content-factory-vps.win` рабочим маршрутом: при последней проверке она давала
404. Фактический список всегда проверяйте через `domain-router list --json`.

## Перенос сервиса на другую VM

```bash
domain-router set app.content-factory-vps.win http://10.77.0.11:80
```

DNS менять не нужно.

## Отключение домена

```bash
domain-router disable app.content-factory-vps.win
```

Запись остается в конфигурации, но Caddy больше не генерирует активный route.

## Удаление домена

```bash
domain-router remove app.content-factory-vps.win
```

Wildcard DNS остается без изменений. Удаленный поддомен будет отвечать `404`.

## Безопасность

- Не публиковать Proxmox UI напрямую в интернет.
- Не публиковать SSH напрямую в интернет.
- Для администрирования использовать Tailscale/WireGuard.
- Не открывать на роутере порты для Tailscale: соединение устанавливается
  исходящим трафиком и работает за NAT.
- Панель доступна через Tailscale Serve; Tailscale Funnel для нее не используется.
- Хранить Cloudflare credentials только внутри LXC и в Proxmox backup.
- Не давать приложениям доступ к Cloudflare token, если он не нужен.

## Бэкапы

Рекомендуемый минимум:

```text
daily backup of LXC 100 domain-router
keep last 7 daily backups
```

Для панели критичны:

```text
/var/lib/domain-router-ui/app.db
/opt/domain-router-ui/source/
/etc/systemd/system/domain-router-ui.service
/etc/sudoers.d/domain-router-ui
```

Полный перечень секретного состояния CT 100/107 и Proxmox, включая управляемые
версии Tunnel, Tailscale state и inventory decisions, приведён в
[восстановлении](08-rebuild-from-zero.md#данные-которые-восстанавливаются-отдельно).
Один backup исходников не восстанавливает регистрацию устройств и данные сервисов.

Перед крупными изменениями:

```text
snapshot LXC 100
apply changes
test
remove snapshot after verification
```

## Диагностика проблем

### Домен не открывается

Проверить по слоям:

1. DNS-запись существует и ведет на tunnel.
2. `cloudflared` запущен.
3. Caddy запущен.
4. Домен есть в `domain-router list`.
5. Target доступен из LXC.

### Target недоступен

Из LXC:

```bash
curl -I http://10.77.0.10:80
```

Если нет ответа, проблема не в домене, а во внутренней VM/LXC, firewall или IP.

### Caddy не перезагружается

```bash
caddy validate --config /etc/caddy/Caddyfile
journalctl -u caddy -n 100 --no-pager
```

Обычно причина в ошибке генерации Caddyfile или некорректном target.

### Tunnel работает, но домен дает 404

Это значит, что запрос дошел до tunnel, но не совпал с активным route.

Проверить:

```bash
domain-router list
cat /etc/caddy/Caddyfile
```

### Панель не открывается

1. Убедиться, что клиент подключен к тому же Tailscale tailnet.
2. Проверить `tailscale serve status --json`.
3. Проверить `systemctl status domain-router-ui`.
4. Проверить локальный `curl http://127.0.0.1:8090/healthz`.

### Панель показывает ошибку sudo

```bash
systemctl show domain-router-ui -p NoNewPrivileges
visudo -cf /etc/sudoers.d/domain-router-ui
sudo -u domain-router-ui sudo -n /usr/local/bin/domain-router status --json
```

Для выбранной схемы restricted sudo параметр `NoNewPrivileges` должен быть
`no`. Изоляция сервиса обеспечивается отдельным пользователем, `PrivateTmp`,
`ProtectHome` и точным списком разрешенных CLI-команд.

### Внутренние адреса и VPN-переключатели

Управление сервисами находится на `/internal`, отдельно от публичных маршрутов.
Источник данных и решений - `/var/lib/service-inventory/inventory.json` на
Proxmox; обновление выполняет `service-inventory-sync.timer`. Не менять live
firewall CT 107 вручную после установки агента. Диагностика, backup и rollback:
[12-internal-services.md](12-internal-services.md).
