from copy import deepcopy
import json

import pytest

from domain_router_services import agent


def guest(vmid=104, identity='original'):
    return dict(kind='lxc', vmid=vmid, identity=identity, name='media', status='running')


def empty_state():
    return dict(services=[], aliases={}, revision=0, updated_at=None, errors=[])


def discovered(vmid=104, identity='original', ips=None):
    host = guest(vmid, identity)
    return agent.merge_snapshot(empty_state(), [host], {f'lxc:{vmid}': (
        ips or ['10.77.0.14', '192.168.0.156'], [('tcp', 8096, '0.0.0.0')])})


def test_parse_listeners():
    output = '''tcp LISTEN 0 512 0.0.0.0:8096 0.0.0.0:*
tcp LISTEN 0 512 [::]:22 [::]:*
tcp LISTEN 0 512 127.0.0.1:3000 0.0.0.0:*
udp UNCONN 0 0 0.0.0.0:53 0.0.0.0:*
udp UNCONN 0 0 0.0.0.0:5353 0.0.0.0:*
tcp LISTEN 0 512 172.30.32.1:1234 0.0.0.0:*
'''
    assert agent.parse_listeners(output) == [('tcp', 22, '::'), ('tcp', 8096, '0.0.0.0'), ('udp', 53, '0.0.0.0')]


def test_bootstrap_and_preserve_disabled_on_refresh():
    state = discovered()
    assert state['services'][0]['enabled'] is True
    state['services'][0]['enabled'] = False
    result = agent.merge_snapshot(state, [guest()], {'lxc:104': (
        ['10.77.0.14'], [('tcp', 8096, '*'), ('tcp', 445, '*')])})
    assert not any(item['enabled'] for item in result['services'])
    assert len(result['services']) == 2


def test_new_guests_start_off_and_stable_alias():
    first = discovered(vmid=110, ips=['192.168.0.190'])
    assert not first['services'][0]['enabled']
    assert first['services'][0]['address'] == '10.77.0.128'
    second = agent.merge_snapshot(first, [guest(110)], {'lxc:110': (
        ['192.168.0.191'], [('tcp', 8096, '*')])})
    assert second['services'][0]['address'] == '10.77.0.128'
    assert second['services'][0]['target'] == '192.168.0.191'
    nft = agent.firewall_text(second['services'])
    assert 'dnat ip to 192.168.0.191' in nft
    assert 'dport 8096 counter accept' not in nft


def test_missing_guest_or_listener_is_stale_and_identity_reuse_safe():
    state = discovered()
    failed = agent.merge_snapshot(state, [guest()], {})
    assert failed['services'][0]['stale']
    missing = agent.merge_snapshot(state, [guest()], {'lxc:104': (['10.77.0.14'], [])})
    assert missing['services'][0]['stale']
    assert agent.merge_snapshot(state, [guest(identity='recreated')], {})['services'] == []
    recreated = agent.merge_snapshot(state, [guest(identity='recreated')], {
        'lxc:104': (['10.77.0.14'], [('tcp', 8096, '*')])})
    assert not recreated['services'][0]['enabled']


def test_disabled_drop_precedes_established():
    items = discovered()['services']
    items[0]['enabled'] = False
    text = agent.firewall_text(items)
    assert text.index('dport 8096 counter drop') < text.index('ct state established')
    assert 'dport 8096 counter accept' not in text
    assert 'flush ruleset' not in text


@pytest.mark.parametrize('field,value', [('target', '8.8.8.8'), ('address', 'x; drop'),
                                        ('port', 0), ('protocols', ['icmp; accept'])])
def test_firewall_rejects_invalid_inventory(field, value):
    items = discovered()['services']
    items[0][field] = value
    with pytest.raises(ValueError):
        agent.firewall_text(items)


@pytest.fixture
def state_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, 'STATE_DIR', tmp_path)
    agent.atomic_json(tmp_path / 'inventory.json', discovered())
    return tmp_path


def request(**kwargs):
    return dict(action='toggle', id=discovered()['services'][0]['id'],
                enabled=False, expected_enabled=True, **kwargs)


def test_toggle_transaction_and_conflict(state_directory, monkeypatch):
    applied = []
    monkeypatch.setattr(agent, 'apply_firewall', lambda items: applied.append(deepcopy(items)))
    assert agent.handle_request(request())['enabled'] is False
    assert not json.loads((state_directory / 'inventory.json').read_text())['services'][0]['enabled']
    with pytest.raises(ValueError, match='another session'):
        agent.handle_request(request())
    assert len(applied) == 1


def test_failed_firewall_does_not_commit_state(state_directory, monkeypatch):
    def fail(items):
        raise RuntimeError('nft failed')
    monkeypatch.setattr(agent, 'apply_firewall', fail)
    with pytest.raises(RuntimeError):
        agent.handle_request(request())
    assert agent.read_state()['services'][0]['enabled']


@pytest.mark.parametrize('change', [{'enabled': 'false'}, {'id': 'evil'}, {'extra': 'argument'}])
def test_restricted_request_rejects_bad_fields(state_directory, monkeypatch, change):
    monkeypatch.setattr(agent, 'apply_firewall', lambda items: pytest.fail('Must not apply'))
    payload = request()
    payload.update(change)
    with pytest.raises(ValueError):
        agent.handle_request(payload)
