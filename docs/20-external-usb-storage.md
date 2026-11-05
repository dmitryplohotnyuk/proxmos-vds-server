# Внешний SSD: Бэкапы И Фото

## Развёрнуто 2026-11-05

Samsung SSD 990 PRO 2TB подключён через Realtek RTL9210B, USB 10 Gbit/s, UAS.
Полная ёмкость: 2 000 398 934 016 bytes. GPT содержит ровно два ext4-раздела:

| Назначение | Размер Раздела | UUID | Точка Монтирования |
| --- | --- | --- | --- |
| Бэкапы VM/LXC и конфигов | 399 999 238 144 bytes, около 400 GB / 372.5 GiB | `ca744010-53c5-4cde-846a-d66c60f4cf02` | `/mnt/pve/usb-backups` |
| Будущая фототека Immich | 1 600 398 557 184 bytes, около 1.60 TB / 1.46 TiB | `6acc0a42-5294-42ca-a54f-b3bdaf1ab69e` | `/mnt/immich-photos` |

GB/TB здесь десятичные, GiB/TiB двоичные. Часть каждого раздела занята ext4;
`df` показывает меньшую полезную ёмкость. Root-reserved blocks установлены в 0%:
это отдельные data-разделы, не системный filesystem.

На Proxmox зарегистрирован только `usb-backups`, тип directory, content `backup`.
Подкаталог `dump` предназначен для vzdump, `host-configs` - для будущих копий
конфигурации. Оба закрыты правами 0700. Фото-раздел пока не передан контейнеру.
Новых backup jobs, retention и расписаний этим этапом не создано; существующие
backups управляющей части не перенастраивались. Immich не установлен.

## Отсутствие И Отключение USB

В `/etc/fstab` добавлен только блок `domain-router optional USB storage`.
Обе записи используют UUID, а не нестабильное имя `/dev/sdb`:

```text
defaults,nofail,noatime,nodev,nosuid,errors=remount-ro,x-systemd.device-timeout=5s,x-systemd.mount-timeout=15s,x-systemd.device-bound=yes
```

- `nofail`: разделы не обязательны для `local-fs.target`, не блокируют обычную
  загрузку Proxmox и остальных гостей при отсутствии накопителя.
- Ожидание отсутствующего устройства ограничено 5 секундами, команды mount -
  15 секундами. `BindsTo` связывает mount unit с устройством.
- Нет automount: autofs не должен ошибочно считаться присутствующим диском при
  проверке storage. После подключения диска к уже работающему хосту mount units
  запускаются вручную; при загрузке с подключённым SSD они запускаются штатно.
- В directory storage установлены `is_mountpoint=1`, `create-base-path=0`,
  `create-subdirs=0`. При отсутствии mount Proxmox показывает storage inactive
  и не начинает писать бэкапы на системный filesystem.
- **Нижележащие пустые каталоги на системном диске имеют mode 000 и `chattr +i`.**
  Даже root не может создать там файл после unmount. Поверх них смонтированы
  обычные writable ext4-разделы с mode 0750; immutable не установлен на SSD.

При отсутствии SSD недоступны только бэкапы и данные фото. Само приложение Immich
потребует отдельного условия запуска/проверки UUID при его будущем развёртывании:
нельзя запускать его с пустым каталогом вместо фототеки. Root filesystem будущего
гостя следует оставить на внутреннем хранилище. Гостям не нужно делать общий
`Requires` на USB-хранилище, от которого зависит загрузка всего хоста.

Выдёргивание во время записи может повредить текущие файлы или filesystem.
`nofail` не делает такое отключение безопасным для данных и не гарантирует
отсутствие задержек ядра при неисправном USB-мосте. Перед штатным отключением:

```bash
# Сначала остановить будущий Immich и все операции записи/бэкапа на этот SSD.
systemctl stop "$(systemd-escape --path --suffix=mount /mnt/pve/usb-backups)" \
  "$(systemd-escape --path --suffix=mount /mnt/immich-photos)"
```

После подключения:

```bash
udevadm settle
systemctl reset-failed "$(systemd-escape --path --suffix=mount /mnt/pve/usb-backups)" \
  "$(systemd-escape --path --suffix=mount /mnt/immich-photos)"
systemctl start "$(systemd-escape --path --suffix=mount /mnt/pve/usb-backups)" \
  "$(systemd-escape --path --suffix=mount /mnt/immich-photos)"
findmnt --mountpoint /mnt/pve/usb-backups -o UUID,SOURCE,TARGET
findmnt --mountpoint /mnt/immich-photos -o UUID,SOURCE,TARGET
pvesm status --storage usb-backups
```

Если unmount сообщает busy, не использовать `umount -l` или принудительное
отключение вместо остановки приложения. Сначала найти и остановить писателей.

## Повторная Подготовка С Нуля

Скрипт [prepare-usb-photo-storage.py](../scripts/prepare-usb-photo-storage.py)
использует только Python stdlib и установленное ПО Proxmox. Сначала проверить:

