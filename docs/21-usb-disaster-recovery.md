# Недельные Комплекты Восстановления Proxmox

## Что Сохраняется

Состав гостей определяется заново при каждом запуске по текущим конфигурациям
Proxmox, включая выключенных гостей и templates. Новые VM/LXC автоматически
попадают в следующий комплект; удалённые не копируются. Если конфигурация или
состав меняются во время копирования, комплект не заменяет предыдущий успешный.

- Полные штатные snapshot-архивы vzdump всех VM/LXC, zstd, по одному гостю.
- Настройки хоста: `/etc` (включая `/etc/pve`), `/usr/local`, наш код в `/opt`,
  `/root` без shell history/cache, state сервисного SSH и Tailscale, snippets.
- SQLite online snapshot `/var/lib/pve-cluster/config.db` с integrity check,
  **только как аварийный reference**, не для записи поверх живого pmxcfs.
- Версии Proxmox/пакетов, сетевые интерфейсы/адреса, storage, сервисные аккаунты,
  исходные конфигурации гостей и список внешних зависимостей.
- Проверяемый manifest, SHA-256 всех файлов и переносимый recovery kit с кодом,
  инструкцией и исходниками проекта. Kit лежит также в корне backup-раздела.

**По решению владельца `/mnt/media-ssd` НЕ включён.** База и настройки Jellyfin
в rootfs CT 104 включены, медиафайлы на отдельном SATA SSD не копируются.
`/mnt/immich-photos` также не копируется на соседний раздел того же SSD:
для фототеки требуется отдельный физический носитель/удалённая копия.
Эти исключения явно записаны в manifest и показаны в панели. Неизвестный новый
исключённый mount/data disk останавливает задачу, а не создаёт ложный зелёный статус.

Vzdump не сохраняет историю старых снапшотов и состояние RAM. Snapshot с QEMU
guest agent улучшает согласованность filesystem, но не заменяет специальные
application-level backups для произвольной БД. Восстановление требует не более
старой версии Proxmox, достаточно RAM/диска, подходящих bridges и устройств.
Гости копируются последовательно, поэтому это не единый одновременный snapshot
всех приложений: записи, сделанные после копии конкретного гостя, попадут в
следующий комплект. Для связанных БД с жёсткой межсервисной согласованностью
требуется отдельная согласованная application-level процедура.

## Запуск И Хранение

Timer `proxmox-usb-backup.timer`: воскресенье 14:00 Europe/Kyiv + 0–5 минут.
`Persistent=false` не запускает пропущенную задачу при ночной загрузке. Первая
копия и компенсация пропуска выполняются health-службой только в дневном окне
10:00–18:00 Europe/Kyiv. Ручной запуск администратором остаётся возможен в любое
время. Проверка диска раз в минуту читает только mount/status и маленькие
конфиги, не опрашивает гостей через тяжёлый pvesh. При подключённом диске и
просроченной/отсутствующей копии повтор не чаще раза в час.
Окно задаётся `auto_retry_window` в config.json. Оно ограничивает время старта,
не прерывает уже работающую копию в 18:00. Ежедневные encrypted copies:
Vaultwarden в 12:00 (+0–15 минут), control-plane в 12:30 (+0–10 минут); их missed
запуски не компенсируются ночью, следующая попытка по дневному календарю.

Ограничения фоновой работы: nice 10, низкий I/O priority, 2 CPU на service,
vzdump до 128 MiB/s, один поток zstd, гости последовательно. Остановка гостей
для штатного backup не выполняется. Все restore проходят с выключенным start.
Для metadata непривилегированных LXC используется локальный `/var/tmp` и штатная
маска прав подпроцесса 0022. Это не открывает архивы: родительский каталог 0700,
готовые файлы 0600. Установщик блокирует обновление также в состояниях systemd
`activating`/`deactivating`, а не только `active`.

1. Проверяются точный UUID backup-раздела, место под весь новый комплект плюс
   8 GiB резерва, guest locks, исключения и наличие recovery kit.
2. Новые файлы создаются только в `.staging/set-...`, предыдущий комплект цел.
3. Каждый архив fsync, сброс подсказкой его page cache и затем проверка zstd и
   полным чтением TAR либо `vma verify -`. Глобальный cache хоста не сбрасывается.
