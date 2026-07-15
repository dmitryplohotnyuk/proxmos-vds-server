# Admin Panel Implementation Plan

Статус: реализовано и развернуто 2026-07-15.

## Цель

Добавить в LXC `domain-router` приватную веб-панель для управления доменными
маршрутами без ручного входа по SSH. Панель дополняет CLI, но не заменяет его:
оба интерфейса используют один набор правил в `/etc/domain-router/routes.yml`.

Панель предназначена только для администрирования инфраструктуры. Она не должна
быть доступна через публичный Cloudflare Tunnel или wildcard DNS.

## Пользовательский сценарий

1. Администратор подключает компьютер к своему tailnet.
2. Открывает `https://domain-router.tail6ace7f.ts.net`.
3. Вводит логин и пароль панели.
4. Добавляет домен и внутренний upstream VM/LXC.
5. Панель валидирует данные, сохраняет маршрут и перезагружает Caddy.
6. Администратор видит результат операции и состояние сервисов.

Доступ требует двух независимых условий:

- устройство авторизовано в Tailscale;
- пользователь прошел авторизацию в панели.

## Объем первой версии

### Входит

- первоначальное создание администратора из консоли;
- вход и выход из панели;
- смена собственного пароля;
- просмотр списка маршрутов;
- добавление и изменение маршрута;
- включение, отключение и удаление маршрута;
- подтверждение перед удалением;
- проверка формата домена и upstream;
- проверка доступности upstream по запросу пользователя;
- состояние Caddy, cloudflared и Tailscale;
- журнал административных действий;
- сообщения об ошибках валидации и reload без потери старой конфигурации;
- автоматический запуск после перезагрузки LXC;
- адаптивный интерфейс для настольного браузера и телефона.

### Не входит

- управление VM/LXC через Proxmox API;
- терминал или выполнение произвольных команд;
- редактирование Cloudflare DNS и tunnel credentials;
- публикация панели через `content-factory-vps.win`;
- регистрация пользователей через веб-интерфейс;
- восстановление пароля по email;
- роли и многопользовательская модель;
- графики трафика и длительное хранение метрик.

## Технический стек

```text
Python 3.11
FastAPI
Uvicorn
Jinja2 templates
SQLite
Argon2id password hashing
systemd
Tailscale Serve
```

Интерфейс будет серверным HTML-приложением. Для первой версии не нужен отдельный
Node.js frontend или SPA: это уменьшает количество зависимостей, процесс сборки
и поверхность обновлений в инфраструктурном контейнере.

## Сетевая схема

```text
Administrator browser
  |
  | HTTPS inside tailnet
  v
Tailscale Serve
  |
  | http://127.0.0.1:8090
  v
domain-router-ui (unprivileged systemd service)
  |
  | restricted sudo commands
  v
domain-router CLI/core
  |
  +-- /etc/domain-router/routes.yml
  +-- /etc/caddy/Caddyfile
  +-- systemctl reload caddy
```

Uvicorn слушает только `127.0.0.1:8090`. Tailscale Serve завершает HTTPS и
публикует приложение только внутри tailnet. Tailscale Funnel не включается.
Порт `8090` не открывается на `eth0`, `lan0` или публичном Cloudflare Tunnel.

## Совместимость CLI и панели

Текущий `src/domain-router.py` совмещает доменную логику, работу с файлами и CLI.
Перед созданием панели его нужно разделить:

```text
domain_router/
  core.py       validation, load, render, transactional save
  services.py   Caddy and service status operations
  cli.py        argparse interface
domain_router_ui/
  app.py
  auth.py
  database.py
  templates/
  static/
```

`routes.yml` остается источником истины. Перенос маршрутов в SQLite не
планируется: конфигурация должна оставаться читаемой, переносимой и доступной
аварийному CLI.

Для защиты одновременных изменений CLI и панели используется exclusive lock,
например `/run/lock/domain-router.lock`. Полный цикл `read -> modify -> validate
-> write -> reload/rollback` выполняется под одной блокировкой.

## Привилегии

