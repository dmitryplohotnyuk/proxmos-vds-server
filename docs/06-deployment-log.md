# Deployment Log

## 2026-07-15

Создан unprivileged LXC:

```text
CT ID: 100
Hostname: domain-router
OS: Debian 12.12
CPU: 1 core
RAM: 1024 MB
Swap: 512 MB
Disk: 8 GB on local-lvm
Network: DHCP on vmbr0
MAC: BC:24:11:2B:16:61
Current IP: 192.168.0.173
Private bridge: vmbr1, 10.77.0.0/24
Private IP: 10.77.0.2/24
Autostart: enabled, order 1
```

Установлено:

```text
Caddy 2.6.2 (Debian package)
cloudflared 2026.7.1 (official amd64 deb)
Python 3.11
PyYAML
curl
jq
nftables
Tailscale 1.98.9
```

Развернуты файлы:

```text
/usr/local/bin/domain-router
/etc/domain-router/routes.yml
/etc/caddy/Caddyfile
/etc/sysctl.d/99-domain-router.conf
/etc/nftables.conf
/usr/local/sbin/domain-router-network-watchdog
/etc/systemd/system/domain-router-network-watchdog.service
/etc/systemd/system/domain-router-network-watchdog.timer
/etc/systemd/system/nftables.service.d/tailscale-order.conf
/etc/cloudflared/config.yml
/etc/cloudflared/3967b53d-d8e8-4f1b-aa23-38105aa9eb62.json
/etc/systemd/system/cloudflared.service
```

Caddy слушает `127.0.0.1:8080`; административный API доступен только на
`127.0.0.1:2019`. При неизвестном домене возвращается `HTTP 404`.

## Проверка

Временный upstream был запущен на `127.0.0.1:9000`. Проверен маршрут
`app.example.com`:

```text
add       -> HTTP 200
disable   -> HTTP 404
enable    -> маршрут снова активен
remove    -> маршрут удален
```

После проверки временный upstream и тестовый маршрут удалены. Caddy активен,
список маршрутов пуст. Позднее в этот же день `cloudflared` был авторизован и
запущен как постоянный сервис.

## Переносимая сеть

Создан внутренний bridge `vmbr1` без физического порта:

```text
Proxmox:      10.77.0.1/24
domain-router 10.77.0.2/24
Applications 10.77.0.10-10.77.0.199/24
Gateway:      10.77.0.2
```

В `domain-router` включены IPv4 forwarding и nftables masquerade из
`10.77.0.0/24` через DHCP-интерфейс `eth0`. Исходящий доступ проверен временным
клиентом `10.77.0.254`: ping до `1.1.1.1` прошел без потерь.

Установлен DHCP watchdog. Он выполняет проверку каждые 30 секунд и после трех
неудач обновляет аренду `eth0`. После перезагрузки LXC проверены оба интерфейса,
NAT, forwarding, timer, Caddy и доступ в интернет.

## Приватный доступ через Tailscale

Контейнеру передан `/dev/net/tun`, установлен и авторизован Tailscale:

```text
Hostname:       domain-router
Tailscale IPv4: 100.103.127.113
MagicDNS:       domain-router.tail6ace7f.ts.net
Tailscale SSH:  enabled
Subnet route:   10.77.0.0/24, approved
Key expiry:     disabled
Health:         no errors
```

В nftables добавлен явный forwarding `tailscale0` -> `lan0`. Сервис nftables
запускается перед `tailscaled`, чтобы `flush ruleset` происходил до создания
служебных цепочек Tailscale. Полная проверка доступа к `10.77.0.1` и внутренним
VM остается на клиентском компьютере после установки Tailscale.

После контрольной перезагрузки LXC подтверждено:

```text
nftables, tailscaled, Caddy и DHCP watchdog: active
Tailscale: online, health без ошибок
Subnet route: 10.77.0.0/24 сохранен
eth0: 192.168.0.173/24 (DHCP)
lan0: 10.77.0.2/24 (постоянный)
tailscale0: 100.103.127.113/32 (постоянный)
10.77.0.1: доступен
1.1.1.1: доступен
Caddy для неизвестного Host: HTTP 404
```

## Cloudflare Tunnel

Домен `content-factory-vps.win` активирован в Cloudflare. Создан и запущен
именованный туннель:

```text
Name:       proxmox-domain-router
Tunnel ID:  3967b53d-d8e8-4f1b-aa23-38105aa9eb62
Protocol:   QUIC
Origin:     http://127.0.0.1:8080
DNS:        content-factory-vps.win
DNS:        *.content-factory-vps.win
```

