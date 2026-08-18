(() => {
  'use strict';

  document.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-copy]');
    if (!button) return;
    const sourceId = button.dataset.copySource;
    const source = sourceId
      ? document.getElementById(sourceId)
      : button.parentElement.querySelector('[data-copy-target]');
    if (!source) return;
    const value = 'value' in source ? source.value : source.textContent;
    const originalMarkup = button.innerHTML;
    try {
      await navigator.clipboard.writeText(value);
      button.textContent = 'Copied';
    } catch (_) {
      source.select();
      button.textContent = document.execCommand('copy') ? 'Copied' : 'Select and copy';
    }
    window.setTimeout(() => { button.innerHTML = originalMarkup; }, 1600);
  });

  const leadResults = document.getElementById('lead-results');
  const leadPage = document.querySelector('[data-lead-page]');
  const viewLinks = [...document.querySelectorAll('[data-view-link]')];
  const viewTemplates = [...document.querySelectorAll('[data-lead-view-template]')];
  const interestStates = new Map();
  const viewStorageKey = leadPage
    ? 'homing.lead-view.' + leadPage.dataset.userId + '.' + leadPage.dataset.projectSlug
    : '';

  const getStoredView = () => {
    if (!viewStorageKey) return null;
    try {
      const value = window.localStorage.getItem(viewStorageKey);
      return value === 'cards' || value === 'list' ? value : null;
    } catch (_) {
      return null;
    }
  };

  const storeView = (mode) => {
    if (!viewStorageKey) return;
    try {
      window.localStorage.setItem(viewStorageKey, mode);
    } catch (_) {
      // Private browsing and blocked storage should not disable the controls.
    }
  };

  const viewLink = (mode) => viewLinks.find((link) => link.dataset.viewLink === mode);
  const viewTemplate = (mode) => viewTemplates.find(
    (template) => template.dataset.leadViewTemplate === mode,
  );

  const updateViewControls = (mode) => {
    viewLinks.forEach((link) => {
      const active = link.dataset.viewLink === mode;
      link.classList.toggle('is-selected', active);
      if (active) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    const filterView = document.querySelector('.filter-bar input[name="view"]');
    if (filterView) filterView.value = mode;
  };

  const setLeadView = (mode, { push = false, replace = false, persist = true } = {}) => {
    if (!leadResults || (mode !== 'cards' && mode !== 'list')) return;
    const template = viewTemplate(mode);
    if (template && leadResults.dataset.viewMode !== mode) {
      leadResults.replaceChildren(template.content.cloneNode(true));
      leadResults.dataset.viewMode = mode;
      leadResults.className = mode === 'list' ? 'lead-list-rows' : 'lead-list';
      leadResults.querySelectorAll('[data-lead-item]').forEach((item) => {
        const state = interestStates.get(item.dataset.leadId);
        if (state) updateInterest(item, state);
      });
    }
    updateViewControls(mode);
    if (persist) storeView(mode);

    const link = viewLink(mode);
    if ((push || replace) && link) {
      const method = replace ? 'replaceState' : 'pushState';
      window.history[method]({}, '', link.href);
    }
    syncSelection();
  };

  viewLinks.forEach((link) => {
    link.addEventListener('click', (event) => {
      if (!leadResults || !viewTemplate(link.dataset.viewLink)) return;
      event.preventDefault();
      setLeadView(link.dataset.viewLink, { push: true });
    });
  });

  window.addEventListener('popstate', () => {
    if (!leadResults) return;
    const requested = new URL(window.location.href).searchParams.get('view');
    setLeadView(
      requested === 'cards' || requested === 'list' ? requested : (getStoredView() || 'list'),
      { persist: false },
    );
  });

  const batchForm = document.getElementById('batch-form');
  const selectAll = document.querySelector('[data-select-all]');
  const selectionCount = document.querySelector('[data-selection-count]');
  const actionInput = batchForm && batchForm.querySelector('[data-batch-action-value]');
  const selectedCheckboxes = () => leadResults
    ? [...leadResults.querySelectorAll('.lead-checkbox')].filter((checkbox) => checkbox.checked)
    : [];

  const syncSelection = () => {
    if (!batchForm) return;
    batchForm.querySelectorAll('[data-generated-lead-id]').forEach((input) => input.remove());
    selectedCheckboxes().forEach((checkbox) => {
      const input = document.createElement('input');
      input.type = 'hidden';
      input.name = 'lead_ids';
      input.value = checkbox.value;
      input.dataset.generatedLeadId = 'true';
      batchForm.appendChild(input);
    });
    if (selectionCount) selectionCount.textContent = selectedCheckboxes().length;
    if (selectAll) {
      const checkboxes = leadResults ? [...leadResults.querySelectorAll('.lead-checkbox')] : [];
      selectAll.checked = checkboxes.length > 0 && selectedCheckboxes().length === checkboxes.length;
      selectAll.indeterminate = selectedCheckboxes().length > 0
        && selectedCheckboxes().length < checkboxes.length;
    }
  };

  if (leadResults) {
    leadResults.addEventListener('change', (event) => {
      if (event.target.matches('.lead-checkbox')) syncSelection();
    });
  }
  if (selectAll) {
    selectAll.addEventListener('change', () => {
      if (!leadResults) return;
      leadResults.querySelectorAll('.lead-checkbox').forEach((checkbox) => {
        checkbox.checked = selectAll.checked;
      });
      syncSelection();
    });
  }

  if (batchForm && actionInput) {
    batchForm.querySelectorAll('[data-batch-action]').forEach((button) => {
      button.addEventListener('click', (event) => {
        event.preventDefault();
        if (!selectedCheckboxes().length) {
          window.alert('Select at least one lead first.');
          return;
        }
        actionInput.name = 'action';
        actionInput.value = button.dataset.batchAction;
        syncSelection();
        HTMLFormElement.prototype.submit.call(batchForm);
      });
    });
  }

  const trashButton = document.querySelector('[data-batch-trash]');
  const trashDialog = document.querySelector('[data-batch-trash-dialog]');
  const trashDialogForm = document.querySelector('[data-batch-trash-dialog-form]');
  if (trashButton && trashDialog && batchForm && actionInput) {
    trashButton.addEventListener('click', () => {
      if (!selectedCheckboxes().length) {
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
        HTMLFormElement.prototype.submit.call(batchForm);
      });
    }
  }

  const csrfToken = (form) => {
    const field = form.querySelector('input[name="csrfmiddlewaretoken"]');
    if (field && field.value) return field.value;
    const cookie = document.cookie.split('; ').find((part) => part.startsWith('csrftoken='));
    if (cookie) return decodeURIComponent(cookie.split('=').slice(1).join('='));
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta && meta.content && meta.content !== 'NOTPROVIDED' ? meta.content : '';
  };

  const updateInterest = (item, payload) => {
    const interested = Boolean(payload.is_interested);
    const count = Number(payload.interest_count) || 0;
    const members = Array.isArray(payload.interested_members) ? payload.interested_members : [];
    if (item.dataset.leadId) {
      interestStates.set(item.dataset.leadId, {
        is_interested: interested,
        interest_count: count,
        interested_members: members,
      });
    }
    item.classList.toggle('has-interest', count > 0);
    item.classList.toggle('is-interested', interested);
    item.querySelectorAll('.interest-button').forEach((button) => {
      button.classList.toggle('has-interest', count > 0);
      button.classList.toggle('active', interested);
      button.setAttribute('aria-pressed', interested ? 'true' : 'false');
      button.title = interested ? 'Remove interest' : 'Mark interested';
      const label = button.querySelector('.sr-only');
      if (label) label.textContent = interested ? 'Remove interest' : 'Mark interested';
    });
    item.querySelectorAll('input[name="interested"]').forEach((input) => {
      input.value = interested ? 'false' : 'true';
    });
    item.querySelectorAll('[data-interest-icon]').forEach((icon) => {
      icon.textContent = interested ? '♥' : (count ? '♡' : '');
    });
    item.querySelectorAll('[data-interest-count]').forEach((countNode) => {
      countNode.textContent = countNode.closest('.lead-card')
        ? (count ? ' ' + count + ' interested' : ' No interest yet')
        : String(count);
    });
    item.querySelectorAll('[data-interest-members]').forEach((membersNode) => {
      membersNode.textContent = members.length ? ' · ' + members.join(', ') : '';
    });
    item.querySelectorAll('.interest-summary').forEach((summary) => {
      summary.title = members.join(', ');
    });
  };

  document.addEventListener('submit', async (event) => {
    const form = event.target.closest('[data-interest-form]');
    if (!form || !leadResults || !form.closest('#lead-results')) return;
    event.preventDefault();
    const button = form.querySelector('button[type="submit"], button:not([type])');
    if (button) button.disabled = true;
    try {
      const token = csrfToken(form);
      if (!token) throw new Error('Missing CSRF token');
      const response = await fetch(form.action, {
        method: 'POST',
        body: new FormData(form),
        credentials: 'same-origin',
        headers: {
          Accept: 'application/json',
          'X-CSRFToken': token,
          'X-Requested-With': 'XMLHttpRequest',
        },
      });
      if (!response.ok) throw new Error('Interest update failed (' + response.status + ')');
      const payload = await response.json();
      if (typeof payload.interest_count !== 'number') throw new Error('Invalid interest response');
      const item = form.closest('[data-lead-item]');
      if (item) updateInterest(item, payload);
    } catch (_) {
      // The form remains a normal POST when enhancement is unavailable or a
      // stale token is rejected, so users still get the server's fallback.
      HTMLFormElement.prototype.submit.call(form);
    } finally {
      if (button && document.contains(button)) button.disabled = false;
    }
  });

  // A URL containing view= is authoritative.  Otherwise restore the last
  // selection for this signed-in user and project, if browser storage
  // is available.  Native links still provide a complete no-JS fallback.
  if (leadResults) {
    const requested = new URL(window.location.href).searchParams.get('view');
    if (requested === 'cards' || requested === 'list') {
      storeView(requested);
    } else {
      const saved = getStoredView();
      if (saved && saved !== leadResults.dataset.viewMode) {
        setLeadView(saved, { replace: true });
      } else {
        updateViewControls(leadResults.dataset.viewMode || 'list');
      }
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

  const projectDeleteButton = document.querySelector('[data-project-delete]');
  const projectDeleteDialog = document.querySelector('[data-project-delete-dialog]');
  if (projectDeleteButton && projectDeleteDialog) {
    projectDeleteButton.addEventListener('click', () => {
      if (typeof projectDeleteDialog.showModal === 'function') projectDeleteDialog.showModal();
      else projectDeleteDialog.setAttribute('open', 'open');
    });
    const cancel = projectDeleteDialog.querySelector('[data-project-delete-cancel]');
    if (cancel) cancel.addEventListener('click', () => projectDeleteDialog.close());
  }
})();
