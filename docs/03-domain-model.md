# Domain Model

## Конфигурация маршрутов

Первая версия хранит маршруты в одном файле:

```text
/etc/domain-router/routes.yml
```

Пример:

```yaml
routes:
  - domain: app.example.com
    upstream: http://10.77.0.10:80
    enabled: true

  - domain: api.example.com
    upstream: http://10.77.0.12:8080
    enabled: true

  - domain: old.example.com
    upstream: http://10.77.0.13:80
    enabled: false
```

## Генерация Caddyfile

На основе `routes.yml` скрипт генерирует:

```text
/etc/caddy/Caddyfile
```

Пример результата:

```caddyfile
{
    auto_https off
}

:8080 {
    bind 127.0.0.1

    @route_0 host app.example.com
    handle @route_0 {
        reverse_proxy http://10.77.0.10:80
    }

    @route_1 host api.example.com
    handle @route_1 {
        reverse_proxy http://10.77.0.12:8080
    }

    handle {
        respond "Unknown domain" 404
    }
}
```

`auto_https off` нужен в модели, где TLS завершается на Cloudflare, а внутрь туннеля идет HTTP до Caddy. Если позже будет выбран VPS/WireGuard или прямой публичный IP, TLS-модель можно изменить.

## Cloudflare Tunnel ingress

`cloudflared` должен передавать все HTTP-запросы в Caddy.

Пример:

```yaml
tunnel: <tunnel-id>
credentials-file: /etc/cloudflared/<tunnel-id>.json

ingress:
  - hostname: "*.example.com"
    service: http://127.0.0.1:8080
  - hostname: "example.com"
    service: http://127.0.0.1:8080
  - service: http_status:404
```

Если wildcard нежелателен, можно перечислять конкретные домены. Для первой версии удобнее поддержать оба режима.

## CLI

Планируемый интерфейс:

```bash
domain-router list
domain-router add app.example.com http://10.77.0.10:80
domain-router set app.example.com http://10.77.0.11:80
domain-router disable app.example.com
domain-router enable app.example.com
domain-router remove app.example.com
domain-router render
domain-router reload
domain-router test app.example.com
domain-router status
```

Команда `add` должна:

1. Проверить формат домена.
2. Проверить формат target URL.
3. Добавить запись в `routes.yml`.
4. Сгенерировать Caddyfile.
5. Выполнить `caddy validate`.
6. Выполнить `systemctl reload caddy`.

## Что пользователь делает в DNS

Пользователь вручную настраивает DNS.

Для Cloudflare Tunnel:

```text
app.example.com  CNAME  <tunnel-id>.cfargotunnel.com
api.example.com  CNAME  <tunnel-id>.cfargotunnel.com
```

Для wildcard:

```text
*.example.com CNAME <tunnel-id>.cfargotunnel.com
```

После этого локальный `domain-router` решает, какой внутренний сервис получит запрос.
