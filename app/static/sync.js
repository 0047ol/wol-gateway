/* Version snapshots contain configuration revisions only, never credentials. */
(() => {
  const versions = new Map();
  const valid = value => typeof value === 'string' && /^[0-9a-f]{32}$/.test(value);

  function remember(key, revision) {
    if (!valid(revision)) return;
    versions.set(key, revision);
    for (const dialog of document.querySelectorAll('dialog[open][data-sync-keys]')) {
      if (dialog._wolVersions?.[key] && dialog._wolVersions[key] !== revision) {
        let note = dialog.querySelector('.sync-notice');
        if (!note) {
          note = document.createElement('p');
          note.className = 'sync-notice';
          note.setAttribute('role', 'status');
          (dialog.querySelector('.dialog-heading') || dialog.firstElementChild).after(note);
        }
        note.textContent = t('其他页面已更新此记录。当前输入已保留，请关闭并重新打开后再保存。');
      }
    }
  }

  function seed() {
    for (const node of document.querySelectorAll('[data-sync-key][data-revision]'))
      if (!versions.has(node.dataset.syncKey)) remember(node.dataset.syncKey, node.dataset.revision);
  }

  function snapshot() {
    seed();
    const result = Object.fromEntries(versions);
    for (const dialog of document.querySelectorAll('dialog[open][data-sync-keys]')) Object.assign(result, dialog._wolVersions || {});
    return result;
  }

  function freeze(dialog) {
    const current = Object.fromEntries(versions);
    dialog._wolVersions = Object.fromEntries((dialog.dataset.syncKeys || '').split(' ').filter(Boolean).map(key => [key, current[key]]));
    if (dialog.dataset.revision && dialog.dataset.syncKeys?.startsWith('gateway:')) dialog._wolVersions[dialog.dataset.syncKeys.split(' ')[0]] = dialog.dataset.revision;
    dialog.querySelector('.sync-notice')?.remove();
  }

  function headers(url, body, frozen) {
    const values = frozen || snapshot(),
      keys = new Set();
    const gateway = url.pathname.match(/\/gateway\/(g_[a-f0-9]{32})(?:\/|$)/)?.[1];
    if (gateway) keys.add('gateway:' + gateway);
    const device = url.pathname.match(/\/devices\/(\d+)(?:\/|$)/)?.[1];
    if (device) keys.add('device:' + device);
    if (device && /\/share(?:\/|$)/.test(url.pathname)) keys.add('share:' + device);
    const user = url.pathname.match(/\/api\/users\/(u_[a-f0-9]{32})(?:\/|$)/)?.[1];
    if (user) keys.add('user:' + user);
    if (url.pathname === '/api/account') {
      const accountKey = document.getElementById('account-password-dialog')?.dataset.syncKeys;
      if (accountKey) keys.add(accountKey);
    }
    const pairing = url.pathname.match(/\/pairing\/(\d+)(?:\/|$)/)?.[1];
    if (pairing) keys.add('pairing:' + pairing);
    const batchOta = /^\/gateway\/batch_ota(?:\/latest)?$/.test(url.pathname);
    const ids = batchOta ? (url.searchParams.get('ids') || '').split(',') : typeof body?.get === 'function' ? String(body.get('ids') || '').split(',') : [];
    for (const id of ids.filter(Boolean)) {
      if (batchOta || ['/gateway/batch_unbind', '/gateway/batch_update', '/gateway/batch_assign'].includes(url.pathname)) keys.add(id.startsWith('p:') ? 'pairing:' + id.slice(2) : 'gateway:' + id.replace(/^g:/, ''));
      else if (url.pathname === '/api/users/bulk') keys.add('user:' + id);
      else if (gateway) keys.add('device:' + id);
    }
    return JSON.stringify(Object.fromEntries([...keys].filter(key => valid(values[key])).map(key => [key, values[key]])));
  }

  function observe(url, data) {
    if (!data || typeof data !== 'object') return;
    if (data.account) remember('user:' + data.account.public_id, data.account.revision);
    for (const row of Array.isArray(data.gateways) ? data.gateways : []) remember('gateway:' + row.id, row.revision);
    for (const row of Array.isArray(data.pairings) ? data.pairings : []) remember('pairing:' + row.id, row.revision);
    for (const row of Array.isArray(data.users) ? data.users : []) remember('user:' + row.public_id, row.revision);
    const gateway = url.pathname.match(/\/gateway\/(g_[a-f0-9]{32})(?:\/|$)/)?.[1];
    if (gateway) {
      if (data.gateway?.revision) remember('gateway:' + gateway, data.gateway.revision);
      if (url.pathname.endsWith('/availability')) remember('gateway:' + gateway, data.revision);
      for (const row of (Array.isArray(data) ? data : data.items || []))
        if (row.id && row.revision) remember('device:' + row.id, row.revision);
      const device = url.pathname.match(/\/devices\/(\d+)\/share(?:\/|$)/)?.[1];
      if (device && data.revision) remember('share:' + device, data.revision);
      if (device && data.device_revision) remember('device:' + device, data.device_revision);
      if (data.gateway_revision) remember('gateway:' + gateway, data.gateway_revision);
    }
  }
  window.WolSync = {
    remember,
    seed,
    snapshot,
    freeze,
    headers,
    observe
  };
  document.addEventListener('DOMContentLoaded', seed);
  document.addEventListener('wol:page-loaded', seed);
})();
