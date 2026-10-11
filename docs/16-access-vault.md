# Хранилище Доступов

## Границы

Vaultwarden находится в отдельном unprivileged Debian 12 LXC 108: 1 CPU,
1024 MiB RAM, 256 MiB swap, 8 GiB thin disk. LAN DHCP через vmbr0;
private IP `10.77.0.4/24` через vmbr1. Root-пароль не задаётся, SSH отключён,
управление через Proxmox `pct`. Необходим запас физических RAM/disk;
сумма виртуальных thin-дисков не является физически свободным местом.

Внутри LXC официальный образ `ghcr.io/dani-garcia/vaultwarden:1.37.4`
загружается один раз и закрепляется по digest в `/opt/vaultwarden/image.ref`.
Docker использует host network внутри LXC; Rocket слушает только `127.0.0.1:8080`,
без Docker published-port/NAT. Все Linux capabilities сброшены, включён
no-new-privileges, лимиты памяти/CPU/PID. Это не отдельная VM-граница безопасности.

Tailscale работает в userspace mode, без /dev/net/tun, subnet routes и DNS takeover.
Serve даёт приватный HTTPS; Funnel не включается. Имя получается из реального
Tailscale DNSName, а не предполагается заранее. Публичного Cloudflare-маршрута нет.
ACL tailnet определяют доступ к узлу; размещение в tailnet само по себе не
ограничивает доступ одному владельцу. Для строгой изоляции задайте ACL владельца
в Tailscale. MFA хранилища настраивает владелец после регистрации.

Domain Router хранит в SQLite только название, логин, способ входа, service identity,
UUID записи и время изменения привязки. Секреты и мастер-пароль туда не попадают.
Это реестр ссылок, не проверка действительности паролей и не SSO. Привязки защищены
авторизацией, CSRF, HTML escaping и optimistic concurrency; операции аудируются
без содержимого записей. Нет API смены паролей и дополнительных root-прав UI.

Вкладка группирует сервисы по identity гостя и IP, отдельно от ручных узлов.
Для Proxmox, шлюза или внешнего устройства можно задать пару `node_name` /
`node_address` в привязке без обнаруженного сервиса. Поля принимают только
название и IP, не URL/команды; сетевые запросы по ним не выполняются.
Пересозданные гости с другой identity не объединяются со старыми привязками.
Поиск раскрывает найденные группы; обычное раскрытие сохраняется только в
sessionStorage браузера. Секретов в этом состоянии нет.

## SSH По Явному Запросу Владельца

Ключевой вход остаётся рекомендуемым. 2026-10-11 владелец отдельно выбрал root SSH
с уникальными паролями вместо ранее заблокированных root-аккаунтов. Это повышает
последствия компрометации пароля; публичный SSH через Cloudflare не настраивался.
Изменены только CT 100/104/105/106/107 и VM 101/103. Proxmox сохранён без смены
его известного пароля. HA OS 102 и Vaultwarden 108 не переделывались для SSH.

Для повторения только с отдельным разрешением владельца: подготовьте уникальный
пароль, сначала сохраните его в Vaultwarden, затем на Proxmox передайте точное
значение через stdin в `python3 scripts/configure-root-ssh.py --kind lxc --id ID
--allow-root-password`. Не используйте пароль в shell argv/history, `.env` или Git.
Для VM выберите `--kind qemu`; нужен действующий guest agent. Скрипт не устанавливает
SSH и не запускает выключенную службу. Он сохраняет исходный sshd_config в
`/root/ssh-access-before-password-TIMESTAMP/`, добавляет Match только для root,
проверяет конфигурацию, применяет пароль через chpasswd и делает reload, не reboot.
Существующие authorized_keys и настройки остальных пользователей не меняются.
Повторное добавление managed block запрещено; это не универсальный rotation API.

После применения проверьте новый вход с клиента без SSH multiplexing и обновите
статус записи; сохранение в vault само по себе не означает успешное применение.
На Domain Router Tailscale SSH остаётся с проверкой личности: вход на его Tailscale
IP/имя может требовать подтверждения в браузере, а не новый пароль.
Обычный парольный SSH проверен по LAN `192.168.0.173`; это отражено в записи.
Восстановление старого sshd_config возвращает только SSH-политику, не старый пароль.

## Развёртывание С Нуля

На Proxmox из Git checkout:

```sh
bash scripts/create-vaultwarden-lxc.sh
pct exec 108 -- tailscale up --hostname=vaultwarden --accept-routes=false --accept-dns=false --timeout=30s
# Владелец подтверждает выданную ссылку в браузере.
pct exec 108 -- bash /opt/vaultwarden/source/scripts/configure-vaultwarden-https.sh
```

Если исходники доставлены архивом без `.git`, передайте проверенный source archive
через `VAULT_SOURCE_ARCHIVE=/путь/source.tar.gz`. Создание отказывается изменять
существующий guest. При частичной установке проверьте `pct config 108` и продолжайте
установщик внутри именно созданного гостя; не удаляйте и не пересоздавайте данные.
`install-vaultwarden.sh` отказывается перезаписывать существующий environment.
При изменении CT ID передайте `VAULT_CT_ID` и обновите команды; private адрес должен
быть свободен. Настройки реестра/агентов не меняются этим установщиком.

После авторизации добавьте drop-in UI (не перезаписывая существующие drop-ins):

```ini
[Service]
Environment=DOMAIN_ROUTER_VAULT_URL=https://ФАКТИЧЕСКИЙ-DNSNAME.ts.net
```

Путь: `/etc/systemd/system/domain-router-ui.service.d/vault.conf` внутри CT 100.
Обновите Python package обычной процедурой из документа 08, затем
`systemctl daemon-reload && systemctl restart domain-router-ui`.
Вкладка `/access` читает уже существующий inventory; backup app.db включает привязки.

## Первичная Регистрация

```sh
pct exec 108 -- /usr/local/sbin/vaultwarden-registration open
pct exec 108 -- /usr/local/sbin/vaultwarden-registration status
# Немедленно закрыть при необходимости:
pct exec 108 -- /usr/local/sbin/vaultwarden-registration close
```

Окно длится не более 15 минут плюс интервал проверки (15 секунд); оно закрывается
после появления первого аккаунта. Одновременно два клиента теоретически могут
зарегистрироваться до проверки: это не атомарный single-user gate. Используйте
доверенный tailnet / ACL владельца и проверьте количество аккаунтов.
После появления аккаунта повторное open запрещено. При перезагрузке таймер закрывает
просроченное окно. Web admin panel выключен (ADMIN_TOKEN не установлен).
SMTP и email verification не настроены; email служит идентификатором, не механизмом
восстановления. Пользователь сам создаёт мастер-пароль и включает MFA, recovery code
хранит отдельно. Приватные ключи и пароль хранилища не передавайте в Git/чат.

Инструкция для пользователя также доступна в UI `/docs/access`.

## Сохранение Через CLI

На доверенном устройстве установите официальный `bw`. Не запускайте постоянный
`bw serve` и не храните мастер-пароль / BW_SESSION в Domain Router.

```sh
bw config server https://ФАКТИЧЕСКИЙ-DNSNAME.ts.net
bw login
export BW_SESSION="$(bw unlock --raw)"
python3 scripts/vault-save.py --server https://ФАКТИЧЕСКИЙ-DNSNAME.ts.net \
  --name 'Jellyfin / admin' --username admin --uri http://10.77.0.14:8096
# Скрытый ввод существующего пароля. Вывод: только метаданные и UUID.
bw lock
unset BW_SESSION
```

Для автоматизации подайте точный пароль через stdin без завершающего перевода строки;
не добавляйте пароль в командную строку. CLI проверяет origin/статус разблокировки.
Сбой не пересылает stdout/stderr Bitwarden, где могут быть секреты. При timeout
результат сохранения может быть неопределённым: проверьте vault до повтора.
Запись в хранилище не меняет сервис. Полученный UUID привяжите во вкладке Доступы.

У `create-content-factory-vm.sh` есть опциональный hook
`CONTENT_FACTORY_SAVE_TO_VAULT=1` + `VAULTWARDEN_URL` + временная `BW_SESSION`.
Сохранение выполняется до любых изменений VM, при неудаче provisioning прекращается.
В этом режиме root-пароль не печатается. По умолчанию прежний сценарий без vault
сохранён. Не храните unlock-сессию постоянно на Proxmox. Остальные установщики
требуют ручного сохранения через CLI/клиент; автоматическое извлечение старых хешей
и смена паролей исключены. Автоматическая привязка metadata пока не реализована.

