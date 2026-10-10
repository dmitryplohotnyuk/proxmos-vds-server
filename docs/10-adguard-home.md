# AdGuard Home

## Назначение

AdGuard Home работает в отдельном LXC и фильтрует DNS-запросы устройств домашней
сети. Контейнер не публикуется в интернет и не связан с Cloudflare Tunnel.

## Текущая конфигурация

```text
LXC ID:              106
Hostname:            adguard-home
OS:                  Debian 12
IP:                  192.168.0.5/24
Gateway:             192.168.0.3
Bridge:              vmbr0
CPU:                 1 core
RAM / swap:          512 MiB / 512 MiB
Disk:                4 GiB
Autostart:           enabled, order 1
AdGuard Home:        v0.107.79
DNS:                 TCP/UDP 53
Admin panel:         http://192.168.0.5:3000
```

Логин панели: `admin`. Пароли администратора и root LXC не хранятся в Git.

Настроены:

- Quad9 DNS-over-HTTPS как основной upstream;
- Cloudflare DNS-over-HTTPS как fallback;
- DNSSEC, DNS cache и optimistic cache;
- официальный AdGuard DNS filter;
- HaGeZi Multi NORMAL;
- журнал запросов с хранением 7 дней;
- автоматическое определение клиентов;
- локальные DNS rewrites.

Archer C80 не отвечает на PTR-запросы, поэтому он не используется как private
reverse resolver: это создавало бы лишние таймауты без определения имён.

Локальные записи:

```text
proxmox.home.arpa         -> 192.168.0.176
home-assistant.home.arpa -> 192.168.0.222
```

При проверке `doubleclick.net` возвращает `0.0.0.0`.

## Роутер

На TP-Link Archer C80 настроено:

```text
DHCP pool:       192.168.0.100-192.168.0.249
Lease time:      120 minutes
Primary DNS:     192.168.0.5
Secondary DNS:   not configured (0.0.0.0)
IPv6 mode:       disabled
```

Адрес `.5` находится вне DHCP-пула, поэтому конфликт с динамическими адресами
исключен. Новая DNS-настройка применяется после обновления DHCP-аренды. Для
быстрого применения достаточно переподключить Wi-Fi или перезапустить сеть на
клиенте.

IPv6 на роутере отключен, поэтому клиенты не получают альтернативный IPv6 DNS.

Второй публичный DNS в роутере не задан намеренно: иначе устройства смогут
обходить фильтрацию при нормальной работе AdGuard.

## Проверка

На Proxmox:

```bash
pct status 106
pct exec 106 -- systemctl status AdGuardHome
dig @192.168.0.5 example.com
dig @192.168.0.5 doubleclick.net
dig @192.168.0.5 proxmox.home.arpa
dig @192.168.0.5 home-assistant.home.arpa
```

Ожидаемый ответ для заблокированного домена:

```text
0.0.0.0
```

## Повторное развертывание

На Proxmox из корня репозитория:

```bash
sudo ./scripts/create-adguard-lxc.sh
```

Скрипт создаёт LXC 106, проверяет SHA-256 закреплённого релиза, устанавливает
AdGuard Home, применяет upstream, cache, retention, HaGeZi и DNS rewrites,
генерирует пароли и выводит их один раз. Настройка DNS в DHCP роутера выполняется
отдельно после успешной проверки контейнера.

## Отказоустойчивость

Если Proxmox или LXC 106 выключен, клиенты с этой DHCP-конфигурацией не смогут
разрешать новые DNS-имена. Для полноценного резервирования второй AdGuard должен
работать на другом физическом устройстве. Публичный DNS как Secondary повышает
доступность, но делает фильтрацию необязательной для клиентов.

Клиентские DoH/Private DNS могут обходить локальный DNS. Их нужно отключать или
ограничивать отдельно на устройствах и firewall.
