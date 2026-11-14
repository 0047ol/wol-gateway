/* User management is a regular page; selection and sort stay within this visit. */
WolPage.mount(({
  document,
  fetch,
  showToast,
  uiConfirm,
  setInterval,
  window,
  history
}) => {
  const body = document.getElementById('user-list-body');
  if (!body) return;
  const form = document.getElementById('create-user-form');
  const passwordForm = document.getElementById('user-password-form');
  let passwordUserId = '';
  let passwordTargets = [],
    passwordVersions, permissionTargets = [],
    permissionVersions, permissionBaselines = [],
    permissionConfirming = false;
  const permissionsForm = document.getElementById('user-permissions-form');
  const create = document.getElementById('user-create-toggle');
  const all = document.getElementById('user-select-all');
  const search = document.getElementById('user-search');
  const filter = document.getElementById('user-status-filter');
  const requestedStatus = new URLSearchParams(location.search).get('status');
  filter.value = ['enabled', 'disabled'].includes(requestedStatus) ? requestedStatus : 'all';
  const selected = new Set();
  let users = [],
    creating = false,
    loading = false;
  const busyUsers = new Set();
  const paths = {
    password: 'm15 4 5 5M4 20l5-1L21 7l-5-5L4 14v6Z',
    permissions: 'M12 3 3 7v5c0 5 9 9 9 9s9-4 9-9V7l-9-4Zm-4 9 3 3 5-6',
    disable: 'M8 5v14M16 5v14',
    enable: 'm8 5 11 7-11 7V5Z',
    delete: 'M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7'
  };
  const chosen = () => users.filter(user => selected.has(user.public_id));
  const visibleUsers = () => users.filter(user => user.username.toLocaleLowerCase().includes(search.value.trim().toLocaleLowerCase()) && (filter.value === 'all' || Boolean(user.enabled) === (filter.value === 'enabled')));
  const deletable = user => !user.gateway_count && !user.has_pending_pairing;

  function syncSelection() {
    const targets = chosen(),
      count = document.getElementById('user-selection-count');
    const mixed = targets.some(user => user.enabled) && targets.some(user => !user.enabled);
    const stateMenu = document.getElementById('user-bulk-state');
    stateMenu.hidden = !mixed;
    document.getElementById('user-bulk-state-action').disabled = targets.some(user => busyUsers.has(user.public_id));
    window.setAnimatedHidden?.(count, !targets.length);
    count.textContent = t(`已选择 ${targets.length} 项`);
    window.setListSelection?.(document.getElementById('user-bulk-actions'), targets.length > 0);
    const visible = visibleUsers();
    const visibleSelected = visible.filter(user => selected.has(user.public_id)).length;
    all.checked = Boolean(visible.length && visibleSelected === visible.length);
    all.indeterminate = visibleSelected > 0 && visibleSelected < visible.length;
    all.disabled = !visible.some(user => !busyUsers.has(user.public_id));
    document.querySelectorAll('#user-bulk-actions button[data-bulk]').forEach(button => {
      const applicable = targets.length && (['password', 'permissions'].includes(button.dataset.bulk) || (button.dataset.bulk === 'delete' ? targets.every(deletable) : !mixed && targets.every(user => Boolean(user.enabled) !== (button.dataset.bulk === 'enable'))));
      button.hidden = !applicable;
      button.disabled = targets.some(user => busyUsers.has(user.public_id));
    });
  }

  function control(action, label) {
    const button = document.createElement('button');
    button.className = 'icon-button gray';
    button.type = 'button';
    button.dataset.action = action;
    button.disabled = false;
    button.title = label;
    button.setAttribute('aria-label', t(label));
    const iconLabel = {
      password: '编辑',
      permissions: '权限设置',
      enable: '启用',
      disable: '禁用',
      delete: '删除'
    } [action];
    const icon = window.WolActionPaths?.[iconLabel] || paths[action];
    button.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${icon}"/></svg>`;
    if (action === 'delete') button.classList.replace('gray', 'danger');
    return button;
  }

  function renderUsers() {
    const rows = visibleUsers().map(user => {
      const row = document.createElement('tr'),
        checkbox = document.createElement('input');
      row.dataset.userId = user.public_id;
      checkbox.type = 'checkbox';
      checkbox.className = 'user-check';
      checkbox.value = user.public_id;
      checkbox.checked = selected.has(user.public_id);
      checkbox.disabled = busyUsers.has(user.public_id);
      checkbox.setAttribute('aria-label', t('选择用户') + ' ' + user.username);
      row.insertCell().append(checkbox);
      const name = row.insertCell();
      name.dataset.noI18n = '';
      name.className = 'user-account-name';
      const username = document.createElement('span');
      username.textContent = user.username;
      name.append(username);
      if (!user.enabled || (user.status && user.status !== 'normal')) {
        const badge = document.createElement('span');
        badge.className = 'badge ' + (!user.enabled ? 'user-disabled' : 'error');
        badge.textContent = t(!user.enabled ? '禁用' : '异常');
        badge.title = t(!user.enabled ? '当前用户已被禁用，请联系管理员重新启用' : '用户状态异常');
        name.append(badge);
      }
      row.insertCell().textContent = user.gateway_count;
      row.insertCell().textContent = user.device_count;
      row.insertCell().textContent = user.share_count;
      const actions = document.createElement('div');
      actions.className = 'row-actions user-row-actions';
      const toggle = control(user.enabled ? 'disable' : 'enable', user.enabled ? '禁用用户' : '启用用户');
      const remove = control('delete', '删除');
      remove.disabled = busyUsers.has(user.public_id) || !deletable(user);
      if (!deletable(user)) remove.title = '请先移交该用户的网关并取消有效配对码';
      actions.append(control('password', '编辑用户'), control('permissions', '权限设置'), toggle, remove);
      for (const button of actions.children) button.disabled ||= busyUsers.has(user.public_id);
      const cell = row.insertCell();
      cell.className = 'actions-cell';
      cell.append(actions);
      return row;
    });
    if (!rows.length) {
      const row = document.createElement('tr'),
        cell = row.insertCell();
      cell.colSpan = 6;
      cell.className = 'scan-empty';
      cell.textContent = t(search.value.trim() || filter.value !== 'all' ? '没有符合条件的用户' : '暂无用户，点击加号创建。');
      rows.push(row);
    }
    const incoming = new Map(rows.filter(row => row.dataset.userId).map(row => [row.dataset.userId, row]));
    for (const row of [...body.children]) {
      if (row.dataset.userId && !incoming.has(row.dataset.userId)) window.removeTableRow?.(row);
      else if (!row.dataset.userId && incoming.size) window.removeTableRow?.(row);
    }
    for (const [index, next] of rows.entries()) {
      const key = next.dataset.userId;
      const current = key ? [...body.children].find(row => row.dataset.userId === key) : [...body.children].find(row => !row.dataset.userId);
      if (!current) {
        body.append(next);
        window.animateTableRow?.(next);
      } else if (current !== next && !key) {
        if (current.firstElementChild.textContent !== next.firstElementChild.textContent) current.firstElementChild.textContent = next.firstElementChild.textContent;
      } else if (current !== next) {
        // Keep the focused checkbox/button and unchanged rows in the DOM.
        window.animateTableRow?.(current);
        const oldCheck = current.querySelector('.user-check');
        const nextCheck = next.querySelector('.user-check');
        if (oldCheck && nextCheck) {
          oldCheck.checked = nextCheck.checked;
          oldCheck.disabled = nextCheck.disabled;
        }
        for (let cell = 1; cell < 5; cell++) {
          if (current.cells[cell].innerHTML !== next.cells[cell].innerHTML) current.cells[cell].replaceChildren(...next.cells[cell].childNodes);
        }
        const buttons = current.querySelectorAll('[data-action]');
        next.querySelectorAll('[data-action]').forEach((button, index) => {
          const old = buttons[index];
          old.disabled = button.disabled;
          old.dataset.action = button.dataset.action;
          if (old.className !== button.className) old.className = button.className;
          if (old.innerHTML !== button.innerHTML) old.innerHTML = button.innerHTML;
          if ((old.title || old.dataset.tooltip) !== button.title) old.title = button.title;
          old.setAttribute('aria-label', button.getAttribute('aria-label'));
        });
      }
      const row = [...body.children].find(item => item === next || (key && item.dataset.userId === key));
      const reference = body.children[index];
      if (row && reference !== row) body.insertBefore(row, reference || null);
    }
    globalThis.WolTables?.reorder('user-table', row => users.find(user => user.public_id === row.dataset.userId) || {});
    syncSelection();
  }

  async function loadUsers() {
    if (loading) return;
    loading = true;
    try {
      const response = await fetch('/api/users', {
          cache: 'no-store'
        }),
        result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.detail || '用户列表读取失败');
      const changed = JSON.stringify(users) !== JSON.stringify(result.users);
      users = result.users;
      for (const id of selected)
        if (!users.some(user => user.public_id === id)) selected.delete(id);
      const count = document.getElementById('user-list-count');
      count.textContent = result.total;
      const enabled = users.filter(user => user.enabled).length;
      count.title = t(`${enabled}个已启用，${users.length - enabled}个已禁用`);
      if (changed) renderUsers();
    } catch (error) {
      showToast(error.message || '用户列表读取失败', {
        error: true
      });
      if (!users.length) {
        const row = document.createElement('tr'),
          cell = row.insertCell(),
          retry = document.createElement('button');
        cell.colSpan = 6;
        cell.className = 'scan-empty';
        retry.type = 'button';
        retry.className = 'gray';
        retry.textContent = t('重试');
        retry.addEventListener('click', loadUsers);
        cell.append(retry);
        body.replaceChildren(row);
      }
    } finally {
      loading = false;
      create.disabled = creating;
    }
  }

  async function mutate(url, data, wolVersions) {
    const response = await fetch(url, {
        method: 'POST',
        wolVersions,
        body: data
      }),
      result = await response.json();
    if (!response.ok || !result.ok) throw new Error(typeof result.detail === 'string' ? result.detail : result.msg || '操作失败');
    showToast(result.msg, {
      success: true
    });
  }

  function setBusy(value, ids = []) {
    for (const id of ids) value ? busyUsers.add(id) : busyUsers.delete(id);
    create.disabled = creating;
    form.querySelector('[type=submit]').disabled = creating;
    passwordForm.querySelector('[type=submit]').disabled = passwordTargets.some(user => busyUsers.has(user.public_id));
    syncPermissionSave();
    renderUsers();
  }
  async function operate(action, targets, bulk = false) {
    if (!targets.length || targets.some(user => busyUsers.has(user.public_id))) return;
    const versions = window.WolSync.snapshot();
    const verb = action === 'delete' ? '删除' : action === 'disable' ? '禁用' : '启用';
    const message = action === 'delete' ? (bulk ? '删除选中的用户？用户必须先移交全部网关并取消有效配对码。' : '删除此用户？请先移交全部网关并取消有效配对码。') : action === 'disable' ? (bulk ? '禁用后，所选用户暂停使用，保留登录和网关数据；重新启用后自动恢复。' : '禁用此用户后暂停使用，保留登录和网关数据；重新启用后自动恢复。') : (bulk ? '确认启用所选用户？' : '确认启用此用户？');
    if (!await uiConfirm(message, {
        title: action === 'delete' ? '删除用户' : action === 'disable' ? '禁用用户' : '启用用户',
        confirmText: verb,
        danger: action !== 'enable',
        countdown: bulk || action === 'delete' ? 3 : 0,
        skipCountdown: !bulk && action !== 'delete',
        targets: bulk ? targets.map(user => user.username) : undefined
      }) || targets.some(user => busyUsers.has(user.public_id))) return;
    const ids = targets.map(user => user.public_id);
    setBusy(true, ids);
    try {
      await mutate('/api/users/bulk', new URLSearchParams({
        ids: targets.map(user => user.public_id).join(','),
        action
      }), versions);
      for (const id of ids) selected.delete(id);
      await loadUsers();
    } catch (error) {
      showToast(error.message, {
        error: true
      });
      await loadUsers();
    } finally {
      setBusy(false, ids);
    }
  }
  create.addEventListener('click', () => {
    if (!creating) openDialog('create-user-dialog');
  });
  document.getElementById('user-password-dialog').addEventListener('close', () => {
    passwordForm.reset();
    passwordUserId = '';
    passwordTargets = [];
    passwordVersions = undefined;
    renderConfirmationTargets(document.getElementById('user-password-name'));
  });

  function openUserEditor(targets, bulk = false) {
    if (!targets.length || targets.some(user => busyUsers.has(user.public_id))) return;
    passwordForm.reset();
    passwordTargets = [...targets];
    passwordUserId = bulk ? '' : targets[0].public_id;
    passwordVersions = window.WolSync.snapshot();
    const name = document.getElementById('user-edit-username');
    name.disabled = bulk;
    name.value = bulk ? '' : targets[0].username;
    document.getElementById('user-edit-username-field').hidden = bulk;
    document.getElementById('user-password-new').required = bulk;
    document.getElementById('user-password-reset-help').textContent = t(bulk ? '为所选用户设置相同的8至128位密码，原登录将失效。' : '留空则保留原密码。');
    renderConfirmationTargets(document.getElementById('user-password-name'), bulk ? targets.map(user => user.username) : []);
    document.getElementById('user-password-title').textContent = t(bulk ? '批量修改密码' : '编辑用户');
    const dialog = document.getElementById('user-password-dialog');
    dialog.dataset.syncKeys = targets.map(user => 'user:' + user.public_id).join(' ');
    passwordForm.querySelector('[type=submit]').disabled = false;
    openDialog(dialog.id);
  }
  passwordForm.addEventListener('submit', async event => {
    event.preventDefault();
    if (!passwordTargets.length || passwordTargets.some(user => busyUsers.has(user.public_id))) return;
    const data = new FormData(passwordForm);
    const targets = [...passwordTargets],
      bulk = !passwordUserId,
      versions = {
        ...passwordVersions
      };
    if (bulk) {
      if (!await uiConfirm('为选中的用户设置相同密码？原登录将失效。', {
          title: '批量修改密码',
          confirmText: '保存',
          countdown: 3,
          targets: targets.map(user => user.username)
        })) return;
      if (targets.some(user => busyUsers.has(user.public_id))) return;
      data.set('ids', targets.map(user => user.public_id).join(','));
      data.set('action', 'password');
    }
    const ids = targets.map(user => user.public_id);
    setBusy(true, ids);
    try {
      await mutate(bulk ? '/api/users/bulk' : '/api/users/' + encodeURIComponent(ids[0]) + '/update', data, versions);
      closeDialog('user-password-dialog');
      for (const id of ids) selected.delete(id);
      await loadUsers();
    } catch (error) {
      showToast(error.message, {
        error: true
      });
    } finally {
      setBusy(false, ids);
    }
  });
  const permissionValues = () => JSON.stringify([...permissionsForm.querySelectorAll('input:checked')].map(node => node.value).sort());
  const permissionsChanged = () => permissionBaselines.some(baseline => baseline !== permissionValues());

  function syncPermissionSave() {
    permissionsForm.querySelector('[type=submit]').disabled = permissionConfirming || !permissionTargets.length || permissionTargets.some(user => busyUsers.has(user.public_id)) || !permissionsChanged();
  }

  function openPermissionEditor(targets, bulk = false) {
    if (!targets.length || targets.some(user => busyUsers.has(user.public_id))) return;
    permissionsForm.reset();
    permissionTargets = [...targets];
    permissionVersions = window.WolSync.snapshot();
    permissionBaselines = targets.map(user => JSON.stringify([...user.permissions].sort()));
    document.getElementById('user-permissions-title').textContent = t(bulk ? '批量设置权限' : '权限设置');
    const name = document.getElementById('user-permissions-name');
    name.textContent = bulk ? '' : targets[0].username;
    name.hidden = bulk;
    document.getElementById('user-permissions-note').hidden = !bulk;
    renderConfirmationTargets(document.getElementById('user-permissions-targets'), bulk ? targets.map(user => user.username) : []);
    for (const node of permissionsForm.querySelectorAll('input[name=permission]')) node.checked = targets.every(user => user.permissions.includes(node.value));
    const dialog = document.getElementById('user-permissions-dialog');
    dialog.dataset.permissionMode = bulk ? 'bulk' : 'single';
    dialog.dataset.syncKeys = targets.map(user => 'user:' + user.public_id).join(' ');
    syncPermissionSave();
    openDialog(dialog.id);
  }

  permissionsForm.addEventListener('change', syncPermissionSave);
  document.getElementById('user-permissions-dialog').addEventListener('close', () => {
    permissionsForm.reset();
    permissionTargets = [];
    permissionVersions = undefined;
    permissionBaselines = [];
    renderConfirmationTargets(document.getElementById('user-permissions-targets'));
    syncPermissionSave();
  });
  permissionsForm.addEventListener('submit', async event => {
    event.preventDefault();
    if (permissionConfirming || !permissionTargets.length || permissionTargets.some(user => busyUsers.has(user.public_id)) || !permissionsChanged()) return;
    const targets = [...permissionTargets],
      ids = targets.map(user => user.public_id),
      versions = {
        ...permissionVersions
      },
      bulk = document.getElementById('user-permissions-dialog').dataset.permissionMode === 'bulk',
      data = new URLSearchParams({
        permissions: permissionValues()
      });
    if (bulk) {
      let confirmed = false;
      permissionConfirming = true;
      syncPermissionSave();
      try {
        confirmed = await uiConfirm('将所选用户的权限统一替换为当前勾选项？未勾选的权限将被移除。', {
          title: '批量设置权限',
          confirmText: '保存',
          countdown: 3,
          targets: targets.map(user => user.username)
        });
      } finally {
        permissionConfirming = false;
        syncPermissionSave();
      }
      if (!confirmed || targets.some(user => busyUsers.has(user.public_id))) return;
      data.set('ids', ids.join(','));
      data.set('action', 'permissions');
    }
    setBusy(true, ids);
    try {
      await mutate(bulk ? '/api/users/bulk' : '/api/users/' + ids[0] + '/permissions', data, versions);
      closeDialog('user-permissions-dialog');
      for (const id of ids) selected.delete(id);
      await loadUsers();
    } catch (error) {
      showToast(error.message, {
        error: true
      });
    } finally {
      setBusy(false, ids);
    }
  });
  document.getElementById('create-user-dialog').addEventListener('close', () => form.reset());
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (creating) return;
    const data = new FormData(form);
    creating = true;
    setBusy(true);
    try {
      await mutate('/api/users', data);
      form.reset();
      closeDialog('create-user-dialog');
      await loadUsers();
    } catch (error) {
      showToast(error.message, {
        error: true
      });
    } finally {
      creating = false;
      setBusy(false);
    }
  });
  all.addEventListener('change', () => {
    for (const user of visibleUsers().filter(user => !busyUsers.has(user.public_id))) all.checked ? selected.add(user.public_id) : selected.delete(user.public_id);
    renderUsers();
  });
  body.addEventListener('change', event => {
    if (!event.target.matches('.user-check') || busyUsers.has(event.target.value)) return;
    event.target.checked ? selected.add(event.target.value) : selected.delete(event.target.value);
    syncSelection();
  });
  body.addEventListener('click', event => {
    const button = event.target.closest('button[data-action]');
    if (!button || button.disabled) return;
    const user = users.find(item => item.public_id === button.closest('tr').dataset.userId);
    if (!user || busyUsers.has(user.public_id)) return;
    if (button.dataset.action === 'password') {
      openUserEditor([user]);
    } else if (button.dataset.action === 'permissions') {
      openPermissionEditor([user]);
    } else void operate(button.dataset.action, [user]);
  });
  document.getElementById('user-bulk-actions').addEventListener('click', event => {
    const button = event.target.closest('[data-bulk]');
    if (button && !button.disabled) {
      if (button.dataset.bulk === 'password') openUserEditor(chosen(), true);
      else if (button.dataset.bulk === 'permissions') openPermissionEditor(chosen(), true);
      else void operate(button.dataset.bulk, chosen(), true);
    }
  });
  document.getElementById('user-bulk-state-action').addEventListener('change', event => {
    const action = event.target.value;
    if (!['enable', 'disable'].includes(action)) return;
    if (!event.target.disabled) void operate(action, chosen(), true);
  });
  globalThis.WolTables?.bind('user-table', () => {
    const url = new URL(location.href);
    for (const [key, value] of Object.entries(globalThis.WolTables.options('user-table'))) url.searchParams.set(key, value);
    history.replaceState({}, '', url);
    renderUsers();
  }, {
    key: 'username',
    direction: 'asc'
  });
  search.addEventListener('input', renderUsers);
  filter.addEventListener('change', () => {
    selected.clear();
    const url = new URL(location.href);
    if (filter.value === 'all') url.searchParams.delete('status');
    else url.searchParams.set('status', filter.value);
    history.replaceState({}, '', url);
    renderUsers();
  });
  setInterval(() => {
    if (!document.hidden) void loadUsers();
  }, 3000);
  window.addEventListener('focus', () => {
    void loadUsers();
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) void loadUsers();
  });
  void loadUsers();
  if (new URLSearchParams(location.search).get('create') === '1') openDialog('create-user-dialog');
});