```bash
lsblk -b -o NAME,SIZE,TYPE,TRAN,MODEL,FSTYPE,MOUNTPOINTS
udevadm info --query=property --name=/dev/disk/by-id/WHOLE_DISK_ID
wipefs --no-act /dev/disk/by-id/WHOLE_DISK_ID
```

**Следующая команда уничтожает содержимое подтверждённого USB-диска.** Не запускать
для уже созданных разделов с фотографиями или бэкапами. Нельзя переносить UUID
текущей установки на новый filesystem вручную.

```bash
python3 scripts/prepare-usb-photo-storage.py \
  --device /dev/disk/by-id/WHOLE_DISK_ID \
  --serial EXPECTED_SERIAL \
  --expected-size-bytes EXACT_CAPACITY_BYTES \
  --backup-gb 400 --initialize --confirm-erase-serial EXPECTED_SERIAL
```

Защиты: whole USB disk, совпадение serial и точной byte capacity, отсутствие
mount/holders/существующих разделов, явное подтверждение erase; неизвестные
data signatures блокируют инициализацию. Старый остаточный GPT без распознаваемой
таблицы разделов на текущем пустом SSD был удалён при подтверждённой разметке.

Без `--initialize` и `--confirm-erase-serial` скрипт **не форматирует**: проверяет
готовые два ext4-раздела, размер первого и labels, затем восстанавливает настройки
mount/storage. Для этого предварительно штатно размонтировать оба раздела.
Повторный запуск с `--initialize` на диске с разделами отклоняется.

Перед изменением конфигурации сохраняются `/etc/fstab` и `/etc/pve/storage.cfg`
в закрытый `/root/usb-storage-before-<UTC timestamp>/`.
Текущая копия: `/root/usb-storage-before-20261105T085030Z/`.
Это backup настроек, не backup содержимого внешнего SSD.

## Проверено

- GPT, два ext4, UUID, ёмкость, права и активный `usb-backups`.
- `findmnt --verify`: без ошибок/предупреждений; USB mounts не входят в `Requires`
  у `local-fs.target`, связь с device units подтверждена.
- Штатный unmount обоих пустых разделов; root touch в оба каталога запрещён;
  Proxmox storage становится inactive, системный диск не используется как fallback.
- После unmount временно отключён **только** USB device `2-4` через driver unbind.
  Оба UUID исчезли; mount attempt завершился ожидаемой ошибкой через 5 секунд.
  `multi-user.target` остался active, статусы всех 9 существующих гостей неизменны.
- Адаптер снова привязан, оба раздела смонтированы, storage active. Это безопасная
  имитация отсутствия USB, не тест аварийного выдёргивания во время записи.
- Хост и гости не перезагружались. Полная загрузка с физически отсутствующим SSD
  не проводилась; отсутствие критической boot-зависимости проверено в systemd.
- Повторный configure-only запуск прошёл без mkfs/переразметки, UUID сохранены.
  Directory storage обновляется без фиксированного поля path; `lsblk --tree`
  обязателен, чтобы проверка видела существующие и смонтированные разделы.
- Пройдены 118 Python-тестов (9 для этого скрипта), проверки документации и
  шаблонов секретов, `git diff --check`. Domain Router, Jellyfin, TorrServer и
  Home Assistant отвечают HTTP 200. Критические службы Proxmox и сборщик HA active.

Тесты скрипта: `env PYTHONPATH=src ../.venv/bin/python -m pytest tests/test_usb_storage.py`.

## Откат Настроек

Остановить потребителей USB и размонтировать оба раздела. Удалить только
определение `usb-backups` командой `pvesm remove usb-backups`, затем только
управляемый блок из `/etc/fstab` и выполнить `systemctl daemon-reload`.
Это не удаляет разделы и данные на SSD. Не заменять всю storage.cfg старой
копией, если после её создания появились другие настройки.

При необходимости удалить пустые нижележащие каталоги сначала снять с них
`chattr -i` **при размонтированном SSD**, восстановить права и проверить пустоту.
Не снимать immutable со смонтированной фототеки вместо каталога на root.
Не запускать initialization для отката или восстановления монтирования.

## Ограничения Бэкапов

Фото на этом SSD и их копия на соседнем разделе имеют одну точку аппаратного
отказа. Для резервирования фототеки нужен другой физический носитель/удалённое
хранилище. Обычный vzdump LXC не сохраняет содержимое bind mounts; при установке
Immich нужно отдельно защитить и файлы, и согласованную БД. Расписание и retention
VM/LXC/host-configs остаются следующим этапом обсуждения.

Первоисточники: [systemd.mount](https://www.freedesktop.org/software/systemd/man/257/systemd.mount.html),
[Proxmox directory storage](https://pve.proxmox.com/pve-docs/pvesm.1.html),
[Proxmox backup limitations](https://github.com/proxmox/pve-docs/blob/master/vzdump.adoc).