## Шифрованные Резервные Копии

На отдельном доверенном компьютере установите age и создайте identity:

```sh
umask 077
age-keygen -o age-identity.txt
age-keygen -y age-identity.txt > backup-recipient.txt
```

На Proxmox передаётся **только** `backup-recipient.txt`:

```sh
bash scripts/install-vaultwarden-backups.sh /путь/backup-recipient.txt
systemctl status vaultwarden-host-backup.timer
systemctl start vaultwarden-host-backup.service
journalctl -u vaultwarden-host-backup.service --no-pager -n 20
```

Копирование в 04:20 по локальному времени хоста + до 15 минут случайной задержки,
пропущенный запуск выполняется после включения. CT 108 кратко останавливает только
Vaultwarden, архивирует согласованные `data`, `vaultwarden.env`, `image.ref` напрямую
в age, без plaintext tar на диске, и запускает сервис. В CT retention 14 дней,
на Proxmox `/var/lib/vz/dump/vaultwarden` 30 дней. На сервере нет decryption identity.
Копии содержат служебные ключи приложения и требуют защиты даже при E2EE.

На другом устройстве:

```sh
bash scripts/pull-vaultwarden-backups.sh root@PROXMOX_IP /защищённый/каталог
python3 scripts/verify-vaultwarden-backup.py /путь/backup.tar.age \
  --identity /путь/age-identity.txt --age /путь/age
```

Перенос off-host этим скриптом ручной. Ежедневная копия на том же Proxmox не защищает
от потери физического диска. Автоматический удалённый target требует отдельного
устройства/доступа и пока не настроен. Identity держите отдельно от backup и
дублируйте на защищённом внешнем носителе; утрата identity делает age-копии бесполезными.

## Восстановление И Обновление

1. Проверьте расшифровку архивов через verify-скрипт, сохранив оригинал.
2. Создайте новый пустой LXC, установите Docker/age/Tailscale. Не запускайте второй
   активный клон старого узла Tailscale; state Tailscale в этом архиве намеренно нет.
3. Остановите Vaultwarden. На доверенном устройстве расшифруйте архив и безопасно
   восстановите три каталога/файла в `/opt/vaultwarden`, сохранив права. Запускайте
   с восстановленным закреплённым image.ref и signup=false; не устанавливайте
   новый пустой environment поверх восстановленного.
4. Авторизуйте новый Tailscale-узел, задайте фактический DOMAIN, включите Serve.
   Если DNSName изменился, обновите адрес UI/клиентов и WebAuthn-настройки.
5. Проверьте `/alive`, вход владельца, MFA, записи и вложения, затем сделайте новую
   backup-копию. На этапе пустого vault проверить расшифровку реальных записей нельзя.

Для обновления сначала backup и тест восстановления. Загрузите явно выбранный
официальный tag, сохраните его RepoDigest, обновите image.ref и перезапустите
Vaultwarden. Обновления автоматически не применяются. Откат версии после миграции
БД требует восстановления согласованной старой копии, а не только старого image.
Следите за release notes и security advisories Vaultwarden и совместимостью клиентов.

## Проверки

```sh
pct exec 108 -- systemctl is-active vaultwarden tailscaled
pct exec 108 -- curl -fsS http://127.0.0.1:8080/alive
pct exec 108 -- tailscale serve status
pct exec 108 -- /usr/local/sbin/vaultwarden-registration status
pct exec 108 -- ss -lntp
curl --connect-timeout 3 http://10.77.0.4:8080/alive  # Должно отказать
```

Проверьте HTTPS из tailnet без `-k`, отсутствие Funnel и LAN HTTP listener,
работоспособность прежних маршрутов/сервисов и UI после обновления. Проверяйте
лог backup service на ошибки; наличие timer само по себе не гарантирует свежую копию.

Для Codex: выполнить этапы этого документа последовательно. Не собирать секреты
из существующих сервисов, не менять их пароли, не устанавливать MASTER_PASSWORD
в router environment. Авторизация Tailscale, мастер-пароль и MFA требуют владельца.

Источники: [Vaultwarden](https://github.com/dani-garcia/vaultwarden),
[Backup](https://github.com/dani-garcia/vaultwarden/wiki/Backing-up-your-vault),
[Bitwarden CLI](https://bitwarden.com/help/cli/),
[Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve).
