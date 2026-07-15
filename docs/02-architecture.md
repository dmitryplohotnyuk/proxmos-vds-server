# Architecture

## Компоненты

### Proxmox host

Текущий хост:

```text
Hostname: proxmox
IP: 192.168.0.176/24
Gateway: 192.168.0.3
CPU: Intel Core i7-8700T, 6c/12t
RAM: 32 GB
Storage: 512 GB NVMe, LVM-thin
```

Proxmox отвечает только за виртуализацию, мостовую сеть, бэкапы и снапшоты.

### LXC domain-router

Назначение: инфраструктурная точка входа.

Рекомендуемые ресурсы:

```text
OS: Debian 12
CPU: 1-2 cores
RAM: 512 MB - 1 GB
Disk: 4-8 GB
WAN: DHCP on vmbr0
LAN: 10.77.0.2/24 on vmbr1
```

Внешний `eth0` получает адрес по DHCP. Текущий адрес `192.168.0.173` и MAC
`BC:24:11:2B:16:61`, но этот адрес не используется в доменных маршрутах.
Внутренний `lan0` всегда имеет адрес `10.77.0.2/24`.

Сервисы:

- `caddy` - локальный reverse proxy.
- `cloudflared` - исходящий туннель в Cloudflare.
- `domain-router` - CLI-скрипт управления маршрутами.
- `domain-router-ui` - приватная веб-панель управления маршрутами.
- `nftables` - маршрутизация и NAT внутренних VM через DHCP-интерфейс.
- `domain-router-network-watchdog` - обновление DHCP после смены роутера.

Веб-панель слушает только `127.0.0.1:8090` и публикуется
внутри tailnet через Tailscale Serve. Публичный Cloudflare Tunnel продолжит
передавать запросы только в Caddy на `127.0.0.1:8080`, поэтому административный
и пользовательский контуры останутся разделены.

### Application VM/LXC

Каждое приложение живет отдельно:

```text
VM 101 production: 10.77.0.10:80
VM 102 wordpress: 10.77.0.11:80
VM 103 api:        10.77.0.12:8080
```

Параметры сети приложения:

```text
Bridge: vmbr1
Address: 10.77.0.10-10.77.0.199/24
Gateway: 10.77.0.2
DNS: 1.1.1.1 or another public resolver
```

В приложениях не нужно ставить `cloudflared` и не нужен второй интерфейс в
сети внешнего роутера.

## Сетевые мосты

```text
Physical router (любая DHCP-подсеть)
  |
vmbr0
  |
  +-- domain-router eth0 (DHCP)

vmbr1 10.77.0.0/24 (без физического порта)
  |
  +-- Proxmox host 10.77.0.1
  +-- domain-router lan0 10.77.0.2, gateway/NAT
  +-- application VM/LXC 10.77.0.10+
```

Watchdog проверяет внешний доступ каждые 30 секунд. После трех неудачных
проверок он сбрасывает старую DHCP-аренду на `eth0`, получает новую и
перезапускает `cloudflared`, если tunnel уже настроен.

## Сетевой поток

### Внешний запрос

```text
Browser
  |
  | https://app.example.com
  v
Cloudflare edge
  |
  | Cloudflare Tunnel
  v
cloudflared in LXC domain-router
  |
  | http://127.0.0.1:8080 or local Caddy listener
  v
Caddy in LXC domain-router
  |
  | reverse_proxy 10.77.0.10:80
  v
Application VM
```

### Почему Cloudflare Tunnel перед Caddy

`cloudflared` решает проблему входа за NAT: контейнер сам создает исходящее соединение к Cloudflare.

Caddy решает проблему маршрутизации внутри Proxmox:

```text
app.example.com  -> 10.77.0.10:80
blog.example.com -> 10.77.0.11:80
api.example.com  -> 10.77.0.12:8080
```

Так Cloudflare знает только про один туннель, а все внутренние изменения делаются локально в LXC.

## DNS-модель

Для Cloudflare-варианта домен должен быть обслужен Cloudflare DNS или иметь DNS-записи, которые ведут на Cloudflare Tunnel.

Практически:

```text
app.example.com  CNAME  <tunnel-id>.cfargotunnel.com
blog.example.com CNAME  <tunnel-id>.cfargotunnel.com
api.example.com  CNAME  <tunnel-id>.cfargotunnel.com
```

Если зона домена полностью подключена к Cloudflare, эти записи можно создать в панели Cloudflare вручную. Автоматизация через API не обязательна для первой версии.

## Альтернатива без Cloudflare

Если позже нужен полный контроль, можно заменить Cloudflare Tunnel на VPS:

```text
Internet
  |
VPS public IP
  |
WireGuard
  |
LXC domain-router
  |
Caddy
  |
VM/LXC apps
```

В этом случае DNS `A`-записи будут указывать на VPS, а VPS будет прокидывать трафик через WireGuard на домашний `domain-router`.

Внутренняя модель маршрутов при этом не меняется.

## Доступ к Proxmox после смены роутера

Работа tunnel и опубликованных приложений не зависит от IP самого Proxmox на
`vmbr0`. Однако его текущий адрес управления `192.168.0.176` статический и в
другой подсети локально открываться не будет.

Постоянная точка управления хостом находится на `10.77.0.1`, но для доступа к
ней извне нужен приватный административный tunnel. Планируемый вариант -
Tailscale в `domain-router` с публикацией маршрута `10.77.0.0/24`. Это не влияет
на публикацию доменов и потребует отдельной авторизации владельца.