`cloudflared` работает как systemd-сервис и автоматически запускается вместе с
LXC. Проверка окружения Cloudflare завершилась без ошибок: DNS, QUIC, HTTP/2 и
доступ к API доступны.

Для сквозного теста временно создан маршрут
`tunnel-test.content-factory-vps.win` на локальный HTTP-сервис. Внешний запрос
вернул `HTTP/2 200` через Cloudflare. После теста маршрут и сервис удалены;
неизвестный wildcard-поддомен возвращает `HTTP/2 404` с ответом `Unknown domain`.
После перезагрузки LXC туннель автоматически восстановил четыре edge-соединения,
а внешний HTTPS-запрос прошел с корректной проверкой TLS.

## Следующий этап

1. Подключить первую реальную VM/LXC к `vmbr1`.
2. Назначить ей постоянный адрес из диапазона `10.77.0.10-10.77.0.199`.
3. Добавить реальный маршрут через панель или CLI и проверить HTTPS извне.

## Веб-панель управления

Реализована и развернута приватная панель Domain Router:

```text
URL:       https://domain-router.tail6ace7f.ts.net
Origin:    http://127.0.0.1:8090
Access:    Tailscale Serve, HTTPS, tailnet only
User:      domain-router-ui
Database:  /var/lib/domain-router-ui/app.db
```

Tailscale Funnel отключен; панель не публикуется в интернет.

Перед развертыванием создан snapshot LXC
`before-admin-panel-20260715`. Установлены `python3-venv` и `sudo`, приложение
размещено в `/opt/domain-router-ui`, зависимости изолированы в отдельном
virtualenv.

Развернуты дополнительные файлы:

```text
/opt/domain-router-ui/source/
/opt/domain-router-ui/venv/
/var/lib/domain-router-ui/app.db
/etc/systemd/system/domain-router-ui.service
/etc/sudoers.d/domain-router-ui
/usr/local/bin/domain-router -> /opt/domain-router-ui/venv/bin/domain-router
/usr/local/bin/domain-router-ui-admin
```

Существующий CLI разделен на общий Python core и CLI-обертку. Добавлены:

- allowlist домена `content-factory-vps.win`;
- allowlist upstream-сети `10.77.0.0/24`;
- exclusive lock `/run/lock/domain-router.lock`;
- транзакционный rollback Caddyfile и routes.yml;
- JSON-вывод для панели;
- безопасная команда проверки upstream.

Панель использует Argon2id, серверные сессии в SQLite, `Secure`/`HttpOnly`/
`SameSite=Strict` cookie, CSRF token для изменяющих операций, rate limiting входа
и audit log. Uvicorn работает от непривилегированного пользователя и слушает
только `127.0.0.1:8090`.

Приемочная проверка:

```text
локальные tests:                         8 passed
desktop browser:                        passed
mobile 390x844, horizontal overflow:    none
production login:                       passed
route add -> CLI -> Caddyfile:           passed
route delete and cleanup:               passed
audit login/add/remove:                  passed
Tailscale Serve HTTPS/TLS:               passed
public Cloudflare unknown host:          HTTP 404
reboot and service recovery:             passed
```

После проверки временный маршрут `panel-test.content-factory-vps.win` удален;
production `routes.yml` снова пуст. Первый администратор `admin` создан, пароль
в документацию не записывается и должен быть заменен через страницу `Профиль`.

## Воспроизводимый комплект

Создан самостоятельный Git-репозиторий `domain-router-deployment`. В него
включены исходники CLI и панели, tests, systemd/sudoers/nftables templates и
полный набор сценариев:

```text
scripts/create-proxmox-lxc.sh
scripts/deploy-from-proxmox.sh
scripts/install-lxc.sh
scripts/install-admin-panel.sh
scripts/configure-integrations.sh
scripts/verify-deployment.sh
```

Зафиксированы три пути восстановления: Bash, ручная инструкция и Codex. Tunnel
credentials, Tailscale auth keys и пароль администратора исключены из Git.
Проверка shell syntax выполнена; тесты Python после сборки комплекта: `8 passed`.
`shellcheck` на рабочем Mac отсутствовал, поэтому отдельная проверка этим
инструментом не выполнялась.

## Первая application VM

Создана и включена в автозапуск VM:

```text
VM ID:       101
Name:        content-factory
OS:          Ubuntu Server 24.04.4 LTS
CPU:         host, 1 socket, 10 cores
RAM:         24576 MB, balloon disabled
Disk:        local-lvm, 300 GB thin, SSD/discard/iothread enabled
Network:     virtio on vmbr1
IP:          10.77.0.10/24
Gateway:     10.77.0.2
DNS:         1.1.1.1
Autostart:   enabled, order 2
```

Образ `noble-server-cloudimg-amd64.img` загружен с официального Ubuntu Cloud
Images и проверен по `SHA256SUMS`. Cloud-init расширил root filesystem до всего
диска и установил `qemu-guest-agent`. Root SSH по паролю разрешен по явному
cloud-init template; пароль в документацию не записан.

Проверено:

```text
root SSH authentication:       passed
Ubuntu version:                24.04.4 LTS
visible CPU:                   10
visible memory:                23 GiB
root filesystem:               290 GiB usable
private IP and default route:  passed
internet through NAT:          passed
qemu-guest-agent:              active
VM reboot and root SSH:        passed
```

Добавлен маршрут:

```text
content-factory-vps.win -> http://10.77.0.10:80
```

Caddy config валиден, Caddy/cloudflared/Tailscale активны. Внешний HTTPS-запрос
доходит через Cloudflare и возвращает ожидаемый `502`, потому что на чистой
Ubuntu веб-сервер еще не установлен.

Диск VM занимает 300 GB виртуального пространства из примерно 349 GB thin pool.
Фактически блоки выделяются по мере записи, но заполнение `local-lvm` нужно
контролировать: `pvesm status` и `lvs -o+data_percent,metadata_percent`.

## Tailscale непосредственно на Proxmox

На Proxmox VE 9.2.4 / Debian 13 установлен официальный Tailscale `1.98.9` и
авторизовано отдельное устройство:

```text
Hostname:       proxmox
Tailscale IP:   100.93.12.80
MagicDNS:       proxmox.tail6ace7f.ts.net
Accept routes:  false
Service:        enabled, active
```

Настроен Tailscale Serve:

```text
https://proxmox.tail6ace7f.ts.net
  -> https+insecure://127.0.0.1:8006
```

Funnel сброшен и не включен. Проверка с Mac вернула `HTTP 200`, TLS verify `0`,
сертификат Let's Encrypt для `proxmox.tail6ace7f.ts.net`. Обычный root SSH по
`100.93.12.80` проверен без включения Tailscale SSH.

После контрольной перезагрузки всего Proxmox подтверждено:

```text
tailscaled:                enabled, active
Tailscale IP:              preserved
Tailscale Serve:           preserved
LXC 100 domain-router:     running
VM 101 content-factory:    running
Caddy/cloudflared:         active
content-factory route:     preserved
Proxmox private HTTPS:     HTTP 200, valid TLS
```

## 2026-09-25: уменьшение VM content-factory

Перед изменением проверено состояние VM: root filesystem был занят на `1%`,
каталоги `/root`, `/home`, `/opt` и `/srv` не содержали прикладных данных, из
сетевых сервисов работал только SSH. Существующий диск был отсоединен и сохранен
как rollback до завершения приемочных проверок.

VM пересоздана из того же официального Ubuntu Server 24.04 cloud image со
следующими текущими параметрами:

```text
VM ID:       101
Name:        content-factory
CPU:         6 vCPU
RAM:         12288 MB, balloon disabled
Disk:        150 GB thin SSD
Root FS:     145 GiB, 143 GiB available after deployment
IP:          10.77.0.10/24
Gateway:     10.77.0.2
Autostart:   enabled, order 2
```

Cloud-init template дополнен явной генерацией SSH host keys перед запуском
SSH. После исправления `cloud-init status --long` показывает `done` без ошибок.

Проверено:

```text
root SSH by password:        passed
qemu-guest-agent:            active
internet through NAT:        passed
domain route:                preserved
VM reboot:                   passed
Caddy/cloudflared/Tailscale: active
```

После успешной проверки старый 300-GB rollback-диск физически удален. Состояние
`local-lvm` после очистки:

```text
Thin pool total:             348.82 GiB
Physical data usage:         2.30%
Physical available:          about 341 GiB
Unreserved logical capacity: about 190 GiB
```

## 2026-10-06: watchdog физического Ethernet

После инцидента `e1000e Hardware Unit Hang` на Proxmox установлен отдельный
watchdog встроенного Intel I219-V:

```text
Service:          proxmox-nic-watchdog.service
Timer:            proxmox-nic-watchdog.timer
Check interval:   30 seconds
Cooldown:         5 minutes
Automatic reboot: disabled
```

Watchdog реагирует только на точную ошибку драйвера в журнале текущей загрузки.
Safe dry-run, обычный запуск без события и доступность шлюза через `vmbr0`
проверены. Установка не прерывала сеть; все шесть VM/LXC остались запущены.

## 2026-10-09: AdGuard Home

Создан отдельный unprivileged LXC 106 `adguard-home` со статическим адресом
`192.168.0.5/24`: 1 core, 512 MiB RAM, 512 MiB swap и 4 GiB thin disk.
Автозапуск включен с order 1.

Установлен AdGuard Home `v0.107.79`, SHA-256 официального архива проверен.
Панель работает на `http://192.168.0.5:3000`, DNS слушает TCP/UDP 53. Проверены
обычное разрешение имён, блокировка `doubleclick.net` и восстановление сервиса
после перезапуска LXC.

На TP-Link Archer C80 Primary DNS DHCP изменён с `0.0.0.0` на
`192.168.0.5`; Secondary DNS оставлен незаданным. DHCP-пул роутера
`192.168.0.100-192.168.0.249`, поэтому статический `.5` находится вне пула.
IPv6 на роутере отключен. Пароли в репозиторий не добавлены.

Дополнительно настроены Cloudflare DoH fallback, optimistic cache, хранение
query log 7 дней, HaGeZi Multi NORMAL и локальные записи
`proxmox.home.arpa` / `home-assistant.home.arpa`. Archer C80 не отвечает на PTR,
поэтому private PTR через роутер не включен. Пароль веб-администратора изменён
вручную и в документации не хранится.

## 2026-10-10: Tailscale gateway и автоматическая вкладка

Создан CT 107 `tailscale-gateway`, одобрены private /24 и Home Assistant /32
routes. С CT 100 отозвано объявление subnet; его HTTPS-панель и SSH сохранены.
Подробности и результаты проверок: [11-tailscale-gateway.md](11-tailscale-gateway.md).

На Proxmox установлен автоматический реестр сервисов; в админке `/internal`
добавлены группировка по гостю/IP, поиск, TCP-пробы со шлюза и индивидуальные
переключатели Tailscale-доступа. Обнаружено 27 сервисов, 12 существующих
разрешений сохранены. Новые сервисы выключены по умолчанию.

OFF/ON Jellyfin через рабочую панель, независимость LAN/TorrServer и сохранение
OFF после сканирования проверены. Временная проверочная сессия отозвана.
Пройдено 28 application tests; адреса, настройки безопасности и восстановление:
[12-internal-services.md](12-internal-services.md). Секреты в Git не добавлялись.

В таблице публичных маршрутов действия заменены на иконки с подсказками,
включение перенесено в колонку состояния. Подтверждение удаления сохранено.
Строка доменов над заголовком удалена. Desktop и мобильная версия 320/390 px
проверены; настройки самих публичных маршрутов не изменялись.

## 2026-10-10: Документация Cloudflare в админке

Добавлена защищённая авторизацией вкладка `/docs` с оглавлением и единым
Markdown-исходником инструкции. Описаны NS/DNSSEC, CNAME в текущий Tunnel,
Proxy status, серверный список разрешённых зон, добавление маршрута,
HTTPS и диагностика. Источники: официальная документация Cloudflare.
Ссылка в репозитории: [13-cloudflare-domain-guide.md](13-cloudflare-domain-guide.md).

Добавлена root-обёртка CLI для ручного подключения новых зон в старых
установках. Она поставлена только как файл исходников, но не установлена в
`/usr/local/bin`: действующая маршрутизация не менялась.

Пройдено 30 тестов. В работающей панели проверены авторизация, оглавление,
неизменность трёх маршрутов и healthz. Мобильная ширина 320 px без
горизонтального переполнения. Повторная проверка Tailscale/LAN: Jellyfin,
TorrServer, AdGuard DNS и блокировки, Home Assistant, Proxmox и админка
доступны; запрещённый SMB-порт закрыт. DNS, Tunnel и firewall не изменялись.

## 2026-10-10: Управление подключениями

Добавлена вкладка `/integrations`: состояние Cloudflare Tunnel, актуальный
DNS Target с копированием, безопасная замена credentials JSON / connector token
и восстановление предыдущей конфигурации. Tailscale отображается отдельно для
админки и шлюза сервисов; доступны первичная авторизация, возобновление подключения
и восстановление HTTPS админки без выхода из текущего аккаунта.

