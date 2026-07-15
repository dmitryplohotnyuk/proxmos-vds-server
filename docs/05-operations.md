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
VM/LXC в приватной сети:      10.77.0.10-10.77.0.199
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
CPU:         10 vCPU
RAM:         24576 MB
Disk:        300 GB thin SSD
Bridge:      vmbr1
IP:          10.77.0.10/24
Gateway:     10.77.0.2
Domain:      content-factory-vps.win
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

Чистая Ubuntu не содержит веб-сервер. Пока приложение не слушает
`10.77.0.10:80`, домен корректно доходит до VM-маршрута, но отвечает `502`.

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