4. Копируются host config и kit, считаются SHA-256, файлы/директории fsync.
5. Manifest и `COMPLETE.json` публикуются только после проверок; каталог атомарно
   переносится в `sets/`. Из `dump/` создаются hard links, без второго расхода
   места, чтобы архивы были видны в штатном интерфейсе Proxmox.
6. Только после этого старые **наши завершённые** комплекты удаляются. При
   активном restore очистка откладывается. Recovery helper держит shared lock
   `/run/lock/proxmox-usb-recovery.lock` уже на этапе проверки SHA, до появления
   native PVE restore task; pruner требует exclusive lock и при конфликте сохраняет
   старые копии. Чужие файлы/бэкапы не затрагиваются.

При ошибке, отключении USB, нехватке места, изменении состава гостей или плохом
архиве предыдущий успешный комплект не удаляется. Частичные файлы текущей
попытки удаляются только на том же ожидаемом mounted filesystem. Если диск
исчез, очистка не выполняется на системном диске. Старые аварийные `.staging`
после жёсткого выключения проверяются/удаляются администратором отдельно.
Read-only filesystem считается неисправным, а не доступным для нового backup.
Ошибка очистки частичного комплекта сохраняется отдельно и не маскирует
первичную ошибку архивации/чтения.
При integrity failure спорный комплект сохраняется в `.staging` для диагностики,
а почасовой health-retry блокируется (`auto_retry_blocked`). Это не успешный backup
и он не публикуется в `sets`/`dump`. Следующий ручной/календарный запуск сбрасывает
этот флаг, поэтому при непроверенном оборудовании администратор останавливает timer
и устанавливает `auto_retry=false`. После диагностики разбираются сохранённые
неполные комплекты; они не удаляются автоматически как завершённые backups.

Хранится один последний проверенный недельный комплект. Это защита от отказа
хоста, не полноценная история от незаметной порчи/шифровальщика. Желательна
вторая off-host/offline копия. Предыдущая неделя удаляется только после новой.

## Панель И Команды

Приватный URL: `https://proxmox.tail6ace7f.ts.net:8443`.
Backend слушает только `127.0.0.1:9130`. Tailscale Serve даёт HTTPS и проверенную
identity; разрешён только аккаунт владельца в `/etc/proxmox-usb-backup/ui.json`.
Чужие/отсутствующие identity получают 403. Существующий HTTPS Proxmox :443 сохранён.
Ссылка добавлена в Domain Router: «Доступы → Proxmox → Резервные копии Proxmox»;
в Vaultwarden: `Proxmox Backup / Tailscale`, без отдельного пароля, со ссылкой на
инструкцию и описанием входа через Tailscale.

UI работает как `pve-backup-ui` без root-доступа к архивам. Единственное действие
«Создать копию» использует CSRF + проверку Origin и Unix socket с peer-UID check.
Отдельный минимальный root-controller принимает только фиксированное действие
start системной backup service; произвольных команд/параметров API нет.
Нет download endpoint для секретных архивов. Статусы обновляются независимо
от USB, поэтому отсутствие диска видно и не ломает саму панель.
Статус старше трёх минут не считается актуальным: панель запрещает новый запуск.
Реальный read-only ext4 определяется по superblock FS-OPTIONS, а не по VFS-флагу
read-only защитного namespace health-службы; ProtectSystem=strict остаётся включён.

```bash
systemctl start --no-block proxmox-usb-backup.service
systemctl status proxmox-usb-backup.service
systemctl list-timers proxmox-usb-backup.timer
journalctl -u proxmox-usb-backup.service -n 50
cat /var/lib/proxmox-usb-backup/status.json
```

Подробный текущий job log: `/var/lib/proxmox-usb-backup/current.log`, root-only.
Конфигурация: `/etc/proxmox-usb-backup/config.json`. Смена расписания через
systemd override, затем `daemon-reload` и restart timer. При отсутствии USB
действует защита из этапа 20: nofail, короткий timeout, is_mountpoint и immutable
нижний каталог. Нельзя отключать диск во время записи. Остановить service и
штатно размонтировать оба раздела перед извлечением.

## Развёртывание С Нуля

После подготовки разделов этапа 20 на Proxmox из исходников:

```bash
bash scripts/install-proxmox-usb-backups.sh "$PWD"
```

