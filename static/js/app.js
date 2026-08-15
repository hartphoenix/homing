(() => {
  'use strict';
  document.querySelectorAll('[data-copy]').forEach((button) => {
    button.addEventListener('click', async () => {
      const sourceId = button.dataset.copySource;
      const input = sourceId ? document.getElementById(sourceId) : button.parentElement.querySelector('[data-copy-target]');
      if (!input) return;
      const value = 'value' in input ? input.value : input.textContent;
      const label = button.dataset.copyLabel || button.textContent;
      try {
        await navigator.clipboard.writeText(value);
        button.textContent = 'Copied';
      } catch (_) {
        input.select();
        button.textContent = document.execCommand('copy') ? 'Copied' : 'Select and copy';
      }
      window.setTimeout(() => { button.textContent = label; }, 1600);
    });
  });
})();