Веб-приложение не запускается от `root`. Создается системный пользователь
`domain-router-ui` без shell и домашнего каталога.

Операции изменения маршрутов выполняются через существующий CLI с ограниченным
`sudoers`-правилом. Разрешаются только конкретные команды `list`, `add`, `set`,
`enable`, `disable`, `remove`, `status` и `test`. Команды запускаются списком
аргументов без shell. CLI повторно валидирует все входные данные.

Файлы Cloudflare credentials, Tailscale state и общие root-команды панели
недоступны. Просмотр состояния сервисов выполняется read-only операциями.

## Модель данных

### Маршруты

Продолжают храниться в `/etc/domain-router/routes.yml`:

```yaml
version: 1
routes:
  - domain: app.content-factory-vps.win
    upstream: http://10.77.0.10:80
    enabled: true
```

### База панели

SQLite-файл `/var/lib/domain-router-ui/app.db` хранит только данные панели:

```text
users
  id
  username
  password_hash
  password_changed_at
  created_at
  disabled

sessions
  id
  user_id
  token_hash
  csrf_secret
  created_at
  expires_at
  last_seen_at

login_attempts
  id
  username_hash
  attempted_at

audit_events
  id
  user_id
  action
  object_type
  object_name
  result
  details
  created_at
```

Пароли, session tokens, CSRF tokens и Cloudflare credentials в audit log не
записываются.

## Валидация маршрутов

- домен нормализуется в lowercase и проверяется существующим правилом;
- в первой версии разрешены `content-factory-vps.win` и его поддомены;
- upstream использует только `http` или `https`;
- upstream должен быть IP-адресом из разрешенной сети `10.77.0.0/24`;
- порт должен находиться в диапазоне `1-65535`;
- path, query, fragment и userinfo запрещены;
- дублирующий домен запрещен;
- перед сохранением выполняется `caddy validate`;
- при ошибке reload восстанавливаются предыдущие `routes.yml` и Caddyfile;
- проверка upstream имеет короткий timeout и не выполняется автоматически при
  открытии списка маршрутов.

Allowlist сети задается конфигурацией, чтобы позже можно было явно добавить
другую внутреннюю подсеть без изменения кода. Это одновременно ограничивает
SSRF-возможности панели.

## Авторизация и защита сессий

- пароль хешируется Argon2id;
- минимальная длина пароля: 12 символов;
- session token генерируется криптографически стойким генератором;
- в cookie хранится token, в SQLite только его hash;
- cookie: `Secure`, `HttpOnly`, `SameSite=Strict`;
- срок сессии: 12 часов без возможности бессрочной сессии;
- все изменяющие POST-запросы защищены CSRF token;
- после смены пароля остальные сессии отзываются;
- ошибки входа не раскрывают существование пользователя;
- после пяти неудачных входов действует временная задержка;
- заголовки запрещают embedding, MIME sniffing и ненужный referrer;
- HTML-шаблоны экранируют пользовательские значения по умолчанию.

Первый администратор создается интерактивной консольной командой. Пароль не
передается аргументом процесса и не сохраняется в shell history.

## Страницы панели

```text
/login                 вход
/                      список маршрутов и состояние сервисов
/routes/new            создание маршрута
/routes/{domain}/edit  изменение upstream
/audit                  последние административные действия
/account                смена пароля и завершение других сессий
```

На главном экране показываются домен, upstream, состояние маршрута, результат
последней ручной проверки и компактные действия. Удаление выполняется только
после отдельного подтверждения.

## Конфигурация и файлы

```text
/opt/domain-router-ui/source/                  application source
/opt/domain-router-ui/venv/                    isolated dependencies
/var/lib/domain-router-ui/app.db               users, sessions, audit
/etc/systemd/system/domain-router-ui.service   application service
/etc/sudoers.d/domain-router-ui                restricted CLI access
/usr/local/bin/domain-router-ui-admin          bootstrap/reset CLI
```

Права каталогов и файлов проверяются при запуске. SQLite и резервные копии базы
доступны только пользователю сервиса и `root`.