Для **другого уже подготовленного** backup-носителя явно передать проверенный UUID:
`EXPECTED_BACKUP_UUID=NEW_UUID bash scripts/install-proxmox-usb-backups.sh "$PWD"`.
UUID существующей конфигурации не заменяется автоматически: при осознанной смене
носителя сначала проверить/обновить `/etc/proxmox-usb-backup/config.json`.
Без override установщик ожидает UUID текущего SSD, а не любой mount с нужным именем.

Установщик проверяет текущий backup UUID, устанавливает stdlib-only engine/UI,
unit/timer, kit, владельца доступа по Tailscale и новый HTTPS :8443. Не очищает
Serve и не меняет :443. Во время активной копии обновление кода запрещено.
Существующая конфигурация engine/allowlist сохраняется. Зависимости: стандартный
Proxmox + Python 3, zstd, vma, tailscale. Pip/npm на хосте не нужны.
URL/Origin панели определяются по фактическому `Self.DNSName` Tailscale нового
хоста; имя старого узла не зашито в HTTP API. Существующий allowlist сохраняется.
UI/controller перезапускаются при обновлении, другие сервисы не затрагиваются.
Установленный привилегированный код root:root без group/other write, UID автора
source tar не переносится в исполняемый код.

## Восстановление На Чистом Сервере

**Старый сервер должен быть выключен/изолирован.** Нельзя одновременно поднимать
его копии с теми же MAC/IP, SSH и Tailscale identities. На новом сервере установить
Proxmox той же или более новой версии; текущий источник 9.2.4. Подготовить target
storage (обычно `local-lvm`), достаточное место, физический uplink и консольный
доступ. Не пытаться восстановить старые partition UUID системного NVMe на новый.

### 1. Подключить USB Только Для Чтения

```bash
lsblk -f
mkdir -p /mnt/recovery
mount -o ro UUID=ca744010-53c5-4cde-846a-d66c60f4cf02 /mnt/recovery
export PYTHONPATH=/mnt/recovery/recovery
python3 -m proxmox_usb_backup.recovery list --root /mnt/recovery
python3 -m proxmox_usb_backup.recovery verify --root /mnt/recovery
```

Не форматировать SSD. Для другого подготовленного носителя использовать UUID
его backup-раздела. SHA-256 обнаруживает повреждения, но не доказывает
подлинность изменённого злоумышленником USB. Носитель должен быть доверенным.

### 2. Подготовить Настройки Хоста

```bash
python3 -m proxmox_usb_backup.recovery stage-host --root /mnt/recovery \
  --destination /root/pve-recovery
```

Команда проверяет все файлы и безопасно распаковывает host snapshot в отдельный
каталог. Просмотреть `/root/pve-recovery/host/etc/network/interfaces`, interfaces.d,
fstab и storage.cfg. Поднять необходимые пакеты (`age`, `sudo`, `curl`, `nftables`,
Tailscale из официального репозитория); список исходных версий есть в manifest.

После проверки, только на чистом хосте без гостей:

```bash
python3 -m proxmox_usb_backup.recovery apply-host --root /mnt/recovery \
  --destination /root/pve-recovery --network-map nic0=NEW_PHYSICAL_INTERFACE \
  --confirm --old-server-off
```

Восстанавливаются наши scripts/services/configs и сервисные аккаунты.
Служебный SSH home и authorized_keys остаются root-owned, сохраняя forced command.
Сеть не
активируется автоматически: кандидат находится в `/root/proxmox-network-candidate`.
Через локальную консоль проверить bridges, uplink, IP и gateway, перенести кандидат
в `/etc/network`, затем `ifquery --check -a`/`ifreload -a` по месту. Обновить имя
интерфейса в `/etc/default/proxmox-nic-watchdog`, если оно изменилось. Системный
fstab, storage.cfg, hostname, PVE access/cluster identity не перезаписываются:
сопоставить новые LVM/storage/UUID и точечно перенести нужные настройки из snapshot.
Старые root shadow/PVE tokens/ACL сохранены в архиве для отдельного осознанного
восстановления; root login свежего хоста по умолчанию не заменяется.

Для сохранения старых SSH/Tailscale identities есть отдельный opt-in
`--restore-identities` к apply-host: он останавливает tailscaled и переносит его
state и SSH host keys. Только при гарантированно выключенном старом сервере.
Без него требуется заново авторизовать Tailscale и обновить pinned SSH host key
в CT 100 (`service-inventory_known_hosts`) после проверки нового fingerprint.
SSH/Tailscale, networking и custom timers автоматически не перезапускаются:
сначала закончить проверку и конфигурацию нового железа.

