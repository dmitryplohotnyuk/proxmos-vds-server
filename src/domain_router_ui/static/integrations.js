const secret = document.querySelector('textarea[name="secret"]');
const fileInput = document.querySelector('[data-file-input]');
document.querySelectorAll('input[name="mode"]').forEach(input => {
  input.addEventListener('change', () => {
    const local = input.value === 'local';
    document.querySelector('[data-credentials-file]').hidden = !local;
    document.querySelector('[data-secret-label]').textContent = local ? 'Содержимое credentials JSON' : 'Connector token';
    secret.value = '';
    fileInput.value = '';
    fileInput.setCustomValidity('');
  });
});
fileInput?.addEventListener('change', async () => {
  const file = fileInput.files[0];
  if (!file) return;
  if (file.size > 8192) {
    fileInput.setCustomValidity('Файл должен быть не больше 8 КиБ.');
    fileInput.reportValidity();
    return;
  }
  fileInput.setCustomValidity('');
  const content = await file.text();
  if (fileInput.files[0] === file && document.querySelector('input[name="mode"]:checked').value === 'local') secret.value = content;
});
document.querySelectorAll('[data-copy-target]').forEach(button => {
  button.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(button.dataset.copyTarget); button.title = 'Скопировано'; }
    catch { button.title = 'Не удалось скопировать'; }
  });
});
document.querySelectorAll('[data-integration-form]').forEach(form => {
  form.addEventListener('submit', event => {
    if (form.dataset.submitting) { event.preventDefault(); return; }
    form.dataset.submitting = 'true';
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    const progress = form.querySelector('.integration-progress');
    if (progress) progress.hidden = false;
  });
});
if (document.querySelector('[data-awaiting-auth]')) {
  setInterval(() => {
    const editing = [...document.querySelectorAll('input[type="password"], textarea')].some(input => input.value);
    if (!editing && !document.querySelector('[data-submitting]')) location.reload();
  }, 15000);
}
window.addEventListener('pagehide', () => {
  if (secret) secret.value = '';
  document.querySelectorAll('input[type="password"]').forEach(input => { input.value = ''; });
});
