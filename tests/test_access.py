import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from domain_router_ui.access import access_groups, access_rows, ssh_command, vault_origin
from domain_router_ui.app import create_app
from domain_router_ui.config import AppSettings
from domain_router_ui.database import Database
from test_app import FakeRouterClient, login
from test_internal_ui import FakeServices

ITEM = 'a4d32612-262b-49c7-9701-a85fd25be230'


@pytest.fixture
def access_ui(tmp_path):
    db = Database(tmp_path / 'app.db')
    db.initialize()
    db.create_user('admin', 'correct horse battery staple')
    services = FakeServices()
    app = create_app(AppSettings(database_path=db.path, vault_url='https://vault.example.test'),
                     FakeRouterClient(), services)
    with TestClient(app, base_url='https://domain-router.test') as client:
        yield client, db, services


def payload(client, path='/access/new', **kwargs):
    page = client.get(path).text
    token = re.search(r'<form class="form-stack.*?name="csrf_token" value="([^"]+)"', page, re.S).group(1)
    return dict(csrf_token=token, title='Jellyfin / admin', username='admin', method='password',
                target_id='', vault_item_id=ITEM, **kwargs)


def test_auth_and_discovery(access_ui):
    client, db, services = access_ui
    for url in ['/access', '/access/new', '/access/edit/test']:
        assert client.get(url, follow_redirects=False).status_code == 303
    assert client.post('/access/new', data={}).history[0].status_code == 303
    login(client)
    page = client.get('/access')
    assert page.status_code == 200 and 'media' in page.text and '8096' in page.text
    assert 'name="password"' not in client.get('/access/new').text
    assert db.access_links() == [] and not services.calls
    assert client.get('/static/key-round.svg').status_code == 200
    assert page.text.count('data-access-group=') == 1
    assert page.text.count('class="guest-address"') == 1
    assert client.get('/static/access.js').status_code == 200


def test_grouping_separates_guest_identity_and_keeps_accounts(access_ui):
    _, _, services = access_ui
    from copy import deepcopy
    inventory = deepcopy(services.state)
    first = inventory['services'][0]
    second = {**first, 'id': 'other-port', 'port': 22, 'name': 'SSH'}
    reused = {**first, 'id': 'new-guest', 'guest_identity': 'new-identity'}
    inventory['services'] += [second, reused]
    links = [dict(id=str(n), target_id=first['id'], title='Account', username=str(n),
                  method='password', vault_item_id=ITEM, updated_at='') for n in range(2)]
    groups = access_groups(access_rows(inventory, links, 'https://vault.test'))
    assert len(groups) == 2
    original = next(group for group in groups if group['guest_identity'] == first['guest_identity'])
    assert len(original['rows']) == 3
    assert original['account_count'] == original['linked_count'] == 2


def test_search_expands_group_and_retains_header(access_ui):
    client, _, _ = access_ui
    login(client)
    html = client.get('/access?q=8096&scope=all').text
    assert re.search(r'<details[^>]*data-access-group=[^>]*\bopen\s*>', html)
    assert '10.77.0.14' in html
    assert 'Ничего не найдено' in client.get('/access?q=no-such-account').text


def test_scopes_keep_inventory_but_hide_unconfigured_ports(access_ui):
    client, db, services = access_ui
    login(client)
    assert 'Найдено: 0' in client.get('/access').text
    assert 'Найдено: 1' in client.get('/access?scope=all').text
    form = payload(client)
    form['target_id'] = services.state['services'][0]['id']
    assert client.post('/access/new', data=form, follow_redirects=False).status_code == 303
    assert 'Найдено: 1' in client.get('/access').text
    assert 'Найдено: 0' in client.get('/access?scope=unlinked').text
    assert 'Найдено: 0' in client.get('/access?scope=ssh').text
    assert 'Найдено: 1' in client.get('/access?scope=invalid').text
    assert 'Найдено: 0' in client.get('/access?q=not-found').text
    assert len(db.access_links()) == 1 and not services.calls


def test_ssh_copy_is_shell_safe_and_never_inferred_from_manual_title():
    import shlex
    row = dict(username='root; touch /tmp/unsafe', missing=False,
               target=dict(name='SSH', port=2222, address='10.77.0.14'))
    assert shlex.split(ssh_command(row)) == [
        'ssh', '-p', '2222', '-l', row['username'], '--', '10.77.0.14']
    assert not ssh_command({**row, 'target': None, 'title': 'SSH root'})
    assert not ssh_command({**row, 'target': {**row['target'], 'address': '-oProxyCommand=bad'}})
    assert not ssh_command({**row, 'missing': True})
    assert not ssh_command({**row, 'username': ''})