Операции защищены сессией, CSRF, подтверждением и повторным вводом пароля.
Root-агенты принимают только фиксированные действия; секреты не возвращаются
в интерфейс, аудит или репозиторий. Установлен независимый systemd rollback timer.
Архитектура и восстановление: [14-integration-manager.md](14-integration-manager.md).

Пройдено 60 тестов. На действующем Tunnel проверены применение тех же credentials,
готовность connector, откат к исходной конфигурации и реальное восстановление
независимым 90-секундным таймером. Первичная авторизация Tailscale проверена на
изолированном userspace-демоне без регистрации нового устройства в tailnet.
Подключение уже работающего Tailscale не меняет его профиль и настройки.

Статический UUID удалён из инструкции Cloudflare: она ссылается на актуальный
Target в Подключениях. DNS и существующие маршруты автоматически не меняются.

## 2026-10-10: Аудит И Публикация Репозитория

Проверены полнота инструкций, зависимости скриптов, упаковка приложения,
локальные Markdown-ссылки и типовые секреты в исходниках/достижимой Git-истории.
Добавлены полный порядок восстановления, отдельная безопасная процедура
обновления, полный backup-перечень и явные ограничения для прикладных VM/CT.
Исправлены зависимости, резерв адресов, упаковка только Git source files и
расхождение default allowed zones с действующей установкой.

Полный набор: 64 теста. Bash/Python/JavaScript syntax checks и сборка wheel
пройдены; добавлен GitHub CI. Известные пароли и типовые credentials не найдены
в публикуемых файлах и истории. Production в рамках этого аудита не менялся.
Подробности и остаточные ограничения: [15-repository-audit.md](15-repository-audit.md).

## 2026-10-11: Иконки Навигации И Селект

Для Подключений добавлена Lucide plug, Внутренние адреса сохраняют network.
Нативный фильтр сервисов получил отдельную chevron-down: центрирование независимо
от системного оформления, одинаковая высота с поиском, focus-visible и fallback
для forced colors. Фильтрация и длинный пункт проверены на ширинах 1280/320 px,
горизонтального переполнения нет. Пройдено 65 тестов. Обновление установлено в
CT 100, healthz OK; три публичных маршрута и настройки VPN не изменялись.

## 2026-10-11: Отдельное Хранилище Доступов

Создан unprivileged CT 108 `vaultwarden`: Debian 12, 1 CPU, 1024 MiB RAM,
256 MiB swap, 8 GiB disk, onboot/startup order 2. Private IP `10.77.0.4`;
LAN DHCP на момент установки `192.168.0.226`. Root заблокирован, SSH выключен.
Vaultwarden 1.37.4 закреплён по digest; слушает только loopback внутри LXC.
Собственный userspace Tailscale Serve: `https://vaultwarden.tail6ace7f.ts.net`.
Funnel и публичный Cloudflare-маршрут отсутствуют; subnet routes не объявляются.

Владелец подтвердил Tailscale и создал аккаунт со своим мастер-паролем.
Проверено: один аккаунт, регистрация закрыта, web-admin выключен. MFA на момент
проверки ещё не включена; это отдельное действие владельца.
С разрешения владельца сохранены семь известных доступов: Proxmox root,
домашний роутер, AdGuard admin, последний известный Ubuntu root, Jellyfin admin,
MeshMonitor admin и существующий Jellyfin API key интеграции. Все семь связаны
с реестром Domain Router; переход к реальной записи проверен. Актуальность старых
данных, кроме проверенного Proxmox, не подтверждена повторным входом, что отмечено
в записях. Пароли не менялись; содержимое vault в документации отсутствует.

В CT 100 добавлена вкладка `/access`: только service identity, название, логин,
способ входа и UUID Vaultwarden. Секреты и unlock-сессии UI не получает.
Доступны несколько аккаунтов на сервис, ручные записи и удаление только привязки.
CSRF, escaping, optimistic concurrency и аудит без содержимого записей проверены.
Опциональный CLI-hook сохраняет новый пароль при создании content-factory VM;
универсальная ротация и автоматическое извлечение старых секретов не реализованы.

Ежедневный host timer в 04:20 с jitter создаёт age-encrypted backup, retention
14 дней в CT / 30 на Proxmox. Приватная age identity сгенерирована только на
компьютере владельца; на Proxmox передан public recipient. Первые копии выгружены
на компьютер, stream-decryption и SQLite integrity проверены. Автоматический
off-host target пока не настроен; полное восстановление с входом владельца не
проверялось. Инструкции: [16-access-vault.md](16-access-vault.md).

