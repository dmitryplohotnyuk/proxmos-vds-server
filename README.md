# Domain Router Deployment

Воспроизводимый комплект для развертывания `domain-router` с нуля на Proxmox:

- Debian 12 LXC с WAN по DHCP и постоянной сетью `10.77.0.0/24`;
- NAT для внутренних VM/LXC;
- Caddy и Cloudflare Tunnel для публичных HTTP/HTTPS-доменов;
- Tailscale для приватного доступа, SSH и админ-панели;
- CLI и веб-панель управления доменными маршрутами;
- systemd-сервисы, watchdog, тесты и эксплуатационная документация.

Секреты, tunnel credentials и пароли в репозиторий не входят.

## Три способа развертывания

### 1. Автоматический Bash-сценарий

На чистом Proxmox склонировать репозиторий, проверить `.env.example` и выполнить:

```bash
cp .env.example .env
sudo ./scripts/deploy-from-proxmox.sh
sudo pct enter 100
/opt/domain-router-ui/source/scripts/configure-integrations.sh
/opt/domain-router-ui/source/scripts/verify-deployment.sh
```

Первый скрипт создает bridge и LXC, затем устанавливает локальные сервисы.
Второй проводит интерактивную авторизацию Cloudflare и Tailscale и создает
администратора. Между этапами можно безопасно остановиться.

### 2. Пошагово по инструкции

Полная ручная процедура с контрольными командами описана в
[`docs/08-rebuild-from-zero.md`](docs/08-rebuild-from-zero.md).

### 3. Через Codex

Открыть этот репозиторий в Codex и передать задачу из [`CODEX.md`](CODEX.md).
Codex должен выполнять этапы последовательно, запрашивая участие только для
Cloudflare/Tailscale авторизации и ввода нового пароля администратора.

## Структура

```text
config/    шаблоны Caddy, nftables, systemd, sudoers и сети
docs/      архитектура, эксплуатация, журнал и восстановление
scripts/   создание LXC, установка, интеграции и приемочная проверка
src/       CLI и FastAPI-панель
tests/     тесты маршрутизации и панели
```

Первая application VM воспроизводится отдельным сценарием на Proxmox:

```bash
sudo ./scripts/create-content-factory-vm.sh
```

Сценарий создает Ubuntu Server 24.04 VM `content-factory`, генерирует новый
root-пароль, выводит его один раз и не сохраняет пароль в Git.

Прямой аварийный доступ к самому Proxmox через Tailscale настраивается на
Proxmox-хосте:

```bash
sudo ./scripts/install-proxmox-tailscale.sh
```

Сценарий выполняет интерактивную авторизацию устройства, отключает прием subnet
routes на хосте, сбрасывает Funnel и публикует `pveproxy` только внутри tailnet.

## Разработка и тесты

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Текущая production-конфигурация зафиксирована в
[`docs/06-deployment-log.md`](docs/06-deployment-log.md). Пароли и credentials
в этот файл добавлять нельзя.
