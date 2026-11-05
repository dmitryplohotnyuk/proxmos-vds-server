const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '../src/proxmox_usb_backup/static');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'backup.js'), 'utf8');

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.hidden = false; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener() {}
  set innerHTML(_) { throw new Error('Use textContent, not HTML'); }
}
function harness() {
  const elements = new Map();
  const document = {
    createElement: tag => new Element(tag),
    getElementById(id) {
      assert.ok(html.includes(`id="${id}"`), `Missing HTML element: ${id}`);
      if (!elements.has(id)) elements.set(id, new Element('div'));
      return elements.get(id);
    },
  };
  const context = vm.createContext({document, fetch: () => new Promise(() => {}), setInterval() {}, setTimeout() {}});
  vm.runInContext(script, context);
  return {render: context.render, get: id => document.getElementById(id)};
}
function fixture() {
  return {csrf:'test', proxmox_url:'https://new-proxmox.example/', disk:{online:true,
    checked_at:new Date().toISOString(), total_bytes:100000, used_bytes:1000, free_bytes:99000,
    inventory:[{id:100,kind:'lxc',name:'router'},{id:101,kind:'qemu',name:'ubuntu'}]},
  job:{running:true,phase:'guests',completed_guests:1,total_guests:2,
    guests:[{id:100,kind:'lxc',name:'router',bytes:400}],current_guest:{id:101,kind:'qemu',name:'ubuntu'}}};
}
test('live queue uses actual HTML IDs, verified status, and dynamic Proxmox link', () => {
  const ui = harness(); ui.render(fixture());
  assert.equal(ui.get('proxmox-link').href, 'https://new-proxmox.example/');
  assert.equal(ui.get('disk-status').textContent, 'Подключён');
  assert.equal(ui.get('guests-heading').textContent, 'Текущий комплект');
  assert.equal(ui.get('guests').children[0].children[3].textContent, 'Проверен');
  assert.equal(ui.get('guests').children[1].children[3].textContent, 'Создаётся');
  assert.equal(ui.get('run').disabled, true);
});
test('stale health data and genuine read-only disk never allow a new backup', () => {
  const ui = harness(); const data = fixture(); data.job = {};
  data.disk.checked_at = new Date(Date.now() - 240000).toISOString();
  ui.render(data);
  assert.equal(ui.get('disk-status').textContent, 'Нет свежих данных');
  assert.equal(ui.get('run').disabled, true);
  assert.equal(ui.get('notice').hidden, false);
  data.disk.checked_at = new Date().toISOString(); data.disk.online = false;
  data.disk.error = 'Backup filesystem is read-only'; ui.render(data);
  assert.match(ui.get('disk-detail').textContent, /только для чтения/);
  data.disk.online = true; data.job.auto_retry_blocked = true; ui.render(data);
  assert.equal(ui.get('notice').hidden, false);
  assert.match(ui.get('notice').textContent, /ошибки целостности/);
});
test('untrusted guest names remain plain text and first failure does not claim a preserved set', () => {
  const ui = harness(); const data = fixture(); data.job = {phase:'failed'};
  data.disk.inventory[0].name = '<img src=x onerror=alert(1)>'; ui.render(data);
  assert.equal(ui.get('guests').children[0].children[0].children[0].textContent, data.disk.inventory[0].name);
  assert.equal(ui.get('job-message').textContent, 'Завершённого комплекта пока нет');
});
