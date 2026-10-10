# Domain Router Deployment

Воспроизводимый комплект для развертывания `domain-router` с нуля на Proxmox:

- Debian 12 LXC с WAN по DHCP и постоянной сетью `10.77.0.0/24`;
- NAT для внутренних VM/LXC;
- Caddy и Cloudflare Tunnel для публичных HTTP/HTTPS-доменов;
- Tailscale для приватного доступа, SSH и админ-панели;
- CLI и веб-панель управления доменными маршрутами;
- автоматический реестр VM/LXC сервисов с группировкой по IP и VPN-переключателями;
- systemd-сервисы, watchdog, тесты и эксплуатационная документация.

Секреты, tunnel credentials и пароли в репозиторий не входят.

**Область восстановления:** базовый Bash-сценарий создаёт CT 100; полный порядок
для шлюза, AdGuard, реестра и подключений приведён в
[Полной текущей схеме](docs/08-rebuild-from-zero.md#полная-текущая-схема).
Home Assistant, Meshtastic, Jellyfin, TorrServer и их данные нужно восстановить
из собственных backups. Этот репозиторий настраивает их сетевой доступ, но не
содержит установщики всех приложений или резервные копии дисков.

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

Watchdog зависания встроенного Ethernet устанавливается отдельно на Proxmox:

```bash
sudo ./scripts/install-proxmox-nic-watchdog.sh
```

Он реагирует на `e1000e Hardware Unit Hang`, пробует восстановить NIC без
перезагрузки хоста и не содержит секретов.

Отдельный LXC с AdGuard Home и сетевым DNS-фильтром создаётся на Proxmox:

```bash
sudo ./scripts/create-adguard-lxc.sh
```

Сценарий создаёт LXC `106` с адресом `192.168.0.5`, проверяет официальный
архив, настраивает DNS и выводит сгенерированные пароли один раз. Настройка DHCP
роутера описана в [`docs/10-adguard-home.md`](docs/10-adguard-home.md).

## Разработка и тесты

Приватный доступ ко всем сервисам через отдельный шлюз и автоматическая
вкладка админки устанавливаются отдельным этапом:
[`docs/11-tailscale-gateway.md`](docs/11-tailscale-gateway.md),
[`docs/12-internal-services.md`](docs/12-internal-services.md).

Инструкция по подключению нового домена к Cloudflare и маршрутам доступна во
вкладке **Документация** в админке и в
[`docs/13-cloudflare-domain-guide.md`](docs/13-cloudflare-domain-guide.md).

Вкладка **Подключения** позволяет настроить connector Cloudflare Tunnel и
подключить Tailscale на админке и отдельном шлюзе. Установка агентов и восстановление:
[`docs/14-integration-manager.md`](docs/14-integration-manager.md).
Основной deploy-сценарий сам не создаёт дополнительный шлюз.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/python scripts/check-repository.py --history
```

CI в GitHub проверяет Python 3.11/3.13, тесты, ссылки документации, синтаксис
Bash/JavaScript и типовые секреты в исходниках/истории. Pattern scan не заменяет
ручную проверку staged diff. Результаты аудита и ограничения:
[`docs/15-repository-audit.md`](docs/15-repository-audit.md).

Текущая production-конфигурация зафиксирована в
[`docs/06-deployment-log.md`](docs/06-deployment-log.md). Пароли и credentials
в этот файл добавлять нельзя.
