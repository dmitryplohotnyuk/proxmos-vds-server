const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const path = require('node:path');

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(key, value) { this.attributes[key] = value; }
  attachShadow() { this.shadowRoot = new Element('shadow'); }
}
const registry = new Map();
const context = vm.createContext({
  HTMLElement: Element,
  document: {createElement: tag => new Element(tag)},
  customElements: {get: key => registry.get(key), define: (key, value) => registry.set(key, value)},
});
const source = fs.readFileSync(path.join(__dirname, '../config/home-assistant/proxmox-guests-card.js'), 'utf8');
vm.runInContext(source + '\nthis.model = guestCardModel;', context);
const inventory = guests => ({state: '2', attributes: {guests}});
const guests = [
  {id: 108, name: 'vaultwarden', type: 'lxc', status: 'stopped'},
  {id: 101, name: 'content-factory', type: 'vm', status: 'running'},
  {id: 100, name: 'domain-router', type: 'lxc', status: 'running'},
];

test('groups, sorts, counts and maps distinct service icons', () => {
  const model = context.model(inventory(guests), 'lxc');
  assert.deepEqual(Array.from(model.rows, row => row.id), [100, 108]);
  assert.equal(model.running, 1);
  assert.notEqual(model.rows[0].icon, model.rows[1].icon);
  assert.equal(model.rows[1].label, 'Остановлен');
  assert.equal(context.model(inventory(guests), 'vm').rows.length, 1);
});

test('unknown and unavailable states never retain guest rows', () => {
  for (const state of ['unknown', 'unavailable']) {
    const result = context.model({...inventory(guests), state}, 'lxc');
    assert.equal(result.available, false);
    assert.equal(result.rows.length, 0);
  }
  assert.equal(context.model(undefined, 'lxc').available, false);
  assert.equal(context.model(inventory([]), 'lxc').available, true);
});

test('new guests and unknown statuses have safe fallbacks', () => {
  const result = context.model(inventory([{id: 200, type: 'lxc', name: 'new-service', status: 'anything'}]), 'lxc');
  assert.equal(result.rows[0].name, 'new-service');
  assert.equal(result.rows[0].status, 'unknown');
  assert.equal(result.rows[0].icon, 'mdi:cube-outline');
  assert.equal(context.model(inventory([{id: 201, type: 'lxc', name: '__proto__'}]), 'lxc').rows[0].name, '__proto__');
});

test('renders safe text, refreshes list, and ignores unrelated HA updates', () => {
  const Card = registry.get('proxmox-guests-card');
  const card = new Card();
  card.setConfig({entity: 'sensor.inventory', guest_type: 'lxc'});
  card.hass = {states: {'sensor.inventory': inventory(guests)}};
  assert.equal(card.card.children[1].children.length, 2);
  const original = card.card.children[1];
  card.hass = {states: {'sensor.inventory': inventory(guests), 'sensor.other': {state: 'changed'}}};
  assert.equal(card.card.children[1], original);
  card.hass = {states: {'sensor.inventory': inventory([{id: 200, type: 'lxc', name: '<img onerror=alert(1)>', status: 'paused'}])}};
  assert.equal(card.card.children[1].children[0].children[1].children[0].textContent, '<img onerror=alert(1)>');
  assert.ok(!source.includes('innerHTML'));
  card.hass = {states: {'sensor.inventory': {state: 'unavailable', attributes: {guests}}}};
  assert.equal(card.card.children[1].textContent, 'Данные недоступны');
  assert.throws(() => card.setConfig({guest_type: 'wrong'}));
});

test('standalone storage card renders disk and unavailable state', () => {
  const disk = {guest_id:104,total_bytes:1000,used_bytes:400,free_bytes:550,reserved_bytes:50,used_percent:42.1};
  const state = {state:'1',attributes:{guests:[{id:104,name:'media',type:'lxc',status:'running'}],media_storage:disk}};
  const card=new (registry.get('proxmox-storage-card'))();
  card.setConfig({entity:'sensor.inventory'});
  card.hass={states:{'sensor.inventory':state}};
  const body=card.card.children[2];
  assert.equal(body.children[1].attributes['aria-valuenow'],'42.1');
  const guestsCard=new (registry.get('proxmox-guests-card'))();
  guestsCard.setConfig({entity:'sensor.inventory',guest_type:'lxc'});
  guestsCard.hass={states:{'sensor.inventory':state}};
  assert.equal(guestsCard.card.children[1].children[0].children.length,3);
  card.hass={states:{'sensor.inventory':{...state,state:'unavailable'}}};
  assert.equal(card.card.children[2].children[0].textContent,'Нет данных о диске');
});
