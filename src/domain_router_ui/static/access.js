'use strict';
let expanded = {};
try { expanded = JSON.parse(sessionStorage.getItem('access-groups') || '{}'); } catch (_) {}
const filters = document.getElementById('access-filters');
const search = filters.elements.q;
const scope = filters.elements.scope;
let filtering = Boolean(search.value.trim());
for (const group of document.querySelectorAll('[data-access-group]')) {
  if (!filtering) group.open = Boolean(expanded[group.dataset.accessGroup]);
  group.addEventListener('toggle', () => {
    if (filtering) return;
    expanded[group.dataset.accessGroup] = group.open;
    try { sessionStorage.setItem('access-groups', JSON.stringify(expanded)); } catch (_) {}
  });
}
function applyFilters() {
  filtering = Boolean(search.value.trim());
  const query = search.value.trim().toLocaleLowerCase();
  let total = 0;
  for (const group of document.querySelectorAll('[data-access-group]')) {
    let count = 0;
    for (const row of group.querySelectorAll('[data-access-row]')) {
      const matches = scope.value === 'all' ||
        (scope.value === 'accounts' && row.dataset.account === 'yes') ||
        (scope.value === 'unlinked' && row.dataset.linked === 'no') ||
        (scope.value === 'ssh' && row.dataset.ssh === 'yes');
      row.hidden = !matches || !row.dataset.search.toLocaleLowerCase().includes(query);
      if (!row.hidden) count++;
    }
    group.hidden = count === 0;
    group.open = filtering ? count > 0 : Boolean(expanded[group.dataset.accessGroup]);
    total += count;
  }
  document.getElementById('access-results').textContent = `Найдено: ${total}`;
  document.getElementById('access-empty').hidden = total > 0;
}
search.addEventListener('input', applyFilters);
scope.addEventListener('change', applyFilters);
filters.addEventListener('submit', (event) => { event.preventDefault(); applyFilters(); });
for (const button of document.querySelectorAll('[data-copy]')) {
  button.addEventListener('click', async (event) => {
    event.preventDefault();
    event.stopPropagation();
    const status = document.getElementById('copy-status');
    try { await navigator.clipboard.writeText(button.dataset.copy); status.textContent = 'Скопировано'; }
    catch (_) { status.textContent = 'Не удалось скопировать'; }
    setTimeout(() => { status.textContent = ''; }, 1800);
  });
}
