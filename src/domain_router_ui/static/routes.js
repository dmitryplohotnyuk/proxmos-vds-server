'use strict';
for (const form of document.querySelectorAll('.route-access')) {
  form.querySelector('[role="switch"]').addEventListener('change', (event) => {
    event.target.disabled = true;
    form.requestSubmit();
  });
}
for (const form of document.querySelectorAll('form[action="/routes/probe"]')) {
  form.addEventListener('submit', () => {
    const button = form.querySelector('button');
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
  });
}
