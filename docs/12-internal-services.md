# Автоматический реестр внутренних сервисов

## Назначение

Вкладка `/internal` в приватной админке Domain Router показывает VM/LXC,
их LAN/IP через Tailscale и обнаруженные сервисы. Один раскрывающийся узел
содержит свои порты, ссылки, состояние TCP и индивидуальные переключатели.
Поиск работает по имени гостя, адресу, сервису и порту. Раскрытые группы и
фильтры сохраняются в текущей сессии браузера после обновления страницы.

Адрес текущей установки: `https://domain-router.tail6ace7f.ts.net/internal`.
Используется существующая авторизация панели. Публичного доступа к ней нет.

Переключатель управляет только пересылкой через CT 107. Он не останавливает
приложение, не меняет LAN, DNS, Cloudflare, Caddy или доступ к самому Proxmox.
Если у приложения есть другой путь доступа, например собственный Tailscale
агент или публичный домен, данный переключатель его не блокирует.

## Архитектура

```text
Browser -> Tailscale Serve HTTPS -> CT 100 /internal
   -> restricted SSH service-inventory@10.77.0.1
   -> root JSON agent on Proxmox
       -> pvesh + pct exec / qm guest agent: interfaces and ss
       -> persistent inventory + per-service decisions
       -> CT 107 nftables: validate, apply, persist
Remote client -> Tailscale -> CT 107 -> allowed service
```

- Обнаружение: systemd timer `service-inventory-sync.timer` на Proxmox,
  через 60 секунд после завершения предыдущего сканирования.
- Браузер обновляет данные каждые 30 секунд, кроме ввода поиска/фильтра.
- Сканируются слушающие TCP-порты и DNS UDP 53. Loopback, link-local,
  multicast discovery и внутренние Docker-сети не публикуются.
- В VM требуется рабочий QEMU guest agent и `ss`; LXC нужны `ip` и `ss`.
- CT 100 и CT 107 исключены: это инфраструктура с независимым доступом.
- Все новые сервисы появляются с выключенным VPN-доступом. При первой
  установке сохраняются разрешения известных активных сервисов из этапа 11.
- Идентичность гостя включает MAC/SMBIOS: при изменении идентичности старые
  разрешения не переносятся автоматически только по совпавшему VMID.
- Недоступный гостевой агент или исчезнувший порт сохраняет запись с пометкой
  отсутствия свежих данных; пересылка такой записи запрещается после сканирования.
- Ошибка общего сканирования оставляет предыдущую конфигурацию; проверяйте
  время обновления и журнал таймера. Неполный первый скан не заменяет firewall.

Проверка TCP выполняется с CT 107, используя его доверенный адрес. Это проверка
участка шлюз -> приложение, не гарантия маршрута на конкретном клиенте.
Приложение всё равно может требовать логин, сертификат или доверенную подсеть.

## Адреса

При наличии адреса `10.77.0.0/24` он используется напрямую. Для сервиса,
слушающего только LAN, CT 107 выполняет DNAT конкретного порта на LAN-адрес.
LAN-only гости получают постоянный виртуальный IP из `10.77.0.128-239`;
новая DHCP-выдача меняет цель DNAT, но не адрес в панели. Этот диапазон нужно
зарезервировать и не выдавать вручную другим VM/LXC. Обнаруженная коллизия
не перенаправляется молча, а отражается ошибкой.

Исключение совместимости: существующий Home Assistant использует
`192.168.0.222/32`, уже одобренный в tailnet. Остальные виртуальные адреса
находятся в уже одобренном `10.77.0.0/24`, дополнительных route approvals не нужно.
Текущая реализация рассчитана на LAN `192.168.0.0/24`, private `10.77.0.0/24`,
Proxmox `10.77.0.1`, панель CT 100 и шлюз CT 107. При смене этой топологии
нужно согласованно изменить агент, установщик, firewall и Tailscale routes.

## Безопасность

- UI не получает root-ключ или пароль Proxmox.
- Отдельная SSH identity хранится в CT 100:
  `/var/lib/domain-router-ui/service-inventory_ed25519`, mode 0600.
- На Proxmox ключ ограничен `from="10.77.0.2"`, `restrict` и forced command.
  Shell, PTY, forwarding и произвольные команды гостям недоступны.
- Sudo разрешает только `/usr/local/sbin/service-inventory-agent` без аргументов;
  удалённый вызов `--sync` не разрешён. Агент принимает JSON `list` или `toggle`
  существующей записи, строго проверяет поля и булевы значения. Начиная с этапа
  14 также разрешены фиксированные `integrations-status` / `integration-action`;
  их ограничения описаны в [управлении подключениями](14-integration-manager.md).
- SSH host key закрепляется из `/etc/ssh/ssh_host_ed25519_key.pub` самого хоста,
  без отключения проверки known_hosts.
- POST требует сессию и CSRF для конкретного сервиса. Старая форма при
  конкурентном изменении отклоняется; действия записываются в журнал админки.
- Общий lock защищает решения от перезаписи таймером. nft проверяется до
  применения, меняется только таблица `inet tailscale_gateway`, без flush ruleset.
- Запрет стоит до established: OFF блокирует и новые, и текущие потоки этого
  сервиса через шлюз. Boot-конфигурация сохраняется в CT 107.
- Tailnet ACL и key expiry этой установкой не меняются. Их ограничения всё
  равно должны разрешать нужные адреса/порты; широкая политика не заменяет
  осторожность при добавлении других пользователей в tailnet.

