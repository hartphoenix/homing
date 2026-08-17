(() => {
  'use strict';
  document.querySelectorAll('[data-copy]').forEach((button) => {
    button.addEventListener('click', async () => {
      const sourceId = button.dataset.copySource;
      const input = sourceId ? document.getElementById(sourceId) : button.parentElement.querySelector('[data-copy-target]');
      if (!input) return;
      const value = 'value' in input ? input.value : input.textContent;
      const originalMarkup = button.innerHTML;
      try {
        await navigator.clipboard.writeText(value);
        button.textContent = 'Copied';
      } catch (_) {
        input.select();
        button.textContent = document.execCommand('copy') ? 'Copied' : 'Select and copy';
      }
      window.setTimeout(() => { button.innerHTML = originalMarkup; }, 1600);
    });
  });

  const batchForm = document.getElementById('batch-form');
  const checkboxes = [...document.querySelectorAll('.lead-checkbox')];
  const selectAll = document.querySelector('[data-select-all]');
  const selectionCount = document.querySelector('[data-selection-count]');
  const actionInput = batchForm && batchForm.querySelector('[data-batch-action-value]');

  const selected = () => checkboxes.filter((checkbox) => checkbox.checked);
  const syncSelection = () => {
    if (!batchForm) return;
    batchForm.querySelectorAll('[data-generated-lead-id]').forEach((input) => input.remove());
    selected().forEach((checkbox) => {
      const input = document.createElement('input');
      input.type = 'hidden';
      input.name = 'lead_ids';
      input.value = checkbox.value;
      input.dataset.generatedLeadId = 'true';
      batchForm.appendChild(input);
    });
    if (selectionCount) selectionCount.textContent = selected().length;
    if (selectAll) {
      selectAll.checked = checkboxes.length > 0 && selected().length === checkboxes.length;
      selectAll.indeterminate = selected().length > 0 && selected().length < checkboxes.length;
    }
  };
  checkboxes.forEach((checkbox) => checkbox.addEventListener('change', syncSelection));
  if (selectAll) {
    selectAll.addEventListener('change', () => {
      checkboxes.forEach((checkbox) => { checkbox.checked = selectAll.checked; });
      syncSelection();
    });
  }

  if (batchForm && actionInput) {
    batchForm.querySelectorAll('[data-batch-action]').forEach((button) => {
      button.addEventListener('click', (event) => {
        event.preventDefault();
        if (!selected().length) {
          window.alert('Select at least one lead first.');
          return;
        }
        actionInput.name = 'action';
        actionInput.value = button.dataset.batchAction;
        syncSelection();
        batchForm.submit();
      });
    });
  }

  const trashButton = document.querySelector('[data-batch-trash]');
  const trashDialog = document.querySelector('[data-batch-trash-dialog]');
  const trashDialogForm = document.querySelector('[data-batch-trash-dialog-form]');
  if (trashButton && trashDialog && batchForm && actionInput) {
    trashButton.addEventListener('click', () => {
      if (!selected().length) {
        window.alert('Select at least one lead first.');
        return;
      }
      if (typeof trashDialog.showModal === 'function') trashDialog.showModal();
      else trashDialog.setAttribute('open', 'open');
    });
    if (trashDialogForm) {
      trashDialogForm.addEventListener('submit', (event) => {
        if (event.submitter && event.submitter.value !== 'delete') return;
        event.preventDefault();
        actionInput.name = 'action';
        actionInput.value = 'trash';
        let commentInput = batchForm.querySelector('[data-batch-comment]');
        if (!commentInput) {
          commentInput = document.createElement('input');
          commentInput.type = 'hidden';
          commentInput.name = 'comment';
          commentInput.dataset.batchComment = 'true';
          batchForm.appendChild(commentInput);
        }
        commentInput.value = trashDialogForm.querySelector('[name="comment"]').value;
        syncSelection();
        trashDialog.close();
        batchForm.submit();
      });
    }
  }
  syncSelection();

  const memberRemoveDialog = document.querySelector('[data-member-remove-dialog]');
  const memberRemoveForm = document.querySelector('[data-member-remove-form]');
  const memberRemoveName = document.querySelector('[data-member-remove-name]');
  if (memberRemoveDialog && memberRemoveForm) {
    document.querySelectorAll('[data-member-remove]').forEach((button) => {
      button.addEventListener('click', () => {
        memberRemoveForm.action = button.dataset.removeUrl;
        if (memberRemoveName) memberRemoveName.textContent = button.dataset.memberName;
        if (typeof memberRemoveDialog.showModal === 'function') memberRemoveDialog.showModal();
        else memberRemoveDialog.setAttribute('open', 'open');
      });
    });
    const cancel = memberRemoveDialog.querySelector('[data-member-remove-cancel]');
    if (cancel) cancel.addEventListener('click', () => memberRemoveDialog.close());
  }
})();
