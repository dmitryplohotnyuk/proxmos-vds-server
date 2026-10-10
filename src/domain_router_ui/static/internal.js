'use strict';
let submitting = false;
const search = document.getElementById('service-search');
const filter = document.getElementById('service-filter');
const groups = [...document.querySelectorAll('[data-guest-group]')];
let expanded = {};
try {
  search.value = sessionStorage.getItem('service-search') || '';
  filter.value = sessionStorage.getItem('service-filter') || 'all';
  expanded = JSON.parse(sessionStorage.getItem('service-groups') || '{}');
} catch (_) { expanded = {}; }
function save(key, value) { try { sessionStorage.setItem(key, value); } catch (_) {} }
function applyFilters() {
  const query = search.value.trim().toLowerCase();
  const filtering = Boolean(query) || filter.value !== 'all';
  let visible = 0;
  for (const group of groups) {
    const identity = group.querySelector('summary').textContent.toLowerCase();
    let matches = 0;
    for (const row of group.querySelectorAll('[data-service-row]')) {
      row.hidden = !(identity + ' ' + row.textContent.toLowerCase()).includes(query) ||
        (filter.value !== 'all' && row.dataset.enabled !== filter.value);
      if (!row.hidden) matches++;
    }
    group.hidden = matches === 0;
    group.open = filtering ? matches > 0 : Boolean(expanded[group.dataset.guestGroup]);
    visible += matches;
  }
  document.getElementById('inventory-no-results').hidden = visible > 0 || groups.length === 0;
  save('service-search', search.value);
  save('service-filter', filter.value);
}
for (const group of groups) {
  group.open = Boolean(expanded[group.dataset.guestGroup]);
  group.addEventListener('toggle', () => {
    if (search.value.trim() || filter.value !== 'all') return;
    expanded[group.dataset.guestGroup] = group.open;
    save('service-groups', JSON.stringify(expanded));
  });
}
search.addEventListener('input', applyFilters);
filter.addEventListener('change', applyFilters);
applyFilters();
document.getElementById('inventory-refresh').addEventListener('click', () => location.reload());
for (const form of document.querySelectorAll('.access-form')) {
  form.querySelector('[role="switch"]').addEventListener('change', (event) => {
    submitting = true;
    event.target.disabled = true;
    form.requestSubmit();
  });
}
for (const button of document.querySelectorAll('[data-copy]')) {
  button.addEventListener('click', async (event) => {
    event.preventDefault();
    event.stopPropagation();
    const status = document.getElementById('copy-status');
    try {
      await navigator.clipboard.writeText(button.dataset.copy);
      status.textContent = 'Скопировано';
    } catch (_) { status.textContent = 'Не удалось скопировать'; }
    setTimeout(() => { status.textContent = ''; }, 1800);
  });
}
for (const time of document.querySelectorAll('time')) {
  const date = new Date(time.textContent);
  if (!Number.isNaN(date.valueOf())) {
    time.dateTime = date.toISOString();
    time.textContent = date.toLocaleString('ru-RU', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
    time.title = date.toLocaleString('ru-RU');
  }
}
setInterval(() => {
  if (!submitting && document.visibilityState === 'visible' &&
      document.activeElement !== search && document.activeElement !== filter) location.reload();
}, 30000);
