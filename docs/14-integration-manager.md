# Управление интеграциями из админки

Вкладка `/integrations` показывает Cloudflare Tunnel, Tailscale на CT 100 и
отдельном шлюзе CT 107. Пользовательская инструкция: разделы 8–9
[единого руководства](../src/domain_router_ui/help/cloudflare.md).

## Установка с нуля

Сначала разверните админку, шлюз и реестр сервисов по предыдущим инструкциям.
Затем на Proxmox из корня исходников:

```sh
bash scripts/install-integration-manager.sh "$PWD"
```

Перед этим синхронизируйте новый checkout в CT 100 и обновите пакет UI по разделу
[обновления](08-rebuild-from-zero.md#обновление-существующей-установки). Если все
зависимости уже установлены, достаточно внутри CT 100:

```sh
/opt/domain-router-ui/venv/bin/pip install --no-deps /opt/domain-router-ui/source
systemctl restart domain-router-ui
```

Установщик агентов не меняет текущие подключения,
маршруты, firewall или Serve и не включает rollback timer без операции замены.
Установленные зависимости: cloudflared, tailscaled на CT 100/107, PyYAML в
venv CT 100. Версия cloudflared должна поддерживать `--token-file` (2025.4.0+).

Для первоначального доступа к ещё не подключённой админке используйте консоль
Proxmox/SSH. Веб-интерфейс не является способом обхода отсутствующего канала
управления.

## Граница привилегий

UI использует существующий forced-command SSH identity `service-inventory`,
не Proxmox root key. Агент принимает только фиксированные JSON-операции и цели
`panel` (CT 100) и `gateway` (CT 107). Данные передаются через stdin, не argv.
Агент внутри контейнера принадлежит root, роль задана root-only файлом
`/etc/domain-router-integrations/role`. Для gateway разрешены только status/connect
Tailscale. Произвольные команды, адреса upstream, VMID и пути из запроса запрещены.

Изменения требуют действующей сессии, CSRF, повторного ввода пароля и checkbox
подтверждения. Ошибки пароля ограничены по частоте. Audit хранит только действие,
имя целевого узла и итог, без submitted credentials, URL авторизации и команд.
Веб-страница не читает root-only секреты назад.

## Cloudflare И Откат

Локальные credentials и remote connector token строго валидируются. `cert.pem`,
API token и custom endpoints не принимаются. Secret хранится в root-only версии
под `/etc/cloudflared/router-managed/<random>/`, права файлов 0600, каталога 0700.
Systemd ExecStart содержит только пути, не значения токенов.

Предварительный connector работает ограниченное время на metrics `127.0.0.1:20246`;
его вывод отключён. При проверке учитываются `/ready` и принадлежность metrics
socket процессу systemd unit. Действующая служба заменяется drop-in
`/etc/systemd/system/cloudflared.service.d/90-domain-router.conf`, metrics 20241.
Основной connector пишет только warn/error. Root-only metadata и backup находятся
в `/var/lib/domain-router-integrations`. Все изменения под flock; revision
предотвращает применение устаревшей формы.

Перед переключением сохраняется pending snapshot и включается постоянный
systemd `domain-router-cloudflare-rollback.timer` с OnActiveSec=90. При успехе
pending удаляется и timer отключается. При сбое процесса/перезагрузке откат
восстанавливает предыдущие override и metadata. Секретные версии, необходимые
для возврата, сохраняются root-only. Резервируйте `/etc/cloudflared` целиком,
`/var/lib/domain-router-integrations` и systemd drop-in как секреты, вне Git.
DNS и remote config в Cloudflare не откатываются. `/ready` подтверждает только
соединение connector с edge; проверяйте сайт отдельно.

Ручное восстановление из root-консоли CT 100 при незавершённой операции:

```sh
/opt/domain-router-ui/venv/bin/python /opt/domain-router-integrations/agent.py --rollback
systemctl status cloudflared --no-pager
```

## Tailscale

Root-агент использует LocalAPI по Unix socket. Веб-процесс не получает права
оператора Tailscale или доступ к записи LocalAPI. Чтение отдаёт только безопасные
поля. Для подключения меняется WantRunning, интерактивный вход запускается
только при необходимости. Рабочие профили не заменяются и не выходят из tailnet.
Для первого профиля применяются фиксированная роль и маршруты шлюза, не произвольные
CIDR из браузера. Ссылки авторизации допускаются только на `https://login.tailscale.com/a/...`.
Объявления сетей не заменяют approval в консоли. ACL, key expiry, logout,
force-reauth и смена аккаунта исключены из доступных операций.

## Проверки

Локально: `PYTHONPATH=src python -m pytest`; исходная интеграция прошла 60 тестов,
актуальный полный набор дополнен проверками репозитория.

На CT 100 с root и venv, для существующего здорового локального Tunnel:

```sh
/opt/domain-router-ui/venv/bin/python /opt/domain-router-ui/source/scripts/verify-integration-manager.py --guard
```

Скрипт кратко перезапускает connector, применяет тот же UUID и возвращает
исходную конфигурацию. Опция `--guard` дополнительно проверяет реальное
срабатывание таймера через 90 секунд. DNS и аккаунт не меняются; секрет не
выводится. Не запускайте эту проверку для одновременно редактируемой конфигурации.

`scripts/verify-tailscale-login-isolated.py` запускает временный userspace daemon
в CT 107 и проверяет ссылку первого входа и фиксированный профиль шлюза, затем
полностью завершает его. Рабочий socket/daemon не используется, авторизация
нового устройства в аккаунте не выполняется и auth URL не сохраняется.