def test_manual_node_metadata_and_validation(access_ui):
    client, db, _ = access_ui
    login(client)
    form = {**payload(client), 'node_name': 'Proxmox', 'node_address': '100.93.12.80'}
    for bad in [dict(node_address='not-an-ip'), dict(node_name=''),
                dict(node_name='a\nb'), dict(target_id='unknown')]:
        assert client.post('/access/new', data={**form, **bad}).status_code == 400
    assert client.post('/access/new', data=form, follow_redirects=False).status_code == 303
    db.initialize()
    assert db.access_links()[0]['node_address'] == '100.93.12.80'
    html = client.get('/access?q=100.93.12.80').text
    assert 'Proxmox' in html and 'Ручной узел' in html and 'data-access-group=' in html


def test_links_crud_multiple_accounts_and_conflict(access_ui):
    client, db, services = access_ui
    login(client)
    form = payload(client)
    form['target_id'] = services.state['services'][0]['id']
    assert client.post('/access/new', data=form, follow_redirects=False).status_code == 303
    link = db.access_links()[0]
    page = client.get('/access').text
    assert 'https://vault.example.test/#/vault?itemId=' + ITEM in page
    path = '/access/edit/' + link['id']
    edit = payload(client, path, expected=link['updated_at'])
    edit['target_id'] = form['target_id']
    edit['username'] = 'other'
    assert client.post(path, data=edit, follow_redirects=False).status_code == 303
    assert client.post(path, data=edit).status_code == 409
    form = payload(client)
    form['target_id'] = edit['target_id']
    assert client.post('/access/new', data=form, follow_redirects=False).status_code == 303
    assert len(db.access_links()) == 2
    html = client.get(path).text
    remove_token = re.search(r'action="/access/remove/.*?name="csrf_token" value="([^"]+)"', html, re.S).group(1)
    remove = dict(csrf_token=remove_token, expected=db.access_link(link['id'])['updated_at'])
    assert client.post('/access/remove/' + link['id'], data=remove).status_code == 400
    assert client.post('/access/remove/' + link['id'], data={**remove, 'confirm': 'yes'}, follow_redirects=False).status_code == 303
    assert len(db.access_links()) == 1 and not services.calls
    assert {event['action'] for event in db.audit_events()} >= {'link', 'unlink'}


def test_input_secrets_csrf_and_xss(access_ui):
    client, db, _ = access_ui
    login(client)
    form = payload(client)
    for invalid in [dict(csrf_token='bad'), dict(password='MUST-NOT-LEAK'),
                    dict(vault_item_id='javascript:alert(1)'), dict(target_id='unknown'),
                    dict(method='exec'), dict(method='none'), dict(title='')]:
        response = client.post('/access/new', data={**form, **invalid})
        assert response.status_code == 400
        assert 'MUST-NOT-LEAK' not in response.text
    assert db.access_links() == []
    form['title'] = '<script>alert(1)</script>'
    assert client.post('/access/new', data=form, follow_redirects=False).status_code == 303
    assert '<script>alert(1)</script>' not in client.get('/access').text


def test_no_password_and_missing_service(access_ui):
    client, db, services = access_ui
    login(client)
    form = payload(client)
    form.update(method='none', vault_item_id='', username='', target_id=services.state['services'][0]['id'])
    assert client.post('/access/new', data=form, follow_redirects=False).status_code == 303
    services.state['services'] = []
    assert 'Сервис отсутствует' in client.get('/access').text
    link = db.access_links()[0]
    path = '/access/edit/' + link['id']
    changed = payload(client, path, expected=link['updated_at'])
    changed.update(method='none', vault_item_id='', target_id=form['target_id'])
    assert client.post(path, data=changed, follow_redirects=False).status_code == 303


@pytest.mark.parametrize('value', ['http://vault.test', 'https://user:pass@vault.test', 'https://vault.test/?x=1',
                                  'https://vault.test/path', 'javascript:alert(1)'])
def test_vault_origin_rejects_unsafe_urls(value):
    with pytest.raises(ValueError):
        vault_origin(value)
