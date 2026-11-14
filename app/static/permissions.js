/* Capability UI mirrors server checks. It never grants access by itself. */
(() => {
  const source = document.getElementById('wol-access');
  if (!source) return;
  let access = JSON.parse(source.textContent);
  const can = key => Boolean(access.enabled && (access.role === 'admin' || key.split('|').some(value => access.permissions.includes(value))));
  const selectorRules = {
    account_edit: '#account-password-trigger,#account-password-form [type=submit]',
    gateway_add: '#gateway-add-trigger,#add-gateway [type=submit]',
    'gateway_edit|gateway_ota|gateway_ota_auto': '[onclick^="showEdit("]',
    gateway_edit: '.gateway-editor [type=submit],#edit-selected-gateways,.gateway-editor-body input:not([data-ota-file]),.gateway-owner-menu select',
    gateway_ota: '[data-ota-file],[data-ota-pick],[data-ota-upgrade],[data-ota-check],#ota-selected-gateways,#batch-ota-file,#batch-ota-pick,#batch-ota-submit,#batch-ota-check',
    gateway_ota_auto: '[data-ota-auto]',
    gateway_delete: '#delete-selected-gateways,[onclick^="deleteGateway("],[onclick^="deletePairing("],[onclick^="cancelGatewayDelete("],#gateway-delete-queue,#gateway-delete-force',
    pairing_renew: '[onclick^="renewPairing("],[onclick^="renewGatewayPairing("]',
    gateway_transfer: '[onclick^="transferGateway("]',
    gateway_logs_clear: '[onclick^="clearLogs("]',
    device_add: '#device-add-trigger,#manual-add-submit,#manual-device-form [type=submit],#scan-add',
    device_scan: '#device-scan-trigger',
    device_edit: '[onclick^="editDevice("],#device-editor-save',
    device_power: '[onclick^="toggleDevicePower("],#power-selected-devices,#power-shutdown',
    device_share: '[onclick^="openDeviceShare("],#share-selected-devices,#share-manager-save,#share-delete,#share-password-reset',
    device_service: '#device-editor-unlink,#unlink-selected-devices,#power-link,#power-install,[onclick^="unlinkWindowsService("]',
    device_delete: '[onclick^="deleteDevice("],#delete-selected-devices'
  };
  const blocked = new Map();
  let queued = false;

  function permissionFor(node) {
    if (node.dataset.permission) return node.dataset.permission;
    if (node.matches('[id^="device-power-"]')) return ['link', 'setup'].includes(node.dataset.action) ? 'device_service' : 'device_power';
    for (const [key, selector] of Object.entries(selectorRules))
      if (node.matches(selector)) return key;
    const href = node.getAttribute('href') || '';
    if (/^\/gateway\/[^/]+\/devices(?:[?#]|$)/.test(href)) return 'device_view';
    if (/^\/gateway\/[^/]+\/logs(?:[?#]|$)/.test(href)) return 'gateway_logs_view';
    return '';
  }

  function apply() {
    queued = false;
    for (const node of document.querySelectorAll('button,a[href],input,select')) {
      const key = permissionFor(node),
        denied = key && !can(key);
      if (denied) {
        if (!blocked.has(node)) blocked.set(node, {
          disabled: node.disabled,
          title: node.title || node.dataset.tooltip || '',
          tabindex: node.getAttribute('tabindex')
        });
        if ('disabled' in node && !node.disabled) node.disabled = true;
        if (node.getAttribute('aria-disabled') !== 'true') node.setAttribute('aria-disabled', 'true');
        if (node.tagName === 'A' && node.tabIndex !== -1) node.tabIndex = -1;
        const note = typeof t === 'function' ? t('没有使用此功能的权限，请联系管理员') : '没有使用此功能的权限，请联系管理员';
        if ((node.title || node.dataset.tooltip) !== note) node.title = note;
      } else if (blocked.has(node)) {
        const old = blocked.get(node);
        blocked.delete(node);
        if ('disabled' in node) node.disabled = Boolean(old.disabled);
        node.removeAttribute('aria-disabled');
        node.title = old.title;
        if (old.tabindex === null) node.removeAttribute('tabindex');
        else node.setAttribute('tabindex', old.tabindex);
      }
    }
    for (const node of blocked.keys())
      if (!node.isConnected) blocked.delete(node);
    const account = document.getElementById('account-password-trigger');
    if (account) account.hidden = !can('account_edit');
    const download = document.getElementById('downloads-trigger');
    if (download) download.hidden = !access.enabled;
    const card = document.getElementById('gateway-list-card');
    if (card) card.hidden = !can('gateway_view');
    const denied = document.getElementById('gateway-access-note');
    if (denied) denied.hidden = can('gateway_view');
  }
  const schedule = () => {
    if (!queued) {
      queued = true;
      queueMicrotask(apply);
    }
  };
  new MutationObserver(schedule).observe(document.body, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ['disabled', 'onclick', 'href', 'data-permission']
  });
  document.addEventListener('click', event => {
    const node = event.target.closest('button,a,input[data-permission]');
    const key = node && (permissionFor(node) || (node.closest('a[href]') && permissionFor(node.closest('a[href]'))));
    if (key && !can(key)) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);
  const pagePermission = () => /\/gateway\/[^/]+\/scan$/.test(location.pathname) ? 'device_scan' : /\/gateway\/[^/]+\/devices$/.test(location.pathname) ? 'device_view' : /\/gateway\/[^/]+\/logs$/.test(location.pathname) ? 'gateway_logs_view' : '';

  function update(value) {
    const changed = JSON.stringify(access.permissions) !== JSON.stringify(value.permissions) || access.enabled !== value.enabled;
    access = value;
    apply();
    if (changed) {
      window.WolPage?.invalidate();
      const key = pagePermission();
      if (key && !can(key)) void window.WolPage?.navigate('/', {
        fresh: true
      });
      document.dispatchEvent(new CustomEvent('wol:permissions-changed'));
      for (const dialog of document.querySelectorAll('dialog[open]')) {
        if (dialog.classList.contains('gateway-editor') && dialog.querySelector('[data-ota-gateway]')) {
          if (!can('gateway_edit|gateway_ota|gateway_ota_auto')) window.closeDialog?.(dialog.id);
          continue;
        }
        if ([...dialog.querySelectorAll('[type=submit],button')].some(node => {
            const key = permissionFor(node);
            return key && !can(key);
          })) window.closeDialog?.(dialog.id);
      }
    }
  }
  window.WolPermissions = {
    can,
    apply,
    update
  };
  document.addEventListener('wol:page-loaded', apply);
  document.addEventListener('DOMContentLoaded', apply);
  apply();
})();