## Развертывание: Bash

Сначала выполнить основной rebuild и этап 11: CT 100 с админкой, CT 107 с
одобренными routes, адреса сервисов и их доверенные сети. Сохранить резервные
копии source/DB панели и `/etc/tailscale-gateway.nft` шлюза.

Обновить исходники в CT 100 из этого репозитория и выполнить внутри CT 100:

```bash
bash /opt/domain-router-ui/source/scripts/install-admin-panel.sh /opt/domain-router-ui/source
```

Затем на Proxmox, в каталоге репозитория:

```bash
bash scripts/install-service-inventory.sh "$PWD"
systemctl status service-inventory-sync.timer
journalctl -u service-inventory-sync.service -n 30
```

Установщик идемпотентен: не пересоздаёт ключ, не сбрасывает решения и не
пересоздаёт гостей. При необходимости устанавливает sudo на хост и SSH client
в CT 100. Обновлять нужно обе части: Python UI в CT 100 и root-агент на хосте.

## Развертывание: Вручную

Точные команды и права находятся в `scripts/install-service-inventory.sh`.
Последовательность для ручной установки:

1. Установить обновлённый пакет UI с JavaScript/SVG assets в CT 100.
2. Скопировать `src/domain_router_services/agent.py` на хост в
   `/opt/service-inventory/agent.py`, wrapper в `/usr/local/sbin/service-inventory-agent`.
3. Создать root-only `/var/lib/service-inventory` и системного SSH пользователя
   `service-inventory`; установить ограниченный sudoers из `config/sudoers/`.
4. Сгенерировать ключ от имени `domain-router-ui` в CT 100. В authorized_keys
   установить ограничения из скрипта; записать доверенный host key в known_hosts.
5. Установить `config/systemd/service-inventory-ui.conf` в drop-in UI на CT 100.
6. Установить service/timer синхронизации на Proxmox, выполнить первый scan.
7. Включить timer, перезапустить UI и выполнить проверки ниже.

Не заменять forced command обычным SSH shell и не расширять sudo до pct/qm.

## Развертывание: Codex

Задача для повторного запуска:

> Прочитай документы 08, 11 и 12. Проверь существующие CT 100/107, сеть,
> guest agents, доступ и резервные копии. Установи текущую админку и агент через
> install-service-inventory.sh, сохрани решения и секреты вне Git. Не меняй
> tailnet ACL/key expiry без отдельного разрешения. Проверь группировку,
> авторизацию, OFF/ON Jellyfin, независимость LAN и сохранение после сканирования.

## Проверка И Эксплуатация

```bash
# На Proxmox:
systemctl start service-inventory-sync.service
journalctl -u service-inventory-sync.service -n 30
printf '{"action":"list"}' | /usr/local/sbin/service-inventory-agent
pct exec 107 -- nft list table inet tailscale_gateway
pct exec 100 -- systemctl is-active domain-router-ui caddy cloudflared tailscaled
# На отдельном Tailscale клиенте в домашней LAN, с root SSH к Proxmox:
python3 scripts/verify-internal-services.py --host root@192.168.0.176
CHECK_LAN=1 bash scripts/verify-tailscale-services.sh
# Локальные unit tests:
PYTHONPATH=src python3 -m pytest
```

`verify-internal-services.py` создаёт временную сессию существующего админа,
не меняет пароль, проверяет CSRF, временно выключает Jellyfin, проверяет LAN и
TorrServer, повторяет discovery, включает Jellyfin обратно и отзывает сессию
в finally. Токен не выводится и не записывается в файлы. На время этого теста
воспроизведение Jellyfin через Tailscale будет прервано.

Источник решений: `/var/lib/service-inventory/inventory.json` на Proxmox.
Сохранять его в защищённый backup вместе с базой панели и SSH identity.
Не включать приватный SSH ключ, app.db, Tailscale state, Cloudflare credentials
или root-пароли в Git. При чистом восстановлении ключ можно сгенерировать заново.

`config/tailscale-gateway.nft` теперь только стартовый allowlist для rebuild.
После включения агента не редактировать live nft вручную: следующий scan его
перегенерирует. Для отката остановить timer и дождаться завершения oneshot,
восстановить `/etc/tailscale-gateway.nft` из backup CT 107 и применить `nft -f`.
После восстановления предыдущего UI убрать его service-inventory drop-in.
Proxmox и CT 100 сохраняют независимые Tailscale административные пути.

## Результаты 2026-10-10

- Обнаружено 27 сервисов в 6 гостях; 12 ранее разрешённых сервисов включены.
- Новый список сгруппирован по гостю/IP; навигация и адаптивный дизайн обновлены.
- Действия публичных маршрутов представлены иконками с подсказками;
  включение находится в колонке состояния, удаление требует подтверждения.
- OFF/ON Jellyfin проверен через рабочий HTTPS endpoint панели.
- При OFF VPN недоступен, LAN и TorrServer доступны; scan сохраняет OFF.
- После ON Jellyfin снова доступен; временная сессия отозвана.
- Endpoint smoke tests, AdGuard DNS/blocking и отрицательная проверка SMB пройдены.
- 28 application tests пройдены; мобильные размеры 320/390 px без переполнения.
- Публичные доменные маршруты не изменялись. Ранее отмеченная 404 корневого
  content-factory-vps.win не исправлялась в рамках этой задачи.
