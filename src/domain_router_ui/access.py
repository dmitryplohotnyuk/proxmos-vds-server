from __future__ import annotations

from urllib.parse import urlsplit
from uuid import UUID
from ipaddress import ip_address
import shlex


METHODS = {
    'password': 'Пароль',
    'ssh-key': 'SSH-ключ',
    'sso': 'SSO / Tailscale',
    'none': 'Без пароля',
}


def search_text(row: dict) -> str:
    target = row.get('target') or {}
    return ' '.join(str(value or '') for value in (
        row['title'], row['username'], row['method_label'], row.get('node_name'),
        row.get('node_address'), target.get('guest_name'), target.get('address'),
        target.get('name'), target.get('port'), target.get('lan_address'),
    )).casefold()


def ssh_command(row: dict) -> str:
    target = row.get('target') or {}
    # Manual records do not declare a protocol: never infer SSH from their title.
    if target.get('name') != 'SSH' or not row['username'] or row.get('missing'):
        return ''
    try:
        address = str(ip_address(target['address']))
        port = int(target['port'])
    except (ValueError, KeyError, TypeError):
        return ''
    if not 1 <= port <= 65535:
        return ''
    return shlex.join(['ssh', '-p', str(port), '-l', row['username'], '--', address])


def vault_origin(value: str) -> str:
    if not value:
        return ''
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('Vault URL must be an HTTPS origin without credentials or path')
    return value.rstrip('/')


def item_id(value: str) -> str:
    value = value.strip()
    if not value:
        return ''
    try:
        normalized = str(UUID(value))
    except ValueError:
        raise ValueError('ID записи должен быть UUID из Vaultwarden.') from None
    if normalized != value.lower():
        raise ValueError('Укажите UUID записи целиком, без URL и дополнительных параметров.')
    return normalized


def validate_link(form: dict[str, str], targets: dict[str, dict], existing: dict | None) -> dict[str, str]:
    allowed = {'csrf_token', 'target_id', 'title', 'username', 'method', 'vault_item_id', 'expected',
               'node_name', 'node_address'}
    if set(form) - allowed:
        raise ValueError('Форма принимает только метаданные. Пароли здесь не сохраняются.')
    target = form.get('target_id', '').strip()
    if target and target not in targets and (not existing or target != existing['target_id']):
        raise ValueError('Сервис больше не присутствует в инвентаризации. Обновите список.')
    title = form.get('title', '').strip()
    username = form.get('username', '').strip()
    method = form.get('method', '')
    if not title or len(title) > 120 or len(username) > 120:
        raise ValueError('Название обязательно; название и логин не должны превышать 120 символов.')
    if any(ord(c) < 32 for c in title + username):
        raise ValueError('Название и логин не должны содержать управляющие символы.')
    if method not in METHODS:
        raise ValueError('Выберите способ входа.')
    identifier = item_id(form.get('vault_item_id', ''))
    node_name = form.get('node_name', '').strip()
    node_address = form.get('node_address', '').strip()
    if len(node_name) > 120 or any(ord(c) < 32 for c in node_name):
        raise ValueError('Некорректное название узла.')
    if bool(node_name) != bool(node_address):
        raise ValueError('Для ручного узла укажите название и IP вместе.')
    if node_address:
        try:
            node_address = str(ip_address(node_address))
        except ValueError:
            raise ValueError('Укажите корректный IP ручного узла.') from None
    if target and (node_name or node_address):
        raise ValueError('Для обнаруженного сервиса узел определяется автоматически.')
    if method == 'none' and identifier:
        raise ValueError('Для входа без пароля привязка к хранилищу не нужна.')
    return dict(target_id=target, title=title, username=username, method=method, vault_item_id=identifier,
                node_name=node_name, node_address=node_address)


def access_rows(inventory: dict, links: list[dict], origin: str) -> list[dict]:
    targets = {item['id']: item for item in inventory.get('services', [])}
    rows = []
    for link in links:
        target = targets.get(link['target_id'])
        rows.append({**link, 'target': target, 'missing': bool(link['target_id'] and not target),
                     'method_label': METHODS.get(link['method'], link['method']),
                     'vault_link': (origin + '/#/vault?itemId=' + link['vault_item_id']
                                    if origin and link['vault_item_id'] else '')})
    linked = {link['target_id'] for link in links}
    for target in targets.values():
        if target['id'] not in linked:
            rows.append(dict(id='', target_id=target['id'], title=target['name'], username='',
                             method='', method_label='Не указан', vault_item_id='', vault_link='',
                             target=target, missing=False, updated_at=''))
    for row in rows:
        row['search_text'] = search_text(row)
        row['ssh_command'] = ssh_command(row)
    return sorted(rows, key=lambda row: (
        (row['target']['guest_name'] if row['target'] else 'Явные записи').casefold(),
        row['title'].casefold(), row['username'].casefold()))


def access_groups(rows: list[dict]) -> list[dict]:
    grouped = {}
    for row in rows:
        target = row['target']
        if target:
            key = ':'.join([target['guest_key'], target['guest_identity'], target['address']])
            group = grouped.setdefault(key, {**target, 'key': key, 'rows': []})
        elif row.get('node_name') and row.get('node_address') and not row['missing']:
            key = 'manual:' + row['node_name'].casefold() + ':' + row['node_address']
            group = grouped.setdefault(key, dict(key=key, rows=[], address=row['node_address'],
                lan_address='', guest_name=row['node_name'], kind='manual', vmid='', guest_status=''))
        else:
            key = 'missing:' + row['target_id'] if row['missing'] else 'manual'
            group = grouped.setdefault(key, dict(key=key, rows=[], address='', lan_address='',
                guest_name='Недоступный узел' if row['missing'] else 'Ручные записи',
                kind='', vmid='', guest_status='', missing=row['missing']))
        group['rows'].append(row)
    for group in grouped.values():
        group['account_count'] = sum(bool(row['id']) for row in group['rows'])
        group['linked_count'] = sum(bool(row['vault_link']) for row in group['rows'])
        group['rows'].sort(key=lambda row: (
            row['target']['port'] if row['target'] else 0, row['title'].casefold(), row['username'].casefold()))
    return sorted(grouped.values(), key=lambda group: (not bool(group['kind']), group['guest_name'].casefold(), group['key']))