## Этапы реализации

### Stage A - Подготовка общей логики

1. Вынести логику маршрутов из CLI в импортируемый Python package.
2. Добавить конфигурируемые пути для production и тестов.
3. Добавить allowlist upstream-сетей.
4. Добавить exclusive file lock для всех изменяющих операций.
5. Сохранить обратную совместимость всех CLI-команд.
6. Покрыть core unit-тестами.

### Stage B - Основа веб-приложения

1. Создать FastAPI-приложение и серверные шаблоны.
2. Добавить SQLite schema и миграции.
3. Реализовать bootstrap/reset администратора через консоль.
4. Реализовать login, logout, session expiry и CSRF.
5. Добавить security headers и rate limiting входа.
6. Покрыть auth и session flow тестами.

### Stage C - Управление маршрутами

1. Реализовать список и форму создания маршрута.
2. Реализовать изменение, enable/disable и удаление.
3. Добавить ручную проверку upstream и внешний URL маршрута.
4. Выводить ошибки CLI/Caddy без раскрытия внутренних секретов.
5. Добавить audit events для каждой изменяющей операции.
6. Проверить совместную работу CLI и панели.

### Stage D - Развертывание и приватный HTTPS

1. Создать системного пользователя и каталоги.
2. Установить приложение в отдельный virtualenv.
3. Установить ограниченный sudoers-файл и проверить его через `visudo -c`.
4. Установить и запустить `domain-router-ui.service` на loopback.
5. Настроить Tailscale Serve на `127.0.0.1:8090`.
6. Создать первого администратора интерактивно.
7. Проверить, что панель недоступна через WAN, LAN и Cloudflare Tunnel.

### Stage E - Приемочные тесты

1. Выполнить login/logout и проверить истечение сессии.
2. Добавить временный маршрут через панель и увидеть его через CLI.
3. Изменить маршрут через CLI и увидеть его в панели.
4. Проверить enable/disable/delete и rollback при ошибке Caddy.
5. Выполнить параллельные изменения и проверить блокировку.
6. Проверить внешний HTTPS-запрос через Cloudflare Tunnel.
7. Перезагрузить LXC и проверить все сервисы и Tailscale Serve.
8. Проверить резервное копирование и восстановление базы панели.

## Тестовая стратегия

- unit tests: normalization, allowlist, rendering, locking, rollback;
- application tests: auth, CSRF, sessions, CRUD, audit;
- integration tests: CLI subprocess, Caddy validation, SQLite migration;
- deployment tests: systemd, Tailscale-only exposure, reboot;
- browser tests: desktop и mobile viewport, отсутствие переполнений и полные
  пользовательские сценарии.

Production `routes.yml` не используется автоматическими тестами. Интеграционные
тесты работают во временных каталогах и с отдельным тестовым Caddyfile.

## Бэкапы и восстановление

В backup LXC должны входить:

```text
/var/lib/domain-router-ui/app.db
/opt/domain-router-ui/source/
/etc/systemd/system/domain-router-ui.service
/etc/sudoers.d/domain-router-ui
```

Маршруты по-прежнему восстанавливаются из `/etc/domain-router/routes.yml`.
После восстановления панели нужно отозвать старые сессии и проверить права на
SQLite, systemd-сервис и конфигурацию Tailscale Serve.

## Критерии готовности

- панель открывается по HTTPS только из авторизованного tailnet;
- без сессии доступны только login и статические ресурсы страницы входа;
- пароль отсутствует в конфигурации, логах и process arguments;
- все изменяющие запросы требуют валидную сессию и CSRF token;
- CRUD маршрутов работает и сразу отражается в CLI;
- CLI продолжает работать независимо от панели;
- одновременные изменения не повреждают `routes.yml`;
- недопустимый upstream не может обратиться за пределы allowlist;
- ошибка Caddy не повреждает действующую конфигурацию;
- после перезагрузки активны панель, Caddy, cloudflared и Tailscale;
- временные тестовые маршруты и учетные данные удалены после приемки;
- документация эксплуатации и восстановления обновлена.
