import base64
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from domain_router_services import integrations as manager
from domain_router_services.agent import integration_request
from domain_router_ui.app import create_app
from domain_router_ui.config import AppSettings
from domain_router_ui.database import Database
from domain_router_ui.service_client import ServiceClientError
from test_app import FakeRouterClient, hidden_token, login

TUNNEL_ID = '1839b5cd-3aa5-45ff-9278-010f2d33f0e9'
CREDENTIALS = dict(AccountTag='a' * 32, TunnelSecret=base64.b64encode(b'x' * 32).decode(), TunnelID=TUNNEL_ID)
TOKEN = base64.b64encode(json.dumps(dict(a=CREDENTIALS['AccountTag'], s=CREDENTIALS['TunnelSecret'], t=TUNNEL_ID)).encode()).decode()


def test_credential_formats():
    assert manager.valid_credentials('local', json.dumps(CREDENTIALS)) == CREDENTIALS
    assert manager.valid_credentials('remote', TOKEN) == CREDENTIALS
    assert manager.valid_credentials('local', json.dumps({**CREDENTIALS, 'Endpoint': ''})) == CREDENTIALS


@pytest.mark.parametrize('mode,secret', [
    ('local', '{}'), ('local', '[]'), ('remote', 'secret-not-a-token'),
    ('evil', TOKEN), ('local', json.dumps({**CREDENTIALS, 'Endpoint': 'https://evil.test'})),
    ('local', json.dumps({**CREDENTIALS, 'TunnelID': []})),
    ('local', json.dumps({**CREDENTIALS, 'TunnelSecret': 'bad'})),
    ('remote', base64.b64encode(json.dumps(dict(a='bad', t=TUNNEL_ID, s='bad')).encode()).decode()),
])
def test_invalid_credentials(mode, secret):
    with pytest.raises(manager.IntegrationError):
        manager.valid_credentials(mode, secret)


def test_auth_url_allowlist():
    assert manager.safe_auth_url('https://login.tailscale.com/a/abcdef')
    for value in ['javascript:alert(1)', 'https://login.tailscale.com.evil.test/a/key',
                  'https://evil.test/a/key', 'https://login.tailscale.com/a/key?secret=x', None]:
        assert not manager.safe_auth_url(value)


def test_key_expiry_is_readonly_and_handles_missing_or_invalid_data():
    for value in [None, '', 'invalid', '2026-01-01', '0001-01-01T00:00:00Z']:
        assert manager.key_expiry(value) == dict(key_expiry='', key_expiry_days=None)
    result = manager.key_expiry('2000-01-01T00:00:00Z')
    assert result['key_expiry'] == '2000-01-01 00:00 UTC'
    assert result['key_expiry_days'] < 0


def test_tailscale_status_redacts(monkeypatch):
    def api(method, endpoint, payload=None):
        if endpoint == 'profiles/current':
            return {'ID': 'existing', 'UserProfile': {'secret': 'SECRET'}}
        if endpoint == 'prefs':
            return dict(Hostname='gateway', AdvertiseRoutes=manager.ROUTES)
        return dict(BackendState='Running', TailscaleIPs=['100.64.1.2'], Peer={'secret': 'SECRET'},
                    Self=dict(Online=True, DNSName='gateway.example.ts.net.'))
    monkeypatch.setattr(manager, 'local_api', api)
    result = manager.tailscale_status()
    assert result['profile_present']
    assert 'SECRET' not in json.dumps(result)
    assert result['dns_name'] == 'gateway.example.ts.net'


@pytest.mark.parametrize('state', ['Running', 'Starting', 'NeedsMachineAuth'])
def test_connected_tailscale_not_reauthenticated(monkeypatch, state):
    before = dict(state=state)
    monkeypatch.setattr(manager, 'tailscale_status', lambda: before)
    monkeypatch.setattr(manager, 'local_api', lambda *args: pytest.fail('Must not change existing connection'))
    assert manager.tailscale_connect('panel') == before


def test_resume_preserves_existing_preferences(monkeypatch):
    snapshots = iter([dict(state='Stopped', hostname='custom', profile_present=True),
                      dict(state='Running')])
    monkeypatch.setattr(manager, 'tailscale_status', lambda: next(snapshots))
    calls = []
    monkeypatch.setattr(manager, 'local_api', lambda *args: calls.append(args))
    assert manager.tailscale_connect('gateway')['state'] == 'Running'
    assert calls == [('PATCH', 'prefs', {'WantRunning': True, 'WantRunningSet': True})]


