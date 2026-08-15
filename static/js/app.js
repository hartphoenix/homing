(() => {
  'use strict';
  document.querySelectorAll('[data-copy]').forEach((button) => {
    button.addEventListener('click', async () => {
      const input = button.parentElement.querySelector('[data-copy-target]');
      if (!input) return;
      try { await navigator.clipboard.writeText(input.value); button.textContent = 'Copied'; }
      catch (_) { input.select(); document.execCommand('copy'); button.textContent = 'Copied'; }
      window.setTimeout(() => { button.textContent = 'Copy link'; }, 1600);
    });
  });
})();
