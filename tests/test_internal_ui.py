import re
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from domain_router_services.agent import merge_snapshot
from domain_router_ui.app import create_app
from domain_router_ui.config import AppSettings
from domain_router_ui.database import Database
from domain_router_ui.service_client import ServiceClientError
from test_app import FakeRouterClient, login


class FakeServices:
    def __init__(self):
        self.state = merge_snapshot(dict(services=[], aliases={}, revision=0, updated_at=None),
            [dict(kind='lxc', vmid=104, identity='abc', name='media', status='running')],
            {'lxc:104': (['10.77.0.14', '192.168.0.156'], [('tcp', 8096, '*')])})
        self.calls = []
        self.fail = False

    def inventory(self):
        return self.state

    def toggle(self, identifier, enabled, expected_enabled):
        if self.fail:
            raise ServiceClientError('Gateway unavailable')
        item = self.state['services'][0]
        if identifier != item['id'] or item['enabled'] != expected_enabled:
            raise ServiceClientError('Conflict')
        self.calls.append((identifier, enabled, expected_enabled))
        item['enabled'] = enabled


@pytest.fixture
def ui(tmp_path):
    path = tmp_path / 'app.db'
    db = Database(path)
    db.initialize()
    db.create_user('admin', 'correct horse battery staple')
    services = FakeServices()
    app = create_app(AppSettings(database_path=path, router_command=('unused',)),
                     FakeRouterClient(), services)
    with TestClient(app, base_url='https://domain-router.test') as client:
        yield client, services, db


def form_data(client, services):
    html = client.get('/internal').text
    token = re.search(r'action="/internal/toggle".*?name="csrf_token" value="([^"]+)"', html, re.S).group(1)
    return dict(csrf_token=token, id=services.state['services'][0]['id'],
                enabled='false', expected_enabled='true')


def test_authentication_and_read_only_get(ui):
    client, services, _ = ui
    assert client.get('/internal', follow_redirects=False).status_code == 303
    assert client.post('/internal/toggle', data={}, follow_redirects=False).status_code == 303
    login(client)
    page = client.get('/internal')
    assert page.status_code == 200
    assert '10.77.0.14:8096' in page.text and 'role="switch"' in page.text
    assert page.text.count('data-guest-group=') == 1
    assert page.text.count('class="guest-address"') == 1
    assert '1 узел' in page.text and '1 сервис' in page.text
    assert not services.calls
    assert client.get('/static/internal.js').status_code == 200
    assert client.get('/static/copy.svg').status_code == 200


def test_csrf_and_input_validation(ui):
    client, services, _ = ui
    login(client)
    payload = form_data(client, services)
    assert client.post('/internal/toggle', data={**payload, 'csrf_token': 'bad'}).status_code == 400
    assert client.post('/internal/toggle', data={**payload, 'enabled': 'nonsense'}).status_code == 400
    assert not services.calls


def test_toggle_and_audit_and_stale_form(ui):
    client, services, db = ui
    login(client)
    payload = form_data(client, services)
    assert client.post('/internal/toggle', data=payload, follow_redirects=False).status_code == 303
    assert not services.state['services'][0]['enabled']
    assert client.post('/internal/toggle', data=payload).status_code == 409
    assert any(item['action'] == 'disable' and item['result'] == 'success' for item in db.audit_events())


def test_failure_visible_and_guest_name_escaped(ui):
    client, services, _ = ui
    login(client)
    payload = form_data(client, services)
    services.fail = True
    response = client.post('/internal/toggle', data=payload)
    assert response.status_code == 409 and 'Gateway unavailable' in response.text
    services.state['services'][0]['guest_name'] = '<script>alert(1)</script>'
    assert '<script>alert(1)</script>' not in client.get('/internal').text


def test_group_multiple_services_under_one_ip(ui):
    client, services, _ = ui
    login(client)
    item = deepcopy(services.state['services'][0])
    item.update(id='lxc:104:abc:445', port=445, name='SMB', enabled=False,
                url=None, lan_url=None)
    services.state['services'].append(item)
    page = client.get('/internal')
    assert page.text.count('data-guest-group=') == 1
    assert page.text.count('data-service-row') == 2
    assert '2 сервиса' in page.text