Перед обновлением UI сохранены исходники/venv и согласованная SQLite-копия
в `/var/backups/domain-router-ui/before-vault-20261011.*` внутри CT 100.
Существующие пароли, маршруты и политики шлюза не менялись. Локально пройдено
82 теста; проверка исходников и Git-истории на типовые секреты пройдена.
Smoke tests: Jellyfin API/web/private/LAN, TorrServer, AdGuard web/DNS/блокировки,
Home Assistant, Proxmox HTTPS, Domain Router HTTPS пройдены; запрещённый SMB-порт
не доступен через шлюз. Desktop UI проверен без overflow и отсутствующих изображений.

## 2026-10-11: Группировка Доступов И SSH

Вкладка `/access` переведена на раскрывающиеся группы по guest identity и IP,
как `/internal`. Общий IP/LAN показывается один раз, внутри находятся сервисы и
несколько аккаунтов. Добавлены ручные узлы с валидируемыми названием/IP, безопасная
миграция SQLite, сохранение раскрытия в browser sessionStorage и поиск по портам.
Desktop 1280 и mobile 390/320 проверены: нет overflow, изображения доступны.

По отдельному явному выбору владельца установлен уникальный root-пароль и разрешён
root SSH в CT 100/104/105/106/107 и VM 101/103. Все пароли сначала сохранены в
Vaultwarden; существующие SSH-ключи не изменялись. Реальный новый парольный вход
проверен без multiplexing на всех семи гостях. Proxmox root не менялся.
Tailscale SSH CT 100 с проверкой личности сохранён: обычный пароль проверен по LAN.
HA OS 102 и CT 108 (SSH выключен) не изменялись для этой задачи.

В vault и UI теперь 13 записей, включая 8 серверных root-доступов с Proxmox.
В UI хранятся только ссылки и метаданные. Универсальная смена паролей из UI всё
ещё не реализована; административный opt-in скрипт описан в документе 16.
88 тестов пройдены. Jellyfin, TorrServer, AdGuard DNS/блокировки, Home Assistant,
Proxmox и Domain Router smoke tests пройдены; SMB через шлюз по-прежнему запрещён.
Новая age-копия выгружена off-host: проверены расшифровка, SQLite integrity,
один аккаунт и 13 encrypted items. Временные локальные файлы новых паролей удалены.

## 2026-10-11: Комплексный Аудит

Проверены ресурсы, SMART, службы всех LXC, VM 101/103, авторизация UI,
root agents, состояние Vaultwarden, ключи Tailscale и существующие backup.
Результаты и оставшиеся риски: [17-system-audit.md](17-system-audit.md).

В `/access` добавлены scopes (по умолчанию учётные записи), мгновенный поиск,
копирование shell-safe SSH-команд и состояния доступности. В подключениях
показан срок ключа панели/шлюза с предупреждением менее чем за 30 дней.
Argon2 UI вынесен из event loop, не более двух проверок одновременно.

На Proxmox установлен `age` из Debian и ежедневный `control-plane-backup.timer`
04:45 + 0–10 минут. Архив включает SQLite online backup с удалёнными только
в копии сессиями, routes/Caddy/Cloudflare, UI drop-ins, gateway policy и
host inventory/network metadata. Приватная age identity не передавалась.
Финальная копия `control-plane-20261011T071403Z.tar.age` проверена на Mac:
84 archive members, SQLite integrity OK, 12 access links, один администратор.
Vault отдельно: один аккаунт, 13 encrypted items, SQLite OK. Привязки из vault
автоматически не восстанавливались. Резервирование не заменяет backup дисков VM
и пока не имеет автоматической off-host доставки/уведомлений.

99 тестов, syntax checks и source/history credential-pattern scan пройдены.
Частные HTTP/DNS smoke tests пройдены, SMB через gateway заблокирован.
Три публичных маршрута не изменены и отвечают HTTP 302. В CT 106 выявлены
существующие `226/NAMESPACE` у вспомогательных systemd-служб; AdGuardHome,
networking и SSH активны, ротация логов вручную выполнена. Защита LXC не ослаблялась.
MFA, обновления гипервизора и постоянное исправление CT 106 требуют следующих
действий владельца/согласованного окна. Пароли и ACL в аудите не менялись.
