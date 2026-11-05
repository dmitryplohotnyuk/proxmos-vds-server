const byId = id => document.getElementById(id);
let token = '';
let busy = false;
function bytes(value) {
  if (value == null) return '—';
  return (value / 1073741824).toLocaleString('ru-RU', {maximumFractionDigits: 1}) + ' ГиБ';
}
function date(value) {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('ru-RU', {day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'});
}
function text(id, value) { byId(id).textContent = value; }
function node(tag, value, cls) {
  const element = document.createElement(tag);
  if (value !== undefined) element.textContent = value;
  if (cls) element.className = cls;
  return element;
}
function render(data) {
  token = data.csrf;
  byId('proxmox-link').href = data.proxmox_url;
  const disk = data.disk || {};
  const job = data.job || {};
  const last = job.last_success || {};
  busy = Boolean(disk.worker_active || job.running);
  const checked = new Date(disk.checked_at).getTime();
  const stale = !Number.isFinite(checked) || Date.now() - checked > 180000;
  const online = disk.online === true && !stale;
  text('disk-status', stale ? 'Нет свежих данных' : online ? 'Подключён' : 'Недоступен');
  byId('disk-status').className = online ? 'success' : 'failure';
  text('disk-detail', stale ? 'Проверка статуса не обновлялась более трёх минут' : online ? bytes(disk.free_bytes) + ' свободно из ' + bytes(disk.total_bytes) : disk.error?.includes('read-only') ? 'Файловая система доступна только для чтения' : 'USB-раздел бэкапов не смонтирован');
  byId('disk-usage').value = online ? 100 * disk.used_bytes / disk.total_bytes : 0;
  text('last-date', last.completed_at ? date(last.completed_at) : 'Копий пока нет');
  text('last-detail', last.completed_at ? bytes(last.total_bytes) + ' · ' + last.guests.length + ' гостей' : 'Первый комплект ещё не завершён');
  text('next-run', disk.next_run || 'Расписание остановлено');
  text('updated', 'Обновлено ' + date(disk.checked_at));
  const phases = {preflight:'Подготовка',guests:'Создание копии',verify:'Проверка архива',host:'Настройки хоста',success:'Успешно',failed:'Ошибка'};
  text('job-status', busy ? (phases[job.phase] || 'Выполняется') : (phases[job.phase] || 'Ожидание'));
  byId('job-status').className = 'badge ' + (busy ? 'warning' : job.phase === 'success' ? 'success' : job.phase === 'failed' ? 'failure' : '');
  const guest = job.current_guest;
  text('current-guest', guest ? (guest.kind === 'lxc' ? 'LXC ' : 'VM ') + guest.id + ' · ' + guest.name : busy ? (phases[job.phase] || 'Подготовка') : last.id ? 'Комплект готов к восстановлению' : 'Ожидание первого запуска');
  const done = job.completed_guests || 0;
  const total = job.total_guests || 0;
  byId('job-progress').value = total ? Math.min(100, 100 * (done + (busy && guest ? (job.guest_percent || 0) / 100 : 0)) / total) : 0;
  text('job-count', total ? done + ' / ' + total + ' гостей' : '');
  const messages = {preflight:'Проверка накопителя и состава гостей',guests:'Архивирование дисков',verify:'Проверка целостности и структуры',host:'Конфигурация Proxmox и комплект восстановления',success:'Все архивы проверены',failed:'Предыдущий успешный комплект сохранён'};
  text('job-message', job.phase === 'failed' && !last.id ? 'Завершённого комплекта пока нет' : messages[job.phase] || '');
  const error = [job.error, job.cleanup_warning].filter(Boolean).join('\n');
  byId('error').hidden = !error;
  text('error', error || '');
  byId('run').disabled = busy || !online;
  const guests = last.guests || [];
  const current = disk.inventory || [];
  const listed = busy || !last.id ? current : guests;
  const verified = new Map((busy ? job.guests || [] : guests).map(g => [g.kind + ':' + g.id, g]));
  text('guests-heading', busy ? 'Текущий комплект' : last.id ? 'Состав последнего комплекта' : 'Узлы для резервирования');
  const rows = listed.map(g => {
    const row = node('tr');
    const name = node('td');
    const saved = verified.get(g.kind + ':' + g.id);
    const inProgress = busy && guest && guest.kind === g.kind && guest.id === g.id;
    const state = saved ? 'Проверен' : inProgress ? (job.phase === 'verify' ? 'Проверка' : 'Создаётся') : 'Ожидает';
    name.append(node('div', g.name, 'guest-name'), node('div', 'ID ' + g.id, 'guest-id'));
    row.append(name, node('td', g.kind === 'lxc' ? 'LXC' : 'VM'), node('td', bytes(saved?.bytes)), node('td', state, saved ? 'success' : inProgress ? 'warning' : 'muted'));
    return row;
  });
  if (!rows.length) {
    const row = node('tr'); const cell = node('td', 'Нет завершённого комплекта', 'muted'); cell.colSpan = 4; row.append(cell); rows.push(row);
  }
  byId('guests').replaceChildren(...rows);
  text('guest-total', listed.length ? listed.length + ' гостей' : '');
  const ids = new Set(guests.map(g => g.kind + ':' + g.id));
  const active = new Set(current.map(g => g.kind + ':' + g.id));
  const added = current.filter(g => !ids.has(g.kind + ':' + g.id));
  const removed = guests.filter(g => !active.has(g.kind + ':' + g.id));
  text('inventory-changes', guests.length && (added.length || removed.length) ? 'Следующий комплект: новых ' + added.length + ', удалённых ' + removed.length : '');
  const warnings = job.warnings || last.warnings || [];
  byId('scope-section').hidden = !warnings.length;
  byId('exclusions').replaceChildren(...warnings.map(w => {const item = node('li'); item.append(node('code', w.source), node('div', 'ID ' + w.guest + ' · ' + w.reason, 'muted')); return item;}));
  text('set-id', last.id || 'Нет завершённых комплектов');
  const age = last.completed_at ? Date.now() - new Date(last.completed_at).getTime() : 0;
  byId('notice').hidden = online && !job.auto_retry_blocked && (!age || age < 8 * 86400000);
  text('notice', stale ? 'Нет свежего статуса накопителя. Запуск копии временно недоступен.' : !online ? 'Накопитель недоступен. Создание копий приостановлено.' : job.auto_retry_blocked ? 'Автоповтор остановлен после ошибки целостности. Перед новым запуском проверьте накопитель.' : 'Последняя успешная копия старше восьми дней.');
}
async function refresh() {
  try {
    const response = await fetch('/api/status', {cache:'no-store'});
    if (!response.ok) throw new Error('Нет доступа к статусу: HTTP ' + response.status);
    render(await response.json());
  } catch (error) { byId('notice').hidden = false; text('notice', error.message); byId('run').disabled = true; }
}
byId('refresh').addEventListener('click', refresh);
byId('run').addEventListener('click', async () => {
  byId('run').disabled = true;
  try {
    const response = await fetch('/api/run', {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':token}, body:'{}'});
    if (!response.ok) throw new Error((await response.json()).error || 'Не удалось запустить копирование');
    text('job-status', 'Запуск…');
    setTimeout(refresh, 1500);
  } catch (error) { byId('notice').hidden = false; text('notice', error.message); byId('run').disabled = busy; }
});
refresh();
setInterval(refresh, 15000);