def test_initial_gateway_login_applies_fixed_routes(monkeypatch):
    snapshots = iter([dict(state='NeedsLogin', hostname='gateway', profile_present=False, auth_url=''),
                      dict(state='NeedsLogin', auth_url='https://login.tailscale.com/a/example')])
    monkeypatch.setattr(manager, 'tailscale_status', lambda: next(snapshots))
    calls = []
    monkeypatch.setattr(manager, 'local_api', lambda *args: calls.append(args))
    result = manager.tailscale_connect('gateway')
    assert result['auth_url']
    assert calls[0][2]['AdvertiseRoutes'] == manager.ROUTES
    assert calls[1] == ('POST', 'login-interactive', {})


@pytest.mark.parametrize('payload', [
    dict(action='integration-action', target='999', operation=dict(action='tailscale-connect')),
    dict(action='integration-action', target='gateway', operation=dict(action='panel-serve')),
    dict(action='integration-action', target='panel', operation=dict(action='tailscale-connect', command='rm')),
    dict(action='integration-action', target='panel', operation=dict(action='logout')),
])
def test_dispatch_rejects_arbitrary_operations(payload):
    with pytest.raises(ValueError):
        integration_request(payload)


@pytest.fixture
def transaction(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, 'STATE', tmp_path / 'state')
    monkeypatch.setattr(manager, 'MANAGED', tmp_path / 'managed')
    monkeypatch.setattr(manager, 'OVERRIDE', tmp_path / 'systemd' / 'override.conf')
    manager.STATE.mkdir()
    manager.OVERRIDE.parent.mkdir()
    manager.OVERRIDE.write_text('ORIGINAL')
    original = dict(override=base64.b64encode(b'ORIGINAL').decode(), metadata=None, was_active=True)
    monkeypatch.setattr(manager, 'snapshot', lambda: original)
    monkeypatch.setattr(manager.shutil, 'which', lambda *args, **kwargs: '/usr/local/bin/cloudflared')
    monkeypatch.setattr(manager, 'cloudflare_status', lambda: dict(pending=False, revision='r', mode='local', tunnel_id='old'))
    calls = []
    monkeypatch.setattr(manager, 'command', lambda *args, **kwargs: calls.append(args) or '')
    monkeypatch.setattr(manager.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=0))
    request = dict(action='cloudflare-apply', mode='remote', secret=TOKEN, expected_revision='r')
    return request, calls


def test_cloudflare_success_commits_secret_files_only(transaction, monkeypatch):
    request, calls = transaction
    monkeypatch.setattr(manager, 'wait_ready', lambda *args: True)
    manager.apply_cloudflare(request)
    assert not (manager.STATE / 'pending.json').exists()
    assert (manager.STATE / 'last-backup.json').exists()
    assert manager.read_json(manager.STATE / 'cloudflare.json')['tunnel_id'] == TUNNEL_ID
    token_file = next(manager.MANAGED.glob('*/connector-token'))
    assert token_file.read_text() == TOKEN
    assert token_file.stat().st_mode & 0o777 == 0o600
    assert TOKEN not in str(calls)
    assert TOKEN not in manager.OVERRIDE.read_text()
    assert 'enable' in str(calls)


def test_cloudflare_preflight_failure_keeps_old_service(transaction, monkeypatch):
    request, calls = transaction
    monkeypatch.setattr(manager, 'wait_ready', lambda *args: False)
    with pytest.raises(manager.IntegrationError):
        manager.apply_cloudflare(request)
    assert manager.OVERRIDE.read_text() == 'ORIGINAL'
    assert not (manager.STATE / 'pending.json').exists()
    assert not list(manager.MANAGED.iterdir())
    assert not any('restart' in call for call in calls)


def test_cloudflare_failed_main_reverts(transaction, monkeypatch):
    request, calls = transaction
    results = iter([True, False])
    monkeypatch.setattr(manager, 'wait_ready', lambda *args: next(results))
    with pytest.raises(manager.IntegrationError):
        manager.apply_cloudflare(request)
    assert manager.OVERRIDE.read_text() == 'ORIGINAL'
    assert not (manager.STATE / 'pending.json').exists()
    assert not (manager.STATE / 'cloudflare.json').exists()
    assert sum('restart' in call for call in calls) == 2