**Никогда не копировать `reference/pmxcfs-config.db` поверх работающего pmxcfs.**
Это last-resort reference для отдельной offline-процедуры, а не штатный restore.

### 3. Восстановить Гостей Без Запуска

```bash
python3 -m proxmox_usb_backup.recovery guests --root /mnt/recovery \
  --storage local-lvm --confirm --old-server-off
```

IDs всех целей проверяются заранее: существующие гости не перезаписываются.
Для выборочного восстановления: `--ids 100 107`, при необходимости
`--map-id 100=200`. Нативные `qmrestore`/`pct restore` восстанавливают диски и
конфигурации. Все восстановленные гости получают `onboot=0` и остаются выключены.
Если восстановление прервалось после нескольких гостей, проверить созданные IDs
и повторить с `--ids` только для оставшихся. Не удалять рабочие гости ради повторного
запуска и не применять `--force`; неудачную частичную копию разбирать отдельно.
Hookscript, USB/PCI passthrough и host bind mounts снимаются с активной конфигурации
для проверки нового железа; исходные значения сохранены в manifest и выводятся
CLI. Сохранить этот вывод. Не заменять bind mounts пустыми writable каталогами.

Переподключить SATA с медиа в правильный UUID mount, затем вернуть CT 104 mp0.
Без исходного SATA медиафайлов не будет: это намеренное исключение владельца.
USB Zigbee/Meshtastic перепривязать по проверенным VID/PID/port path нового хоста.
Проверить network bridges и guest IP. Не включать guest autostart до этого.

### 4. Запуск И Проверки

Сначала CT 100 Domain Router и CT 107 gateway, затем DNS и остальные сервисы.
Проверить публичные сайты, Tailscale routes, Jellyfin, Vaultwarden, AdGuard,
Home Assistant/USB и SSH. Только после проверки вернуть исходные onboot из
manifest (`pct set ID --onboot 1` / `qm set ID --onboot 1`). Если гость был
выключен перед резервированием, не запускать его без необходимости (`was_running`).
Перенастроить backup USB mounts на новом хосте по этапу 20 без initialization,
затем установить/включить backup timers и приватную панель из source kit.

Исходники проекта лежат в `recovery/project-source.tar.gz`: распаковать в отдельный
root-каталог, использовать инструкции/скрипты. Для работы recovery helper не нужны
GitHub, исходный сервер или ключи от старого root: нужен чистый Proxmox и этот USB.
Для возврата внешнего Tailscale/Cloudflare могут понадобиться аккаунт владельца и
интернет; cached credentials в guest backup не отменяют expiry/revocation.

### Изолированная Проверка Восстановления

```bash
python3 -m proxmox_usb_backup.recovery guests --root /mnt/pve/usb-backups \
  --ids 108 --map-id 108=9901 --storage local-lvm --drill --confirm
```

Drill требует свободных новых IDs, запрещает apply-host, оставляет guest выключенным,
с onboot=0, без host passthrough/bind mounts, сетевые интерфейсы link_down=1.
Не запускать копию Vaultwarden в той же сети. После проверки удалить только
созданный тестовый guest. Это проверка архива, не испытание всего disaster recovery
на другом физическом сервере; такое испытание требует отдельного оборудования.

## Защита Носителя

Обычные vzdump/host archives **не зашифрованы**. Они содержат пароли, private keys,
Cloudflare/Tailscale state и пользовательские данные. Каталоги root-only, UI не
раздаёт архивы, но физический доступ к USB позволяет их прочитать на другом хосте.
Хранить SSD безопасно; шифрование с отдельным recovery key можно добавить как
отдельный этап, не теряя возможности восстановления.

Два раздела одного SSD не дают независимого backup для фототеки. Данная схема
защищает от поломки Proxmox/NVMe, но не от одновременной потери хоста и USB,
компрометации root или повреждения единственной последней копии.

Первоисточники: [Proxmox vzdump](https://github.com/proxmox/pve-docs/blob/master/vzdump.adoc),
[qmrestore](https://pve.proxmox.com/pve-docs/qmrestore.1.html),
[Tailscale Serve identity](https://tailscale.com/docs/features/tailscale-serve).