def test_cloudflare_stale_revision_rejected(transaction, monkeypatch):
    request, calls = transaction
    with pytest.raises(manager.IntegrationError):
        manager.apply_cloudflare({**request, 'expected_revision': 'stale'})
    assert not calls and not manager.MANAGED.exists()


def test_cannot_convert_live_local_tunnel_silently(transaction, monkeypatch):
    request, calls = transaction
    monkeypatch.setattr(manager, 'cloudflare_status', lambda: dict(pending=False, revision='r', mode='local', tunnel_id=TUNNEL_ID))
    with pytest.raises(manager.IntegrationError):
        manager.apply_cloudflare(request)
    assert not calls


class FakeIntegrations:
    def __init__(self):
        ts = dict(state='Running', online=True, hostname='router', dns_name='router.example.ts.net',
                  ips=['100.64.1.2'], routes=[], auth_url='')
        self.state = dict(errors=[], panel=ts, gateway={**ts, 'hostname': 'gateway', 'routes': manager.ROUTES},
                          cloudflare=dict(mode='local', tunnel_id=TUNNEL_ID, dns_target=TUNNEL_ID + '.cfargotunnel.com',
                                          connected=True, revision='r', pending=False, can_rollback=False))
        self.calls = []
        self.fail = False

    def integrations(self):
        return deepcopy(self.state)

    def integration_action(self, target, operation):
        if self.fail:
            raise ServiceClientError('NEVER REFLECT ' + operation.get('secret', ''))
        self.calls.append((target, operation))
        return {}


@pytest.fixture
def integration_ui(tmp_path):
    path = tmp_path / 'app.db'
    db = Database(path)
    db.initialize()
    db.create_user('admin', 'correct horse battery staple')
    services = FakeIntegrations()
    app = create_app(AppSettings(database_path=path, router_command=('unused',)), FakeRouterClient(), services)
    with TestClient(app, base_url='https://domain-router.test') as client:
        yield client, services, db


def submitted(client):
    return dict(csrf_token=hidden_token(client.get('/integrations').text), operation='cloudflare-apply',
                target='panel', mode='remote', secret=TOKEN, revision='r', confirm='yes',
                current_password='correct horse battery staple')


def test_integration_page_authenticated_read_only(integration_ui):
    client, services, _ = integration_ui
    assert client.get('/integrations', follow_redirects=False).status_code == 303
    assert client.post('/integrations', data={}, follow_redirects=False).status_code == 303
    login(client)
    page = client.get('/integrations')
    assert page.status_code == 200 and TUNNEL_ID in page.text
    assert 'id="cloudflare-tunnel"' in page.text
    assert 'Шлюз приватных сервисов' in page.text
    assert not services.calls


def test_integration_mutations_require_csrf_password_and_confirmation(integration_ui):
    client, services, _ = integration_ui
    login(client)
    payload = submitted(client)
    assert client.post('/integrations', data={**payload, 'csrf_token': 'bad'}).status_code == 400
    assert client.post('/integrations', data={**payload, 'confirm': ''}).status_code == 400
    assert client.post('/integrations', data={**payload, 'current_password': 'wrong'}).status_code == 403
    assert not services.calls


def test_integrations_audit_does_not_store_credentials(integration_ui):
    client, services, db = integration_ui
    login(client)
    payload = submitted(client)
    assert client.post('/integrations', data=payload, follow_redirects=False).status_code == 303
    assert services.calls[0][1]['secret'] == TOKEN
    assert TOKEN not in json.dumps(db.audit_events())
    assert payload['current_password'] not in json.dumps(db.audit_events())
    services.fail = True
    response = client.post('/integrations', data=payload)
    assert response.status_code == 409
    assert TOKEN not in response.text and 'NEVER REFLECT' not in response.text
    assert TOKEN not in json.dumps(db.audit_events())


def test_gateway_cannot_change_cloudflare(integration_ui):
    client, services, _ = integration_ui
    login(client)
    response = client.post('/integrations', data={**submitted(client), 'target': 'gateway'})
    assert response.status_code == 400 and not services.calls


def test_bad_key_shows_safe_validation_error_without_agent_call(integration_ui):
    client, services, _ = integration_ui
    login(client)
    response = client.post('/integrations', data={**submitted(client), 'secret': 'do-not-echo-this-secret'})
    assert response.status_code == 400
    assert 'Некорректный формат' in response.text
    assert 'do-not-echo-this-secret' not in response.text
    assert not services.calls
