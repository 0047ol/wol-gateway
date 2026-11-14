/* page-scope:start */
WolPage.mount(({
  window,
  globalThis,
  document,
  fetch,
  history,
  setInterval,
  clearInterval,
  setTimeout,
  clearTimeout,
  IntersectionObserver,
  showToast,
  postForm,
  uiConfirm,
  uiAlert,
  expose
}) => {
  const applyAnimatedHidden = (node, hidden) => {
    if (!node) return;
    if (typeof window.setAnimatedHidden === 'function') window.setAnimatedHidden(node, hidden);
    else node.hidden = Boolean(hidden);
  };
  let gatewayBulkBusy = false;
  let gatewayBulkIds = [];

  function bulkGatewayIds(edit = false) {
    const selected = [...document.querySelectorAll('.gateway-check:checked')];
    if (!selected.length || selected.length > 64 || selected.some(box => {
        if (box.disabled || !box.value.startsWith('g:')) return true;
        const badge = document.getElementById('status-' + box.value.slice(2));
        return badge?.dataset.deletePending === 'true' || (edit && !['online', 'offline', 'long_offline'].some(state => badge?.classList.contains(state)));
      })) return [];
    return selected.map(box => box.value);
  }

  function bulkGatewaySelectionMatches(ids, edit = false) {
    const current = bulkGatewayIds(edit);
    return ids.length === current.length && current.every(id => ids.includes(id));
  }

  function gatewayTargets(ids) {
    return ids.map(id => describeTargetRow(document.getElementById('gateway-name-' + id.slice(2))));
  }

  function editSelectedGateways() {
    const ids = bulkGatewayIds(true);
    if (gatewayBulkBusy || !ids.length || (window.WolPermissions && !window.WolPermissions.can('gateway_edit'))) return;
    gatewayBulkIds = ids;
    const dialog = document.getElementById('gateway-bulk-editor');
    dialog.dataset.syncKeys = ids.map(id => 'gateway:' + id.slice(2)).join(' ');
    renderConfirmationTargets(document.getElementById('gateway-bulk-targets'), gatewayTargets(ids));
    dialog.querySelector('form').reset();
    const prefixes = {
      ping_interval: 'ep',
      heartbeat_send_interval: 'ehs',
      heartbeat_timeout: 'eht',
      scan_concurrency: 'escc'
    };
    for (const [key, prefix] of Object.entries(prefixes)) {
      const input = document.getElementById('bulk-config-' + key);
      const values = ids.map(id => document.getElementById(prefix + '-' + id.slice(2))?.value);
      input.value = values.every(value => value === values[0]) ? values[0] : '';
      input.placeholder = t('保持各自配置');
      input.disabled = true;
      input.required = true;
    }
    document.getElementById('gateway-bulk-save').disabled = true;
    openDialog('gateway-bulk-editor');
  }

  document.getElementById('gateway-bulk-form')?.addEventListener('change', event => {
    const key = event.target.dataset.bulkConfig;
    if (key) document.getElementById('bulk-config-' + key).disabled = !event.target.checked;
    document.getElementById('gateway-bulk-save').disabled = gatewayBulkBusy || !document.querySelector('#gateway-bulk-form [data-bulk-config]:checked');
  });
  document.getElementById('gateway-bulk-form')?.addEventListener('submit', async event => {
    event.preventDefault();
    const ids = [...gatewayBulkIds];
    if (gatewayBulkBusy || !bulkGatewaySelectionMatches(ids, true)) return;
    const config = Object.fromEntries([...event.target.querySelectorAll('[data-bulk-config]:checked')].map(box => [box.dataset.bulkConfig, Number(document.getElementById('bulk-config-' + box.dataset.bulkConfig).value)]));
    if (!Object.keys(config).length) return;
    const versions = window.WolSync.snapshot();
    gatewayBulkBusy = true;
    updateGatewaySelection();
    document.getElementById('gateway-bulk-save').disabled = true;
    try {
      if (!await uiConfirm('将为所选网关应用勾选的配置，其他项目保持不变。', {
          title: '批量编辑网关',
          confirmText: '保存并下发',
          targets: gatewayTargets(ids)
        })) return;
      if (!bulkGatewaySelectionMatches(ids, true)) return;
      const result = await postForm('/gateway/batch_update', {
        ids: ids.join(','),
        config: JSON.stringify(config)
      }, {
        wolVersions: versions
      });
      if (result?.ok) closeDialog('gateway-bulk-editor');
    } finally {
      gatewayBulkBusy = false;
      document.getElementById('gateway-bulk-save').disabled = !document.querySelector('#gateway-bulk-form [data-bulk-config]:checked');
      updateGatewaySelection();
    }
  });
  let ownerAssignment = null;

  function openGatewayOwner(id = null) {
    if (gatewayBulkBusy) return;
    const ids = id ? ['g:' + id] : bulkGatewayIds();
    if (!ids.length) return;
    const dialog = document.getElementById('gateway-owner-dialog');
    if (!dialog) return;
    const current = id ? document.querySelector('#edit-' + id + ' [data-owner-id]')?.dataset.ownerId : '';
    ownerAssignment = {
      ids,
      id,
      versions: window.WolSync.snapshot()
    };
    dialog.dataset.syncKeys = ids.map(value => 'gateway:' + value.slice(2)).join(' ');
    document.getElementById('gateway-owner-title').textContent = t(id ? '更改归属' : '批量移交归属');
    renderConfirmationTargets(document.getElementById('gateway-owner-targets'), id ? [] : gatewayTargets(ids));
    const select = document.getElementById('gateway-bulk-owner-select');
    select.value = [...select.options].some(option => option.value === current) ? current : '';
    select.dataset.currentOwner = current || '';
    select.disabled = false;
    select.dispatchEvent(new Event('change', {
      bubbles: true
    }));
    openDialog('gateway-owner-dialog');
  }

  function updateOwnerSubmit() {
    const select = document.getElementById('gateway-bulk-owner-select');
    const button = document.getElementById('gateway-owner-submit');
    if (button) button.disabled = gatewayBulkBusy || !ownerAssignment || !select.value || select.selectedOptions[0]?.disabled || Boolean(ownerAssignment.id && select.value === select.dataset.currentOwner);
  }
  document.getElementById('gateway-bulk-owner-select')?.addEventListener('change', updateOwnerSubmit);

  async function submitGatewayOwner() {
    const selection = ownerAssignment;
    const select = document.getElementById('gateway-bulk-owner-select');
    if (!selection || gatewayBulkBusy || document.getElementById('gateway-owner-submit').disabled) return;
    const {
      ids,
      id,
      versions
    } = selection;
    const owner = select.value;
    gatewayBulkBusy = true;
    select.disabled = true;
    updateGatewaySelection();
    updateOwnerSubmit();
    try {
      if (!await uiConfirm(id ? '移交此网关及其设备？现有分享链接将停用，未领取的配对码将失效。' : '移交所选网关及其设备？现有分享链接将停用，未领取的配对码将失效。', {
          title: id ? '更改归属' : '批量移交归属',
          confirmText: '移交',
          danger: true,
          countdown: 3,
          targets: id ? [] : gatewayTargets(ids)
        })) return;
      if (selection !== ownerAssignment || !document.getElementById('gateway-owner-dialog').open || (!id && !bulkGatewaySelectionMatches(ids))) return;
      const result = await postForm(id ? '/gateway/' + id + '/assign' : '/gateway/batch_assign', {
        ...(id ? {} : {
          ids: ids.join(',')
        }),
        owner
      }, {
        wolVersions: versions
      });
      if (result?.ok) {
        closeDialog('gateway-owner-dialog');
        if (id) closeDialog('edit-' + id);
        void refreshGatewayList(activeGatewayPage, false, false);
      }
    } finally {
      gatewayBulkBusy = false;
      select.disabled = false;
      updateGatewaySelection();
      updateOwnerSubmit();
    }
  }

  function updateGatewaySelection() {
    const boxes = [...document.querySelectorAll('.gateway-check')].filter(box => !box.disabled);
    const count = boxes.filter(box => box.checked).length;
    const selection = document.getElementById('gateway-selection-count');
    applyAnimatedHidden(selection, !count);
    if (selection) selection.textContent = `已选择 ${count} 项`;
    window.setListSelection?.(document.querySelector('.list-toolbar .toolbar-bulk'), count > 0);
    const button = document.getElementById('delete-selected-gateways');
    const available = Boolean(gatewayBatchState());
    applyAnimatedHidden(button, !available);
    if (button) button.disabled = !available;
    const edit = document.getElementById('edit-selected-gateways');
    const editable = Boolean(bulkGatewayIds(true).length);
    applyAnimatedHidden(edit, !editable);
    if (edit) edit.disabled = !editable || gatewayBulkBusy;
    const otaButton = document.getElementById('ota-selected-gateways');
    const otaSelectable = Boolean(count && count <= 64 && [...document.querySelectorAll('.gateway-check:checked')].every(box => box.value.startsWith('g:')));
    applyAnimatedHidden(otaButton, !otaSelectable);
    if (otaButton) otaButton.disabled = !otaSelectable || gatewayBulkBusy || Boolean(window.WolPermissions && !window.WolPermissions.can('gateway_ota'));
    const ownerButton = document.getElementById('assign-selected-gateways');
    if (ownerButton) {
      const assignable = Boolean(bulkGatewayIds().length);
      applyAnimatedHidden(ownerButton, !assignable);
      ownerButton.disabled = !assignable || gatewayBulkBusy;
    }
    const all = document.getElementById('select-all-gateways');
    if (all) {
      all.checked = boxes.length > 0 && count === boxes.length;
      all.indeterminate = count > 0 && count < boxes.length;
    }
  }

  function selectAllGateways(checked) {
    document.querySelectorAll('.gateway-check').forEach(box => {
      box.checked = checked && !box.disabled;
    });
    updateGatewaySelection();
  }

  function gatewaySelectionState(value) {
    const id = value.split(':')[1];
    if (value.startsWith('p:')) {
      const badge = document.getElementById('pair-state-' + id);
      if (!badge?.dataset.expiresAt) return '';
      return 'pairing:' + (Number(badge.dataset.expiresAt) * 1000 <= Date.now() ? 'expired' : 'pending');
    }
    const badge = document.getElementById('status-' + id);
    if (badge?.dataset.deletePending === 'true') return '';
    const state = ['online', 'offline', 'long_offline', 'unbound', 'error', 'bind_failed', 'deleting', 'reset_failed', 'pairing_expired', 'pairing_error'].find(s => badge?.classList?.contains(s));
    return state ? 'gateway:' + state + ':' + (badge.dataset.connectionState || state) : '';
  }

  function gatewayBatchState() {
    const selected = [...document.querySelectorAll('.gateway-check:checked')];
    const states = selected.map(box => [box.value, gatewaySelectionState(box.value)]);
    return states.length && states.every(([, state]) => state) ? JSON.stringify(Object.fromEntries(states.sort(([a], [b]) => a.localeCompare(b)))) : '';
  }

  function gatewaySelectionMatches(ids, state) {
    const selected = [...document.querySelectorAll('.gateway-check:checked')].map(box => box.value);
    return gatewayBatchState() === state && selected.length === ids.length && selected.every(id => ids.includes(id));
  }

  function gatewayDeleteTitle(id, pairing = false) {
    const name = document.getElementById((pairing ? 'pairing-name-' : 'gateway-name-') + id)?.textContent.trim() || t('未命名网关');
    return t('删除{名称}').replace('{名称}', name);
  }

  function showEdit(id) {
    if (document.getElementById('status-' + id)?.dataset.deletePending === 'true') return;
    const dialog = document.getElementById('edit-' + id);
    dialog.querySelector('form').reset();
    openDialog('edit-' + id);
    const section = dialog.querySelector('[data-ota-gateway]');
    if (section) {
      otaPublished.delete(id);
      section.querySelector('[data-ota-latest]').hidden = true;
      section.querySelector('[data-ota-filename]').textContent = '';
      renderOta(section, JSON.parse(section.dataset.ota));
    }
    void refreshOpenOta();
  }

  let otaStatusReading = false;
  const otaUploads = new Set();
  const otaAutoSaving = new Set();
  const otaReadGeneration = new Map();
  const otaResults = new Map();
  const otaChecking = new Set();
  const otaPublished = new Map();
  let batchOtaIds = [];
  let batchOtaBusy = false;
  let batchOtaReading = false;
  let batchOtaEntries = [];
  let batchOtaTargets = [];
  let batchOtaRelease = null;

  function openBatchOta() {
    if (batchOtaBusy || (window.WolPermissions && !window.WolPermissions.can('gateway_ota'))) return;
    const selected = [...document.querySelectorAll('.gateway-check:checked')];
    if (!selected.length || selected.length > 64 || selected.some(box => box.disabled || !box.value.startsWith('g:'))) return;
    batchOtaIds = selected.map(box => box.value.slice(2));
    batchOtaRelease = null;
    batchOtaTargets = batchOtaIds.map(id => {
      const section = document.querySelector('[data-ota-gateway="' + id + '"]');
      const state = section ? JSON.parse(section.dataset.ota) : {};
      return {
        id,
        name: describeTargetRow(document.getElementById('gateway-name-' + id)),
        version: state.running_version || '',
        selected: true
      };
    });
    batchOtaEntries = batchOtaTargets.map(entry => ({
      ...entry
    }));
    const dialog = document.getElementById('gateway-batch-ota');
    dialog.dataset.syncKeys = batchOtaIds.map(id => 'gateway:' + id).join(' ');
    document.getElementById('batch-ota-file').value = '';
    document.getElementById('batch-ota-filename').textContent = '';
    document.getElementById('batch-ota-status').textContent = '';
    renderBatchOta();
    openDialog('gateway-batch-ota');
  }

  function renderBatchOta() {
    const allowed = !window.WolPermissions || window.WolPermissions.can('gateway_ota');
    const file = document.getElementById('batch-ota-file');
    if (!file) return;
    const filename = document.getElementById('batch-ota-filename');
    const name = file.files?.[0]?.name || '';
    filename.textContent = name;
    filename.title = name;
    filename.hidden = !name;
    document.getElementById('batch-ota-empty').hidden = Boolean(name);
    file.disabled = batchOtaBusy || !allowed;
    document.getElementById('batch-ota-pick').disabled = batchOtaBusy || !allowed;
    document.getElementById('batch-ota-check').disabled = batchOtaBusy || !allowed;
    document.getElementById('batch-ota-submit').disabled = batchOtaBusy || !allowed || (!file.files?.length && !batchOtaRelease?.entries.some(entry => entry.eligible));
    document.getElementById('batch-ota-submit').textContent = t(batchOtaRelease ? '开始升级' : '检查并升级');
    document.getElementById('batch-ota-empty').textContent = batchOtaRelease ? t('本站最新版本') + ': v' + batchOtaRelease.version : t('尚未选择固件包');
    const list = document.getElementById('batch-ota-results');
    list.hidden = !batchOtaEntries.length;
    list.replaceChildren(...batchOtaEntries.map(entry => {
      const item = document.createElement('li');
      const name = document.createElement('strong');
      name.textContent = entry.name || entry.mac;
      const status = document.createElement('span');
      const state = entry.ota;
      const version = state?.running_version || entry.version;
      status.textContent = [t('当前版本') + ': ' + (version ? 'v' + version : t('未知')), !entry.selected && t(entry.reason || state?.message || (entry.eligible ? '可升级' : '已跳过')), state?.busy ? state.progress + '%' : ''].filter(Boolean).join(' · ');
      item.append(name, document.createTextNode(' · '), status);
      return item;
    }));
  }

  function selectBatchOtaFile() {
    if (!document.getElementById('batch-ota-file').files?.length && batchOtaRelease) {
      renderBatchOta();
      return;
    }
    if (document.getElementById('batch-ota-file').files?.length) batchOtaRelease = null;
    document.getElementById('batch-ota-status').textContent = '';
    batchOtaEntries = batchOtaTargets.map(entry => ({
      ...entry
    }));
    renderBatchOta();
  }

  async function checkBatchOtaUpdate() {
    const dialog = document.getElementById('gateway-batch-ota');
    if (batchOtaBusy || !dialog?.open || (window.WolPermissions && !window.WolPermissions.can('gateway_ota'))) return;
    const ids = [...batchOtaIds];
    const note = document.getElementById('batch-ota-status');
    batchOtaBusy = true;
    renderBatchOta();
    note.textContent = t('正在检查更新…');
    try {
      const response = await fetch('/gateway/batch_ota/check?ids=' + ids.join(','), {
        cache: 'no-store',
        signal: AbortSignal.timeout(15000)
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.detail || '操作失败');
      if (!dialog.isConnected || !dialog.open || ids.join(',') !== batchOtaIds.join(',')) return;
      batchOtaRelease = result;
      batchOtaEntries = result.entries;
      document.getElementById('batch-ota-file').value = '';
      note.textContent = t('本站最新版本') + ': v' + result.version + ' · ' + t('可升级') + ': ' + result.entries.filter(entry => entry.eligible).length + '/' + ids.length;
      showToast('检查更新完成', {
        success: true
      });
    } catch (error) {
      if (dialog.isConnected && dialog.open) {
        note.textContent = t(error.message || '请求失败，请稍后重试');
        showToast(note.textContent, {
          error: true
        });
      }
    } finally {
      batchOtaBusy = false;
      renderBatchOta();
    }
  }

  async function checkOtaUpdate(id) {
    const section = document.querySelector('[data-ota-gateway="' + id + '"]');
    if (!section?.closest('dialog')?.open || otaChecking.has(id) || otaUploads.has(id) || (window.WolPermissions && !window.WolPermissions.can('gateway_ota'))) return;
    const versions = window.WolSync.snapshot();
    const note = section.querySelector('[data-ota-latest]');
    otaChecking.add(id);
    otaPublished.delete(id);
    note.hidden = false;
    note.textContent = t('正在检查更新…');
    renderOta(section, JSON.parse(section.dataset.ota));
    try {
      const response = await fetch('/gateway/' + id + '/ota/check', {
        cache: 'no-store',
        signal: AbortSignal.timeout(15000)
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.detail || '操作失败');
      if (!section.isConnected || !section.closest('dialog')?.open) return;
      otaPublished.set(id, {
        ...result,
        versions
      });
      section.querySelector('[data-ota-file]').value = '';
      section.querySelector('[data-ota-filename]').textContent = '';
      const entry = result.entries[0];
      note.textContent = t('本站最新版本') + ': v' + result.version + ' · ' + t(entry.reason || '可升级');
      showToast('检查更新完成', {
        success: true
      });
    } catch (error) {
      if (section.isConnected && section.closest('dialog')?.open) {
        note.textContent = t(error.message || '请求失败，请稍后重试');
        showToast(note.textContent, {
          error: true
        });
      }
    } finally {
      otaChecking.delete(id);
      if (section.isConnected) renderOta(section, JSON.parse(section.dataset.ota));
    }
  }

  async function refreshBatchOta() {
    const dialog = document.getElementById('gateway-batch-ota');
    const entries = batchOtaEntries.filter(entry => entry.started && entry.ota?.busy);
    if (!dialog?.open || batchOtaReading || batchOtaBusy || !entries.length || (window.WolPermissions && !window.WolPermissions.can('gateway_ota'))) return;
    const expected = batchOtaEntries;
    batchOtaReading = true;
    try {
      const response = await fetch('/gateway/batch_ota/status?ids=' + entries.map(entry => entry.id).join(','), {
        cache: 'no-store',
        signal: AbortSignal.timeout(10000)
      });
      const result = await response.json();
      if (!response.ok || expected !== batchOtaEntries) return;
      for (const current of result.entries) {
        const entry = batchOtaEntries.find(value => value.id === current.id);
        if (entry) {
          entry.ota = current.ota;
          const section = document.querySelector('[data-ota-gateway="' + current.id + '"]');
          if (section) renderOta(section, current.ota);
        }
      }
      renderBatchOta();
    } catch (_) {
      if (dialog.open) document.getElementById('batch-ota-status').textContent = t('无法读取升级状态，请稍后重试');
    } finally {
      batchOtaReading = false;
    }
  }

  async function uploadBatchOta() {
    if (batchOtaBusy || (window.WolPermissions && !window.WolPermissions.can('gateway_ota'))) return;
    const file = document.getElementById('batch-ota-file').files?.[0];
    const published = !file && batchOtaRelease;
    if (!published && (!file || !/\.zip$/i.test(file.name) || file.size > 3 * 1024 * 1024)) {
      showToast(file?.size > 3 * 1024 * 1024 ? '升级包过大，最多支持 3 MiB' : '请选择固件 ZIP 包', {
        error: true
      });
      return;
    }
    const ids = [...batchOtaIds];
    const versions = window.WolSync.snapshot();
    const dialog = document.getElementById('gateway-batch-ota');
    const note = document.getElementById('batch-ota-status');
    const url = '/gateway/batch_ota' + (published ? '/latest' : '') + '?ids=' + ids.join(',');
    batchOtaBusy = true;
    gatewayBulkBusy = true;
    renderBatchOta();
    updateGatewaySelection();
    note.textContent = t('正在检查固件和网关版本…');
    try {
      const request = (path, plan) => fetch(path, {
        method: 'POST',
        headers: {
          ...(published ? {} : {
            'Content-Type': 'application/zip'
          }),
          ...(plan ? {
            'X-WOL-OTA-Plan': plan
          } : {})
        },
        body: published ? undefined : file,
        wolVersions: versions,
        signal: AbortSignal.timeout(60000)
      });
      const response = await request(url + '&preview=true');
      const plan = await response.json();
      if (!response.ok || !plan.ok) throw new Error(plan.detail || '操作失败');
      batchOtaEntries = plan.entries;
      renderBatchOta();
      note.textContent = t('目标版本') + ': v' + plan.version;
      const eligible = plan.entries.filter(entry => entry.eligible);
      if (!eligible.length) {
        note.textContent = t('没有需要升级的网关，请查看跳过原因。');
        return;
      }
      const target = entry => [entry.name || entry.mac, t('当前版本') + ': ' + (entry.version ? 'v' + entry.version : t('未知')), t(entry.reason || '可升级')].filter(Boolean).join(' · ');
      if (!await uiConfirm('仅升级可用名单中的网关。升级期间请保持供电，各网关独立显示结果。', {
          title: '批量 OTA 升级',
          confirmText: '开始升级',
          countdown: 3,
          targetGroups: [{
            title: '可升级',
            targets: eligible.map(target)
          }, {
            title: '已跳过',
            targets: plan.entries.filter(entry => !entry.eligible).map(target)
          }].filter(group => group.targets.length)
        })) return;
      if (!dialog.isConnected || !dialog.open || JSON.stringify(ids) !== JSON.stringify(batchOtaIds) || (window.WolPermissions && !window.WolPermissions.can('gateway_ota'))) return;
      note.textContent = t('正在提交升级任务…');
      const submitted = await request(url, plan.plan);
      const result = await submitted.json();
      if (!submitted.ok || !result.ok) throw new Error(result.detail || '操作失败');
      batchOtaEntries = result.entries;
      batchOtaRelease = null;
      document.getElementById('batch-ota-file').value = '';
      note.textContent = t('已提交升级任务') + ': ' + result.started + '/' + ids.length;
      showToast(note.textContent, {
        success: result.started > 0
      });
    } catch (error) {
      note.textContent = t(error.message || '请求失败，请稍后重试');
      showToast(note.textContent, {
        error: true
      });
    } finally {
      batchOtaBusy = false;
      gatewayBulkBusy = false;
      renderBatchOta();
      updateGatewaySelection();
    }
  }

  function renderOta(section, state) {
    if (!section || !state) return;
    section.dataset.ota = JSON.stringify(state);
    const id = section.dataset.otaGateway;
    const denied = window.WolPermissions && !window.WolPermissions.can('gateway_ota');
    const uploading = otaUploads.has(id);
    const checking = otaChecking.has(id);
    const automatic = state.auto_update;
    const toggle = section.querySelector('[data-ota-auto]');
    if (toggle && automatic) {
      toggle.setAttribute('aria-checked', String(automatic.enabled));
      toggle.disabled = otaAutoSaving.has(id) || (!automatic.enabled && !automatic.can_enable) || Boolean(window.WolPermissions && !window.WolPermissions.can('gateway_ota_auto'));
      toggle.title = !automatic.enabled && !automatic.can_enable ? t('仅状态正常且支持 OTA 的网关可开启自动更新') : '';
      const autoNote = section.querySelector('[data-ota-auto-status]');
      autoNote.textContent = t(automatic.message || '');
      autoNote.hidden = !automatic.message;
    }
    const blocked = denied || !state.can_upgrade || uploading || checking;
    const file = section.querySelector('[data-ota-file]');
    file.disabled = Boolean(blocked);
    section.querySelector('[data-ota-pick]').disabled = Boolean(blocked);
    section.querySelector('[data-ota-check]').disabled = Boolean(denied || uploading || checking || state.busy);
    const button = section.querySelector('[data-ota-upgrade]');
    button.disabled = Boolean(blocked || (!file.files?.length && !otaPublished.get(id)?.entries[0].eligible));
    button.title = denied ? t('没有使用此功能的权限，请联系管理员') : state.disabled_reason || '';
    const progress = section.querySelector('[data-ota-progress]');
    progress.hidden = !state.busy && !uploading;
    if (uploading) progress.removeAttribute('value');
    else progress.value = state.progress || 0;
    const message = section.querySelector('[data-ota-status]');
    section.querySelector('[data-ota-version]').textContent = state.running_version ? 'v' + state.running_version : t('未知');
    section.querySelector('[data-ota-updated]').textContent = state.last_updated_at > 0 ? new Intl.DateTimeFormat('sv-SE', {
      timeZone: 'Asia/Shanghai',
      dateStyle: 'short',
      timeStyle: 'medium'
    }).format(new Date(state.last_updated_at * 1000)) : t('暂无记录');
    const failed = ['failed', 'rolled_back'].includes(state.state);
    message.textContent = uploading ? t('正在上传固件…') : [t((state.busy || failed ? state.message : '') || state.disabled_reason || ''), state.busy ? (state.progress || 0) + '%' : ''].filter(Boolean).join(' · ');
    message.hidden = !message.textContent;
    message.classList.toggle('ota-error', ['failed', 'rolled_back'].includes(state.state));
    const previous = otaResults.get(id);
    if (previous && previous.task_id === state.task_id && previous.busy && !state.busy && ['success', 'failed', 'rolled_back'].includes(state.state)) {
      showToast(state.message, {
        success: state.state === 'success',
        error: state.state !== 'success'
      });
    }
    otaResults.set(id, state);
  }

  async function refreshOpenOta() {
    void refreshBatchOta();
    const section = document.querySelector('.gateway-editor[open] [data-ota-gateway]');
    if (!section || otaStatusReading || otaAutoSaving.has(section.dataset.otaGateway) || (window.WolPermissions && !window.WolPermissions.can('gateway_edit|gateway_ota|gateway_ota_auto'))) return;
    otaStatusReading = true;
    const generation = otaReadGeneration.get(section.dataset.otaGateway) || 0;
    try {
      const response = await fetch('/gateway/' + section.dataset.otaGateway + '/ota', {
        cache: 'no-store',
        signal: AbortSignal.timeout(10000)
      });
      const result = await response.json();
      if (response.ok && section.isConnected && !otaAutoSaving.has(section.dataset.otaGateway) && generation === (otaReadGeneration.get(section.dataset.otaGateway) || 0)) renderOta(section, result.ota);
    } catch (error) {
      console.warn('OTA status read failed', error);
    } finally {
      otaStatusReading = false;
    }
  }

  function pickOtaFile(id) {
    document.getElementById('ota-file-' + id)?.click();
  }

  async function setAutoUpdate(id) {
    const section = document.querySelector('[data-ota-gateway="' + id + '"]');
    if (!section || otaAutoSaving.has(id) || (window.WolPermissions && !window.WolPermissions.can('gateway_ota_auto'))) return;
    const toggle = section.querySelector('[data-ota-auto]');
    if (!toggle || toggle.disabled) return;
    const state = JSON.parse(section.dataset.ota);
    if (!state.auto_update) return;
    const dialog = section.closest('dialog');
    const enabled = !state.auto_update.enabled;
    otaReadGeneration.set(id, (otaReadGeneration.get(id) || 0) + 1);
    otaAutoSaving.add(id);
    renderOta(section, state);
    try {
      const response = await fetch('/gateway/' + id + '/ota/auto', {
        method: 'POST',
        body: new URLSearchParams({
          enabled: String(enabled)
        }),
        signal: AbortSignal.timeout(15000)
      });
      const result = await response.json();
      showToast(result.msg || result.detail || '操作失败', {
        success: response.ok && result.ok,
        error: !response.ok || !result.ok
      });
      if (response.ok && result?.ok && section.isConnected) {
        const key = 'gateway:' + id;
        if (dialog?._wolVersions && result.revision) dialog._wolVersions[key] = result.revision;
        if (dialog && result.revision) dialog.dataset.revision = result.revision;
        if (result.revision) window.WolSync?.remember(key, result.revision);
        section.dataset.ota = JSON.stringify(result.ota);
        void refreshGatewayList(activeGatewayPage, false, false);
      }
    } catch (error) {
      showToast('请求失败，请稍后重试', {
        error: true
      });
    } finally {
      otaAutoSaving.delete(id);
      if (section.isConnected) renderOta(section, JSON.parse(section.dataset.ota));
    }
  }

  function selectOtaFile(id) {
    const section = document.querySelector('[data-ota-gateway="' + id + '"]');
    if (!section) return;
    const file = section.querySelector('[data-ota-file]').files?.[0];
    if (file) {
      otaPublished.delete(id);
      section.querySelector('[data-ota-latest]').hidden = true;
    }
    section.querySelector('[data-ota-filename]').textContent = file?.name || '';
    renderOta(section, JSON.parse(section.dataset.ota));
  }

  async function uploadOta(id) {
    const section = document.querySelector('[data-ota-gateway="' + id + '"]');
    if (!section || otaUploads.has(id) || (window.WolPermissions && !window.WolPermissions.can('gateway_ota'))) return;
    const file = section.querySelector('[data-ota-file]').files?.[0];
    const published = !file && otaPublished.get(id);
    const state = JSON.parse(section.dataset.ota);
    if (!published && (!file || !/\.zip$/i.test(file.name))) {
      showToast('请选择固件 ZIP 包', {
        error: true
      });
      return;
    }
    if (file && file.size > 3 * 1024 * 1024) {
      showToast('升级包过大，最多支持 3 MiB', {
        error: true
      });
      return;
    }
    if (!state.can_upgrade) {
      showToast(state.disabled_reason, {
        error: true
      });
      return;
    }
    const versions = published ? published.versions : window.WolSync.snapshot();
    const confirmation = published ? t('将安装本站最新固件，网关将短暂重启，请保持供电。') + '\n' + t('目标版本') + ': v' + published.version : '上传并升级此网关固件？升级期间网关将短暂重启，请保持供电。';
    if (!await uiConfirm(confirmation, {
        confirmText: '开始升级',
        title: '远程 OTA 升级'
      })) return;
    if (!section.isConnected || !section.closest('dialog')?.open || (window.WolPermissions && !window.WolPermissions.can('gateway_ota'))) return;
    otaUploads.add(id);
    renderOta(section, state);
    try {
      const response = await fetch('/gateway/' + id + '/ota' + (published ? '/latest' : ''), {
        method: 'POST',
        headers: {
          ...(published ? {
            'X-WOL-OTA-Plan': published.plan
          } : {
            'Content-Type': 'application/zip'
          })
        },
        body: published ? undefined : file,
        wolVersions: versions,
        signal: AbortSignal.timeout(60000)
      });
      const result = await response.json();
      showToast(result.msg || result.detail || '操作失败', {
        success: response.ok && result.ok,
        error: !response.ok || !result.ok
      });
      if (result.ota && section.isConnected) {
        otaPublished.delete(id);
        section.querySelector('[data-ota-latest]').hidden = true;
        section.querySelector('[data-ota-file]').value = '';
        section.querySelector('[data-ota-filename]').textContent = '';
        section.dataset.ota = JSON.stringify(result.ota);
      }
    } catch (error) {
      showToast('请求失败，请稍后重试', {
        error: true
      });
    } finally {
      otaUploads.delete(id);
      if (section.isConnected) renderOta(section, JSON.parse(section.dataset.ota));
      void refreshOpenOta();
    }
  }

  function cancelEdit(id) {
    closeDialog('edit-' + id);
  }
  async function saveGateway(id) {
    const fields = {
      name: 'en',
      ping_interval: 'ep',
      heartbeat_timeout: 'eht',
      heartbeat_send_interval: 'ehs',
      wol_broadcast_addr: 'ewb',
      scan_cidr: 'esc',
      scan_concurrency: 'escc',
      wifi_ssid: 'ewssid',
      wifi_password: 'ewpw'
    };
    const data = Object.fromEntries(Object.entries(fields).map(([key, prefix]) => [key, document.getElementById(prefix + '-' + id).value]));
    const result = await postForm('/gateway/' + id + '/update', data);
    if (result?.ok) closeDialog('edit-' + id);
  }

  let gatewayDeleteResolver;

  function resolveGatewayDelete(action) {
    if (['queue', 'force'].includes(action) && document.getElementById('gateway-delete-dialog').dataset.confirmWaiting === 'true') return;
    closeDialog('gateway-delete-dialog', action);
  }

  function chooseGatewayDelete(message, showForce, targets = [], title = t('删除网关')) {
    const dialog = document.getElementById('gateway-delete-dialog');
    dialog.returnValue = 'cancel';
    dialog.addEventListener('close', () => {
      gatewayDeleteResolver?.(dialog.returnValue);
      gatewayDeleteResolver = null;
    }, {
      once: true
    });
    document.getElementById('gateway-delete-title').textContent = title;
    document.getElementById('gateway-delete-message').textContent = message;
    renderConfirmationTargets(document.getElementById('gateway-delete-targets'), targets);
    dialog.dataset.deleteOptions = String(Boolean(showForce));
    document.getElementById('gateway-delete-force').hidden = !showForce;
    document.getElementById('gateway-delete-queue').textContent = t(showForce ? '排队等待删除' : '删除');
    document.getElementById('gateway-delete-queue').classList.toggle('danger', !showForce);
    globalThis.startConfirmationCountdown?.(document.getElementById('gateway-delete-dialog'), [document.getElementById('gateway-delete-queue'), document.getElementById('gateway-delete-force')]);
    openDialog('gateway-delete-dialog');
    return new Promise(resolve => {
      gatewayDeleteResolver = resolve;
    });
  }
  async function sendGatewayDelete(id, forceOffline = false, versions) {
    const form = new FormData();
    form.append('force_offline', String(forceOffline));
    const response = await fetch(`/gateway/${id}/delete`, {
      method: 'POST',
      wolVersions: versions,
      body: form
    });
    const result = await response.json();
    showToast(result.msg || (result.ok ? '删除请求已处理' : '删除失败'), {
      success: result.ok,
      error: !result.ok
    });
    if (result.ok) {
      await refreshGatewayList(activeGatewayPage);
      await refreshSystemStatus();
    }
  }
  async function deleteGateway(id, state) {
    const versions = window.WolSync.snapshot();
    if (document.getElementById('status-' + id)?.dataset.deletePending === 'true') return;
    const options = {
      titleText: gatewayDeleteTitle(id),
      danger: true,
      confirmText: '删除'
    };
    if (state === 'unbound') {
      if (!await uiConfirm('此网关尚未配对成功。删除后仅移除此待配对记录，不会向设备发送指令。', options)) return;
      return sendGatewayDelete(id, false, versions);
    }
    if (state === 'online') {
      if (!await uiConfirm('将向在线设备发送恢复出厂指令。后台会等设备确认清除配置后，再删除网关和子设备记录。', options)) return;
      return sendGatewayDelete(id, false, versions);
    }
    const choice = await chooseGatewayDelete(t('设备当前离线。推荐排队等待设备恢复在线；后台会重试发送重置，并在确认后删除记录。若设备长期无响应，也可仅删除后台记录，但设备配置不会被清除。'), true, [], options.titleText);
    if (choice === 'queue') return sendGatewayDelete(id, false, versions);
    if (choice === 'force') return sendGatewayDelete(id, true, versions);
  }
  async function deletePairing(id) {
    const versions = window.WolSync.snapshot();
    if (!await uiConfirm('确认删除此配对码记录？', {
        titleText: gatewayDeleteTitle(id, true),
        danger: true,
        confirmText: '删除'
      })) return;
    try {
      const response = await fetch(`/pairing/${id}/delete`, {
        wolVersions: versions,
        method: 'POST'
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.msg || '删除失败');
      showToast(result.msg || '配对记录已删除', {
        success: true
      });
      await refreshGatewayList(activeGatewayPage);
      await refreshSystemStatus();
    } catch (error) {
      showToast(error.message || '删除失败', {
        error: true
      });
    }
  }
  async function removeSelectedGateways() {
    const versions = window.WolSync.snapshot();
    const ids = [...document.querySelectorAll('.gateway-check:checked')].map(c => c.value);
    const selectedState = gatewayBatchState();
    if (!selectedState) {
      showToast('设备选择或状态已变化，请重新确认', {
        error: true
      });
      return;
    }
    if (!ids.length) {
      await uiAlert('请先选择网关或配对记录');
      return;
    }
    const offline = ids.filter(value => {
      const badge = document.getElementById('status-' + value.slice(2));
      return value.startsWith('g:') && (badge?.dataset.connectionState === 'offline' || badge?.classList.contains('offline'));
    });
    const targets = ids.map(value => describeTargetRow(document.getElementById((value.startsWith('p:') ? 'pairing-name-' : 'gateway-name-') + value.slice(2))));
    const title = ids.length === 1 ? gatewayDeleteTitle(ids[0].split(':')[1], ids[0].startsWith('p:')) : t('删除网关');
    const states = Object.values(JSON.parse(selectedState));
    const directMessage = states.every(state => state.startsWith('pairing:')) ? t(`将删除选中的 ${ids.length} 条配对记录。`) :
      states.every(state => state === 'gateway:unbound:unbound') ? t('将删除选中的待配对网关记录，不会向设备发送指令。') :
      t(`将删除选中的 ${ids.length} 条记录。在线网关会先收到重置，并等设备确认后再删除。`);
    const action = offline.length ?
      await chooseGatewayDelete(t(`将删除选中的 ${ids.length} 条记录。${offline.length} 台网关离线：可排队等待恢复在线并重置，或仅删除后台记录（设备本地配置不会清除）。`), true, targets, title) :
      await chooseGatewayDelete(directMessage, false, targets, title);
    if (action !== 'queue' && action !== 'force') return;
    if (!gatewaySelectionMatches(ids, selectedState)) {
      showToast('设备选择或状态已变化，请重新确认', {
        error: true
      });
      return;
    }
    const fd = new FormData();
    fd.append('ids', ids.join(','));
    fd.append('force_offline', String(action === 'force'));
    fd.append('selection_states', selectedState);
    const response = await fetch('/gateway/batch_unbind', {
      wolVersions: versions,
      method: 'POST',
      body: fd
    });
    const result = await response.json();
    showToast((result.msg || '批量删除已处理') + (result.skipped?.length ? '\n' + result.skipped.map(item => `${item.id}：${item.reason}`).join('\n') : ''), {
      success: result.ok,
      error: !result.ok || Boolean(result.skipped?.length)
    });
    if (result.ok) {
      await refreshGatewayList(1);
      await refreshSystemStatus();
    }
  }
  let gatewayCreating = false;
  async function createGateway() {
    if (gatewayCreating) return;
    const fd = new FormData();
    fd.append('name', document.getElementById('n').value);
    const owner = document.getElementById('new-gateway-owner');
    if (owner && (!owner.value || owner.selectedOptions?.[0]?.disabled)) {
      const unavailable = owner.tagName === 'SELECT' && ![...owner.options].some(option => option.value && !option.disabled);
      showToast(unavailable ? document.getElementById('new-gateway-owner-help').textContent.trim() : '请选择归属用户', {
        error: true
      });
      document.getElementById('new-gateway-owner-trigger')?.focus();
      return;
    }
    if (owner) fd.append('owner', owner.value);
    gatewayCreating = true;
    const submit = document.querySelector('#add-gateway button[type=submit]');
    if (submit) submit.disabled = true;
    try {
      const r = await fetch('/gateway/create', {
        method: 'POST',
        body: fd
      });
      const j = await r.json();
      if (!r.ok || !j.ok) throw new Error(j.detail || j.msg || '失败');
      closeDialog('add-gateway');
      if (typeof DOMParser !== 'undefined') await refreshGatewayList(1);
      await refreshSystemStatus();
      showPairing(j);
    } catch (error) {
      showToast(error.message || '失败', {
        error: true
      });
    } finally {
      gatewayCreating = false;
      if (submit) submit.disabled = false;
    }
  }

  function showPairing(j, replacement = j.purpose === 'transfer', recovery = false) {
    window.setDialogSymbol?.(document.querySelector('#key-modal .dialog-symbol'), replacement ? '转移网关' : '配对');
    document.getElementById('pairing-title').textContent = t(replacement ? '转移配对码' : '配对码');
    document.getElementById('key-value').textContent = j.pairing_code;
    const deadline = new Date((Number(j.expires_at) + 8 * 3600) * 1000).toISOString().slice(0, 19).replace('T', ' ');
    document.getElementById('pair-expiry').textContent = t('配对码有效期为15分钟，截止到{时间}。').replace('{时间}', deadline);
    document.getElementById('key-modal').dataset.pairingId = String(j.pairing_id);
    document.getElementById('key-modal').dataset.expiresAt = String(j.expires_at);
    const context = document.getElementById('pair-context');
    if (context) {
      context.hidden = !replacement && !recovery;
      context.textContent = replacement ? t('转移完成后，新网关接管原记录，保留子设备、配置和历史日志；旧网关不再关联此记录。') : recovery ? t('仅供原 MAC 的网关恢复配对，保留子设备、配置和历史日志。') : '';
    }
    refreshPairingTiming();
    openDialog('key-modal');
  }
  const pendingPairingRenewals = new Set();

  function pairingTiming(expiresAt) {
    const expiry = Number(expiresAt);
    if (!Number.isFinite(expiry) || expiry <= 0) return null;
    const remaining = Math.max(0, Math.ceil((expiry * 1000 - Date.now()) / 1000));
    const date = new Date((expiry + 8 * 3600) * 1000).toISOString().slice(0, 19).replace('T', ' ');
    const seconds = remaining <= 60;
    const minutes = Math.ceil(remaining / 60);
    const duration = seconds ? t('{秒}秒').replace('{秒}', remaining) : t('{分钟}分钟').replace('{分钟}', minutes);
    return {
      expired: !remaining,
      label: !remaining ? t('配对超时') : seconds ? t('{秒}s后过期').replace('{秒}', remaining) : remaining <= 300 ? t('即将过期') : t('待绑定'),
      badgeTitle: !remaining ? t('配对码已于{时间}过期').replace('{时间}', date) : remaining <= 300 ? t('配对码将于{时长}后过期').replace('{时长}', duration) : t('配对码将于{时间}过期').replace('{时间}', date),
      renewTitle: !remaining ? t('重新生成配对码') : t('{时长}后可再次生成').replace('{时长}', duration)
    };
  }

  function updatePairingBadge(state, timing) {
    state.dataset.state = timing.expired ? 'expired' : 'pending';
    const css = 'badge ' + (timing.expired ? 'pairing_expired' : 'unbound');
    if (state.className !== css) state.className = css;
    if (state.textContent !== timing.label) state.textContent = timing.label;
    if ((state.getAttribute('data-tooltip') || state.title) !== timing.badgeTitle) state.title = timing.badgeTitle;
  }

  function refreshPairingTiming() {
    for (const state of document.querySelectorAll('[id^="pair-state-"],[id^="gateway-pair-state-"]')) {
      const timing = pairingTiming(state.dataset.expiresAt);
      if (!timing || state.dataset.state === 'claimed') continue;
      updatePairingBadge(state, timing);
      if (state.id.startsWith('gateway-pair-state-')) continue;
      const id = state.id.slice('pair-state-'.length),
        button = document.getElementById('pair-renew-' + id);
      if (button) {
        button.disabled = !timing.expired || pendingPairingRenewals.has(id);
        if ((button.getAttribute('data-tooltip') || button.title) !== timing.renewTitle) button.title = timing.renewTitle;
      }
    }
    for (const button of document.querySelectorAll('[data-pairing-expires-at]')) {
      const timing = pairingTiming(button.dataset.pairingExpiresAt);
      button.disabled = Boolean(timing && !timing.expired);
      const title = timing && !timing.expired ? timing.renewTitle : t(button.dataset.iconLabel === '转移网关' ? '转移网关' : '重新生成配对码');
      if ((button.getAttribute('data-tooltip') || button.title) !== title) button.title = title;
    }
  }

  function pairingRenewalAllowed(id) {
    const state = document.getElementById('pair-state-' + id);
    const expiry = Number(state?.dataset.expiresAt);
    return Boolean(state && state.dataset.state !== 'claimed' && Number.isFinite(expiry) && expiry * 1000 <= Date.now());
  }

  setInterval(refreshPairingTiming, 1000);
  document.addEventListener('DOMContentLoaded', refreshPairingTiming);
  document.addEventListener('wol:language-changed', refreshPairingTiming);

  async function renewPairing(id) {
    const versions = window.WolSync.snapshot();
    id = String(id);
    if (pendingPairingRenewals.has(id) || !pairingRenewalAllowed(id)) return;
    pendingPairingRenewals.add(id);
    refreshPairingTiming();
    try {
      if (!await uiConfirm('重新生成后，旧配对码将立即失效。确认生成新的配对码？', {
          title: '重新生成配对码',
          confirmText: '重新生成',
          danger: true,
          skipCountdown: true
        })) return;
      if (!pairingRenewalAllowed(id)) return;
      const response = await fetch(`/pairing/${id}/renew`, {
        wolVersions: versions,
        method: 'POST',
        signal: AbortSignal.timeout(10000)
      });
      const result = await response.json();
      const state = document.getElementById('pair-state-' + id);
      if (state && Number.isFinite(result.expires_at)) state.dataset.expiresAt = String(result.expires_at);
      if (!response.ok || !result.ok) throw new Error(result.msg || '重新生成配对码失败');
      showPairing(result);
      await refreshGatewayList(activeGatewayPage);
    } catch (error) {
      showToast(error.message || '重新生成配对码失败', {
        error: true
      });
    } finally {
      pendingPairingRenewals.delete(id);
      refreshPairingTiming();
    }
  }

  const pendingGatewayPairingRenewals = new Set();

  function rememberGatewayTicket(id, result) {
    if (!Number.isFinite(result.expires_at)) return;
    const gateway = latestGatewayStates?.find(item => String(item.id) === String(id));
    if (gateway) {
      gateway.pairing = {
        id: result.pairing_id,
        expires_at: result.expires_at,
        purpose: result.purpose
      };
      applyGatewayStates([gateway]);
    } else {
      document.querySelectorAll(`#actions-${id} [data-pairing-expires-at]`).forEach(button => {
        button.dataset.pairingExpiresAt = result.expires_at;
      });
    }
    refreshPairingTiming();
  }
  async function renewGatewayPairing(id) {
    const versions = window.WolSync.snapshot();
    id = String(id);
    const button = document.querySelector('#actions-' + id + ' [data-icon-label="重新生成"]');
    if (!button || button.disabled || pendingGatewayPairingRenewals.has(id)) return;
    pendingGatewayPairingRenewals.add(id);
    try {
      if (!await uiConfirm('重新生成后，旧配对码将立即失效。新码仅供原 MAC 的网关恢复配对，保留现有数据。确认继续？', {
          title: '重新生成配对码',
          confirmText: '重新生成',
          danger: true,
          skipCountdown: true
        })) return;
      const current = document.querySelector('#actions-' + id + ' [data-icon-label="重新生成"]');
      const gateway = latestGatewayStates?.find(item => String(item.id) === id);
      if (!current || current.disabled || (gateway && !gateway.can_repair_pairing)) return;
      const response = await fetch('/gateway/' + id + '/pairing/renew', {
        wolVersions: versions,
        method: 'POST',
        signal: AbortSignal.timeout(10000)
      });
      const result = await response.json();
      rememberGatewayTicket(id, result);
      if (!response.ok || !result.ok) throw new Error(result.msg || '重新生成配对码失败');
      showPairing(result, false, true);
    } catch (error) {
      showToast(error.message || '重新生成配对码失败', {
        error: true
      });
    } finally {
      pendingGatewayPairingRenewals.delete(id);
    }
  }

  const pendingGatewayTransfers = new Set();
  async function transferGateway(id) {
    const versions = window.WolSync.snapshot();
    id = String(id);
    const button = document.querySelector('#actions-' + id + ' [data-icon-label="转移网关"]');
    if (!button || button.disabled || pendingGatewayTransfers.has(id)) return;
    pendingGatewayTransfers.add(id);
    try {
      if (!await uiConfirm('将生成转移配对码，供另一台网关接管原记录，保留子设备、配置和历史日志；旧网关不再关联此记录。确认继续？', {
          title: '转移网关',
          confirmText: '生成转移码',
          danger: true,
          countdown: 3
        })) return;
      const current = document.querySelector('#actions-' + id + ' [data-icon-label="转移网关"]');
      const gateway = latestGatewayStates?.find(item => String(item.id) === id);
      if (!current || current.disabled || (gateway && !gateway.can_transfer)) return;
      const response = await fetch('/gateway/' + id + '/transfer', {
        wolVersions: versions,
        method: 'POST',
        signal: AbortSignal.timeout(10000)
      });
      const result = await response.json();
      rememberGatewayTicket(id, result);
      if (!response.ok || !result.ok) throw new Error(result.msg || '生成转移码失败');
      showPairing(result, true);
    } catch (error) {
      showToast(error.message || '生成转移码失败', {
        error: true
      });
    } finally {
      pendingGatewayTransfers.delete(id);
    }
  }

  function closeKey() {
    closeDialog('key-modal');
  }

  function fallbackCopy(value) {
    const previousFocus = document.activeElement;
    const input = document.createElement('textarea');
    input.value = value;
    input.readOnly = true;
    input.style.cssText = 'position:fixed;left:0;top:0;width:1px;height:1px;opacity:0';
    const dialog = document.getElementById('key-modal');
    (dialog.open ? dialog : document.body).append(input);
    try {
      input.focus();
      input.select();
      input.setSelectionRange(0, value.length);
      return document.execCommand('copy') === true;
    } finally {
      input.remove();
      previousFocus?.focus();
    }
  }
  async function copyKey() {
    const value = document.getElementById('key-value').textContent.trim();
    if (!value) {
      showToast('没有可复制的配对码', {
        error: true
      });
      return false;
    }
    let copied = false;
    try {
      if (window.isSecureContext && navigator.clipboard?.writeText) {
        try {
          await navigator.clipboard.writeText(value);
          copied = true;
        } catch (_) {
          copied = fallbackCopy(value);
        }
      } else copied = fallbackCopy(value);
    } catch (_) {}
    if (copied) {
      closeDialog('key-modal');
      showToast('配对码已复制', {
        success: true
      });
    } else showToast('复制失败，请手动复制：' + value, {
      error: true
    });
    return copied;
  }

  let systemStatusRefreshing = false;
  let backendUptimeSample = null;

  function renderBackendUptime() {
    const node = document.getElementById('backend-uptime');
    if (!node || !backendUptimeSample) return;
    let seconds = Math.max(0, Math.floor(backendUptimeSample.seconds + (performance.now() - backendUptimeSample.at) / 1000));
    const english = t('系统状态') !== '系统状态';
    const units = [
      [31536000, '年', 'y'],
      [2592000, '个月', 'mo'],
      [604800, '星期', 'w'],
      [86400, '天', 'd'],
      [3600, '小时', 'h'],
      [60, '分钟', 'm'],
      [1, '秒', 's']
    ];
    const parts = [];
    for (const [size, zh, en] of units) {
      const value = Math.floor(seconds / size);
      seconds %= size;
      if (value || parts.length || size === 1) parts.push(value + (english ? en : zh));
    }
    node.textContent = (english ? 'Uptime ' : '已运行') + parts.join(english ? ' ' : '\u200b');
    node.hidden = false;
  }

  function serviceDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '';
    if (seconds < 60) return Math.floor(seconds) + 's';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm' + Math.floor(seconds % 60) + 's';
    if (seconds < 86400) return Math.floor(seconds / 3600) + 'h' + Math.floor(seconds % 3600 / 60) + 'm';
    return Math.floor(seconds / 86400) + 'd' + Math.floor(seconds % 86400 / 3600) + 'h';
  }

  function serviceLoad(server) {
    const load = server?.load_percent;
    return Number.isFinite(load) && load >= 0 ? load.toFixed(1) + '%' : '';
  }

  function renderSystemStatus(data) {
    const server = data?.server;
    const load = serviceLoad(server);
    const resourceFailed = data?.health?.issues?.some(issue => ['memory_usage_high', 'disk_usage_high'].includes(issue));
    const nginx = data?.http?.nginx;
    const probe = nginx?.http_probe;
    const httpFailed = nginx?.active === false || probe?.status === 'unavailable' || probe?.http_status >= 500;
    const roundtrip = data?.mqtt?.roundtrip;
    const mqttFailed = ['timeout', 'error'].includes(roundtrip?.status);
    const database = data?.database;
    const databaseLabel = document.getElementById('system-database-label');
    if (databaseLabel && ['MySQL', 'SQLite'].includes(database?.engine)) databaseLabel.textContent = database.engine;
    const states = {
      debian: resourceFailed ? 'error' : load ? 'online' : 'unknown',
      backend: data?.backend?.status === 'ok' ? 'online' : ['unavailable', 'disconnected'].includes(data?.backend?.status) ? 'offline' : ['error', 'timeout', 'degraded'].includes(data?.backend?.status) ? 'error' : 'unknown',
      mqtt: data?.mqtt?.connected === false ? 'offline' : mqttFailed ? 'error' : data?.mqtt?.connected === true && roundtrip?.status === 'responding' ? 'online' : 'unknown',
      http: nginx?.active === false ? 'offline' : httpFailed ? 'error' : nginx?.active === true && probe?.status === 'responding' ? 'online' : 'unknown',
      database: database?.status === 'ok' ? 'online' : ['error', 'degraded', 'timeout'].includes(database?.status) ? 'error' : ['unavailable', 'disconnected'].includes(database?.status) ? 'offline' : 'unknown'
    };
    const measurements = {
      debian: load,
      backend: serviceDuration(data?.backend?.uptime_seconds),
      mqtt: serviceDuration(data?.mqtt?.uptime_seconds),
      http: Number.isInteger(probe?.http_status) && probe.http_status >= 100 && probe.http_status <= 599 ? String(probe.http_status) : '',
      database: database?.status === 'ok' && Number.isFinite(database?.latency_ms) && database.latency_ms >= 0 ? database.latency_ms.toFixed(1) + 'ms' : ''
    };
    const descriptions = {
      debian: t('1 分钟平均负载 ÷ CPU 逻辑核心数 × 100%；可能超过 100%'),
      backend: t('后端本次启动后的运行时长'),
      mqtt: t('MQTT Broker 本次启动后的运行时长'),
      http: t('Nginx 本机 HTTP 探测的响应码，不代表公网 HTTPS 检查'),
      database: t('数据库连接及读取检查耗时')
    };
    const faults = {
      debian: t('服务器资源使用率过高'),
      backend: t('后端响应异常'),
      mqtt: t(data?.mqtt?.connected === false ? 'MQTT 连接已断开' : roundtrip?.status === 'timeout' ? 'MQTT 探测超时' : 'MQTT 探测失败'),
      http: t(nginx?.active === false ? 'Nginx 服务未运行' : 'Nginx HTTP 探测异常'),
      database: t('数据库连接或读取异常')
    };
    for (const [name, state] of Object.entries(states)) {
      const node = document.getElementById('system-' + name);
      if (!node) continue;
      node.textContent = state === 'offline' ? '—' : measurements[name] || '—';
      node.title = [descriptions[name], state === 'unknown' ? t('待确认') : ['offline', 'error'].includes(state) ? faults[name] : ''].filter(Boolean).join(' · ');
      node.className = 'service-value ' + state;
      const group = node.closest?.('.service-state');
      if (group) group.className = 'service-state ' + state;
    }
    const failed = Object.values(states).some(state => ['offline', 'error'].includes(state)) || data?.health?.status === 'degraded';
    const unknown = Object.values(states).includes('unknown');
    const status = document.getElementById('system-status');
    status.textContent = t(failed ? '异常' : unknown ? '未知' : '正常');
    status.className = 'badge service-summary ' + (failed ? 'error' : unknown ? 'unknown' : 'online');
    const notes = Object.entries(states).filter(([, state]) => ['offline', 'error'].includes(state)).map(([name]) => faults[name]);
    if (data?.health?.status === 'degraded') notes.push(t(data?.health?.issues?.includes('database_error') ? '数据库连接或读取异常' : '资源或辅助服务异常'));
    status.title = [...new Set(notes)].join('；') || t(unknown ? '待确认' : '运行正常');
  }
  let systemStatusEpoch = 0;
  let systemStatusPending = false;
  async function refreshSystemStatus() {
    if (!document.getElementById('count-online')) return;
    if (systemStatusRefreshing) {
      systemStatusPending = true;
      return;
    }
    const epoch = systemStatusEpoch;
    systemStatusRefreshing = true;
    try {
      const response = await fetch('/api/status', {
        cache: 'no-store'
      });
      if (!response.ok) throw new Error('无法读取系统状态');
      const data = await response.json(),
        counts = data.gateways || {};
      if (epoch !== systemStatusEpoch) return;
      if (Number.isFinite(data?.backend?.uptime_seconds) && data.backend.uptime_seconds >= 0) {
        backendUptimeSample = {
          seconds: data.backend.uptime_seconds,
          at: performance.now()
        };
        renderBackendUptime();
      }
      if (document.getElementById('system-status')) renderSystemStatus(data);
      document.getElementById('count-online').textContent = counts.online || 0;
      document.getElementById('count-offline').textContent = counts.offline || 0;
      document.getElementById('count-unbound').textContent = counts.abnormal || 0;
      const users = document.getElementById('count-users');
      if (users) users.textContent = data.users_total ?? '—';
      const devices = document.getElementById('count-devices');
      if (devices) devices.textContent = data.devices_total ?? '—';
    } catch (error) {
      if (epoch !== systemStatusEpoch) return;
      if (document.getElementById('system-status')) renderSystemStatus(null);
      const status = document.getElementById('system-status');
      if (!status) return;
      status.textContent = t('异常');
      status.className = 'badge service-summary error';
      status.title = t('无法读取系统状态');
    } finally {
      systemStatusRefreshing = false;
      if (systemStatusPending) {
        systemStatusPending = false;
        void refreshSystemStatus();
      }
    }
  }
  if (document.getElementById('count-online')) {
    setInterval(renderBackendUptime, 1000);
    setInterval(refreshSystemStatus, 5000);
    document.addEventListener('DOMContentLoaded', refreshSystemStatus);
  }
  document.addEventListener('wol:permissions-changed', () => {
    systemStatusEpoch++;
    void refreshSystemStatus();
    gatewayStructureChanged = true;
    void refreshGateways();
  });



  // State polling is independent of the device heartbeat interval.
  const STATUS_TEXT = {
    unbound: '待绑定',
    online: '在线',
    offline: '离线',
    long_offline: '长期离线',
    pairing_expired: '配对超时',
    pairing_error: '配对码错误',
    reset_failed: '重置失败'
  };
  let gatewaysRefreshing = false;
  let lastGatewayIds = null;
  let gatewayStructureChanged = false;
  let latestGatewayStates = null;
  let gatewayRefreshFailed = false;

  function requestGatewayListRefresh(page = 1, append = false) {
    const publicRefresh = globalThis.refreshGatewayList;
    if (typeof publicRefresh === 'function' && publicRefresh !== refreshGatewayList) return publicRefresh(page, append, false);
    return refreshGatewayList(page, append, false);
  }

  function applyGatewayStates(list) {
    for (const g of list) {
      const el = document.getElementById('status-' + g.id);
      if (el) {
        const state = g.display_status || (g.delete_requested_at ? 'deleting' : g.status);
        el.className = 'badge ' + state;
        el.dataset.connectionState = g.status;
        el.textContent = t(({
          error: '异常',
          bind_failed: '绑定失败',
          deleting: '待删除',
          ...STATUS_TEXT
        })[state] || '异常');
      }
      if (el) {
        el.title = t(g.notice || (g.delete_requested_at ? g.delete_error || (g.status === 'offline' ? '设备离线；已排队，等待上线后重置' : '等待设备确认重置') : g.bind_message || ''));
        el.dataset.deletePending = String(Boolean(g.delete_requested_at));
      }
      const ip = document.getElementById('ip-' + g.id);
      const deviceCount = document.getElementById('gateway-device-count-' + g.id);
      if (deviceCount && Number.isInteger(g.device_count)) {
        deviceCount.textContent = g.device_count;
        deviceCount.title = t(`${g.device_online_count}个在线，${g.device_offline_count}个离线${g.device_unknown_count ? `，${g.device_unknown_count}个未知` : ''}`);
      }
      if (ip) ip.textContent = g.ip || '-';
      const mac = document.getElementById('gateway-mac-' + g.id);
      if (mac) mac.textContent = String(g.mac).replace(/(..)(?=.)/g, '$1:').toUpperCase();
      const hb = document.getElementById('hb-' + g.id);
      if (hb) hb.textContent = g.last_heartbeat || '-';
      const identity = document.getElementById('status-' + g.id)?.parentElement;
      let pairingBadge = document.getElementById('gateway-pair-state-' + g.id);
      if (g.pairing && identity) {
        if (!pairingBadge) {
          pairingBadge = document.createElement('span');
          pairingBadge.id = 'gateway-pair-state-' + g.id;
          identity.append(pairingBadge);
        }
        pairingBadge.dataset.expiresAt = g.pairing.expires_at;
      } else pairingBadge?.remove();
      const ssid = document.getElementById('ewssid-' + g.id);
      if (ssid && typeof g.wifi_ssid === 'string') {
        const typed = ssid.value;
        ssid.defaultValue = g.wifi_ssid;
        ssid.value = ssid.closest('dialog')?.open ? typed : g.wifi_ssid;
      }
      const checkbox = document.querySelector(`[data-gateway-row="${g.id}"] .gateway-check`);
      if (checkbox) {
        checkbox.disabled = Boolean(g.delete_requested_at);
        if (checkbox.disabled) checkbox.checked = false;
      }
      const actions = document.getElementById('actions-' + g.id);
      if (actions) {
        const markup = gatewayActionMarkup(g);
        if (actions.dataset.markup !== markup) {
          actions.innerHTML = markup;
          actions.dataset.markup = markup;
          actions.querySelectorAll?.('[data-icon-label]').forEach(button => {
            const title = button.title;
            window.setActionIcon?.(button, button.dataset.iconLabel);
            button.title = title;
          });
        }
      }
    }
    refreshPairingTiming();
    globalThis.WolTables?.reorder('gateway-table');
    updateGatewaySelection();
  }
  let lastPairingSignature = null;
  async function refreshGateways() {
    if (window.WolPermissions && !window.WolPermissions.can('gateway_view')) return;
    if (gatewaysRefreshing) return;
    gatewaysRefreshing = true;
    try {
      const owner = new URLSearchParams(location.search).get('owner');
      const r = await fetch('/api/gateways' + (owner ? '?owner=' + encodeURIComponent(owner) : ''), {
        cache: 'no-store',
        signal: AbortSignal.timeout(10000)
      });
      if (!r.ok) throw new Error('网关列表读取失败');
      const data = await r.json(),
        list = data.gateways || [];
      latestGatewayStates = list;
      gatewayRefreshFailed = false;
      try {
        applyGatewayStates(list);
      } catch (error) {
        console.error('Gateway state render failed', error);
      }
      const liveGatewayIds = new Set(list.map(gateway => String(gateway.id)));
      const missingVisibleRow = [...document.querySelectorAll('[data-gateway-row]')].some(row => !liveGatewayIds.has(row.dataset.gatewayRow));
      const countNode = document.getElementById('gateway-count');
      const renderedTotal = countNode ? Number((countNode.textContent || '').split('/')[1]) : null;
      const initialCountChanged = lastGatewayIds === null && Number.isInteger(data.all_total) && renderedTotal !== data.all_total;
      const statusFilter = document.getElementById('gateway-status-filter');
      const filtering = statusFilter ? statusFilter.value !== 'all' : false;
      const signature = list.map(gateway => `${gateway.id}:${gateway.revision}${filtering ? ':' + gateway.display_status : ''}`).join(',') + (data.owners_revision || '');
      gatewayStructureChanged ||= missingVisibleRow || initialCountChanged || (lastGatewayIds !== null && signature !== lastGatewayIds);
      lastGatewayIds = signature;
      if (gatewayStructureChanged && !gatewayListLoading) {
        gatewayStructureChanged = false;
        requestGatewayListRefresh(1, false);
      }
      const pairings = data.pairings || [];
      const count = document.getElementById('gateway-count');
      if (count) count.textContent = `${data.online_total ?? list.filter(g => g.status === 'online').length}/${data.all_total ?? (list.length + pairings.length)}`;
      const keyModal = document.getElementById('key-modal');
      const activePairingId = keyModal?.dataset.pairingId;
      const pairingExpiry = Number(keyModal?.dataset.expiresAt);

      const livePairingIds = new Set(pairings.map(item => String(item.id)));
      const renderedPairingIds = [...document.querySelectorAll('[data-pairing-row]')].map(row => row.dataset.pairingRow);
      const query = (document.getElementById('q')?.value || '').trim().toLowerCase();
      const includePairings = ['all', 'abnormal'].includes(statusFilter?.value || 'all');
      const matchingPairings = pairings.filter(item => includePairings && (!query || String(item.name || '').toLowerCase().includes(query) || `p-${item.id}` === query));
      const pairingSignature = matchingPairings.map(item => `${item.id}:${item.revision}`).join(',');
      const pairingChanged = lastPairingSignature !== null && pairingSignature !== lastPairingSignature;
      lastPairingSignature = pairingSignature;
      if (!document.querySelector('dialog[open]') && (renderedPairingIds.some(id => !livePairingIds.has(id)) || pairingChanged)) {
        requestGatewayListRefresh(1);
        refreshSystemStatus();
        return;
      }
      for (const item of pairings) {
        if (item.state === 'claimed') {
          requestGatewayListRefresh(1);
          return;
        }
        const state = document.getElementById('pair-state-' + item.id);
        if (state) {
          state.dataset.state = item.state;
          state.dataset.expiresAt = item.expires_at;
        }

      }
      try {
        refreshPairingTiming();
        globalThis.WolTables?.paint("gateway-table");
        updateGatewaySelection();
      } catch (error) {
        console.error('Gateway state controls refresh failed', error);
      }
    } catch (e) {
      if (!gatewayRefreshFailed) showToast('网关状态刷新失败，正在重试…', {
        error: true
      });
      gatewayRefreshFailed = true;
      console.warn('Gateway state refresh failed', e);
    } finally {
      gatewaysRefreshing = false;
    }
  }
  setInterval(() => {
    if (!document.hidden) void refreshGateways();
    if (!document.hidden) void refreshOpenOta();
  }, 3000);
  window.addEventListener('focus', refreshGateways);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) void refreshGateways();
  });
  document.addEventListener('close', event => {
    if (event.target.matches?.('.gateway-editor')) void refreshGatewayList(activeGatewayPage, false, false);
  }, true);
  document.addEventListener('DOMContentLoaded', refreshGateways);

  function gatewayActionMarkup(gateway) {
    const id = String(gateway.id);
    const pending = Boolean(gateway.delete_requested_at);
    const button = (label, css, title, disabled = false, onclick = '') =>
      `<button type="button" ${["重新生成","转移网关"].includes(label) ? `data-pairing-expires-at="${Number(gateway.pairing?.expires_at || 0)}"` : ""} class="${css} icon-button" ${disabled ? 'disabled' : ''} ${onclick ? `onclick="${onclick}"` : ''} data-icon-label="${label}" aria-label="${t(label)}" title="${t(title)}">${t(label)}</button>`;
    const links = [
      ['子设备', 'devices'],
      ['日志', 'logs']
    ].map(([label, page]) =>
      `<a href="/gateway/${id}/${page}">${button(label, 'gray', label)}</a>`).join('');
    const unavailable = '网关正在等待删除，请先撤销删除';
    const blocked = pending || gateway.can_manage === false;
    const edit = button('编辑', 'gray', pending ? unavailable : blocked ? '网关状态异常，仅可查看设备和日志' : '编辑', blocked, blocked ? '' : `showEdit('${id}')`);
    const displayState = gateway.display_status || gateway.status;
    const repairNotice = gateway.can_repair_pairing ? '重新生成配对码' : displayState === 'online' ? '网关在线，无需重新生成配对码' : displayState === 'unbound' ? '网关尚未绑定，无法重新生成配对码' : '网关状态异常，无法重新生成配对码';
    const repair = !pending && gateway.can_repair_pairing ? button('重新生成', 'gray', repairNotice, false, `renewGatewayPairing('${id}')`) : '';
    const transfer = !pending && gateway.can_transfer ? button('转移网关', 'gray', '转移网关', false, `transferGateway('${id}')`) : '';
    const state = ['online', 'unbound'].includes(gateway.status) ? gateway.status : 'offline';
    const remove = button('删除', 'danger', pending ? unavailable : '删除', pending, pending ? '' : `deleteGateway('${id}', '${state}')`);
    const cancel = pending && gateway.can_cancel_delete ? button('撤销删除', 'gray', '撤销删除', false, `cancelGatewayDelete('${id}')`) : '';
    return edit + links + remove + repair + transfer + cancel;
  }

  const pendingDeleteCancellations = new Set();
  async function cancelGatewayDelete(id) {
    const versions = window.WolSync.snapshot();
    let button = document.querySelector(`#actions-${id} [data-icon-label="撤销删除"]`);
    if (button?.disabled || pendingDeleteCancellations.has(id)) return;
    pendingDeleteCancellations.add(id);
    let submitted = false;
    try {
      if (!await uiConfirm('确认撤销此网关的排队删除？撤销后将恢复正常管理。', {
          title: '撤销删除',
          confirmText: '撤销删除'
        })) return;
      button = document.querySelector(`#actions-${id} [data-icon-label="撤销删除"]`);
      const gateway = latestGatewayStates?.find(item => String(item.id) === String(id));
      if (button?.disabled || (gateway && !gateway.can_cancel_delete)) return;
      if (button) button.disabled = true;
      submitted = true;
      const response = await fetch(`/gateway/${id}/delete/cancel`, {
        wolVersions: versions,
        method: 'POST',
        signal: AbortSignal.timeout(10000)
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.msg || '撤销删除失败');
      showToast(result.msg, {
        success: true
      });
    } catch (error) {
      showToast(error.message || '撤销删除失败', {
        error: true
      });
    } finally {
      pendingDeleteCancellations.delete(id);
      if (submitted) {
        await refreshGateways();
        if (button?.isConnected) button.disabled = !latestGatewayStates?.find(gateway => String(gateway.id) === String(id))?.can_cancel_delete;
      }
    }
  }

  const gatewaySentinel = document.getElementById('gateway-sentinel');
  let activeGatewayPage = Number(gatewaySentinel.dataset.page || 1);
  const gatewayPageSize = Number(gatewaySentinel.dataset.pageSize || 20);
  let gatewayTotalPages = Number(gatewaySentinel.dataset.totalPages || 1);
  let gatewayListRequest = 0;
  let gatewayListLoading = false;
  let gatewayListPending = null;

  function gatewayRowToken(row) {
    if (row?.dataset?.pairingRow) return 'p:' + row.dataset.pairingRow;
    if (row?.dataset?.gatewayRow) return 'g:' + row.dataset.gatewayRow;
    return 'empty';
  }

  function reconcileGatewayRows(currentBody, nextBody, append) {
    const currentRows = [...currentBody.children];
    const currentByToken = new Map(currentRows.map(row => [gatewayRowToken(row), row]));
    const incomingRows = [...nextBody.children];
    const incomingTokens = new Set(incomingRows.map(gatewayRowToken));

    for (const row of currentRows) {
      if (!incomingTokens.has(gatewayRowToken(row))) window.removeTableRow?.(row);
    }

    if (append) {
      for (const source of incomingRows) {
        const token = gatewayRowToken(source);
        if (currentByToken.has(token)) continue;
        const row = source.cloneNode(true);
        currentBody.append(row);
        window.animateTableRow?.(row);
      }
      return;
    }

    for (const source of incomingRows) {
      const token = gatewayRowToken(source);
      let row = currentByToken.get(token);
      if (!row) {
        row = source.cloneNode(true);
        currentBody.append(row);
        window.animateTableRow?.(row);
      } else {
        const replacement = source.cloneNode(true);
        row.replaceWith(replacement);
        row = replacement;
      }
      const position = incomingRows.indexOf(source);
      const reference = currentBody.children[position];
      if (reference !== row) currentBody.insertBefore(row, reference || null);
    }
  }

  async function refreshGatewayList(page = 1, append = false, showLoading = true) {
    if (gatewayListLoading) {
      if (!append) ++gatewayListRequest;
      gatewayListPending = {
        page,
        append,
        showLoading
      };
      return;
    }
    gatewayListLoading = true;
    const loadingRow = document.getElementById('gateway-list-loading');
    const loadingTimer = setTimeout(() => {
      if (showLoading && loadingRow) loadingRow.hidden = false;
    }, 350);
    document.getElementById('gateway-table').setAttribute('aria-busy', 'true');
    const q = document.getElementById('q').value || '';
    const currentOwner = new URLSearchParams(location.search).get('owner');
    const owner = currentOwner ? `&owner=${encodeURIComponent(currentOwner)}` : '';
    const status = document.getElementById('gateway-status-filter').value;
    const url = `/?page=${page}&page_size=${gatewayPageSize}&q=${encodeURIComponent(q)}&status=${encodeURIComponent(status)}${owner}${globalThis.WolTables?.query("gateway-table") || ""}`;
    const requestId = ++gatewayListRequest;
    try {
      const response = await fetch(url, {
        cache: 'no-store',
        signal: AbortSignal.timeout(10000),
        headers: {
          'X-Requested-With': 'fetch'
        }
      });
      if (!response.ok) throw new Error('网关列表读取失败');
      const html = await response.text();
      if (requestId !== gatewayListRequest) return;
      const doc = new DOMParser().parseFromString(html, 'text/html');
      const bulkOwner = document.getElementById('gateway-bulk-owner-select');
      const nextBulkOwner = doc.getElementById('gateway-bulk-owner-select');
      if (bulkOwner && nextBulkOwner && bulkOwner.innerHTML !== nextBulkOwner.innerHTML) {
        const value = bulkOwner.value;
        bulkOwner.replaceChildren(...[...nextBulkOwner.options].map(option => option.cloneNode(true)));
        if ([...bulkOwner.options].some(option => option.value === value)) bulkOwner.value = value;
        bulkOwner.dispatchEvent(new Event('change', {
          bubbles: true
        }));
      }
      const nextTable = doc.querySelector('#gateway-table');
      const nextSentinel = doc.querySelector('#gateway-sentinel');
      const nextEditors = doc.querySelector('#gateway-editors');
      if (!nextTable || !nextSentinel || !nextEditors) throw new Error('网关列表格式错误');
      for (const node of nextTable.querySelectorAll('[data-sync-key][data-revision]')) {
        window.WolSync?.remember(node.dataset.syncKey, node.dataset.revision);
      }
      const ownerSelect = document.getElementById('new-gateway-owner');
      const nextOwnerSelect = doc.getElementById('new-gateway-owner');
      if (ownerSelect?.tagName === 'SELECT' && nextOwnerSelect?.tagName === 'SELECT') {
        const value = ownerSelect.value;
        if (ownerSelect.innerHTML !== nextOwnerSelect.innerHTML) {
          ownerSelect.replaceChildren(...[...nextOwnerSelect.options].map(option => option.cloneNode(true)));
          if ([...ownerSelect.options].some(option => option.value === value)) ownerSelect.value = value;
          ownerSelect.dispatchEvent(new Event('change', {
            bubbles: true
          }));
        }
        const help = document.getElementById('new-gateway-owner-help');
        help.textContent = doc.getElementById('new-gateway-owner-help').textContent;
      }
      const selected = new Set([...document.querySelectorAll('.gateway-check:checked')].map(box => box.value));
      nextTable.querySelectorAll('.gateway-check').forEach(box => {
        box.checked = !box.disabled && selected.has(box.value);
      });
      const currentTable = document.getElementById('gateway-table');
      if (append) {
        reconcileGatewayRows(currentTable.tBodies[0], nextTable.tBodies[0], true);
        for (const editor of nextEditors.children)
          if (!document.getElementById(editor.id)) document.getElementById('gateway-editors').append(editor.cloneNode(true));
      } else {
        reconcileGatewayRows(currentTable.tBodies[0], nextTable.tBodies[0], false);
        const currentEditors = document.getElementById('gateway-editors');
        const incoming = new Set([...nextEditors.children].map(node => node.id));
        for (const editor of [...nextEditors.children]) {
          const current = document.getElementById(editor.id);
          if (current?.open) continue;
          if (current) current.replaceWith(editor);
          else currentEditors.append(editor);
        }
        for (const editor of [...currentEditors.children])
          if (!incoming.has(editor.id) && !editor.open) editor.remove();
      }
      document.dispatchEvent(new CustomEvent('wol:controls-changed'));
      const count = doc.getElementById('gateway-count');
      if (count) document.getElementById('gateway-count').textContent = count.textContent;
      const nextSearch = doc.getElementById('gateway-search-form');
      if (nextSearch) setSearchAvailable('gateway-search-form', nextSearch.dataset.hasData === 'true');
      if (latestGatewayStates) applyGatewayStates(latestGatewayStates);
      refreshPairingTiming();
      globalThis.WolTables?.paint("gateway-table");
      updateGatewaySelection();
      activeGatewayPage = Number(nextSentinel.dataset.page || page);
      gatewayTotalPages = Number(nextSentinel.dataset.totalPages || 1);
      gatewaySentinel.dataset.page = String(activeGatewayPage);
      gatewaySentinel.dataset.totalPages = String(gatewayTotalPages);
      applyAnimatedHidden(gatewaySentinel, activeGatewayPage >= gatewayTotalPages);
      history.replaceState({}, '', url);
    } catch (error) {
      showToast(error.message || '网关列表读取失败', {
        error: true
      });
    } finally {
      clearTimeout(loadingTimer);
      if (loadingRow) loadingRow.hidden = true;
      document.getElementById('gateway-table').setAttribute('aria-busy', 'false');
      gatewayListLoading = false;
      if (gatewayListPending) {
        const pending = gatewayListPending;
        gatewayListPending = null;
        refreshGatewayList(pending.page, pending.append, pending.showLoading);
      }
    }
  }
  globalThis.WolTables?.bind('gateway-table', () => refreshGatewayList(1, false));
  let gatewaySearchTimer;
  document.getElementById('q')?.addEventListener?.('input', () => {
    clearTimeout(gatewaySearchTimer);
    gatewaySearchTimer = setTimeout(() => refreshGatewayList(1, false), 250);
  });
  document.getElementById('gateway-status-filter').addEventListener('change', () => {
    clearTimeout(gatewaySearchTimer);
    document.querySelectorAll('.gateway-check').forEach(box => {
      box.checked = false;
    });
    lastGatewayIds = null;
    lastPairingSignature = null;
    updateGatewaySelection();
    void refreshGatewayList(1, false);
  });
  if (typeof IntersectionObserver === 'function') new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting) && activeGatewayPage < gatewayTotalPages) refreshGatewayList(activeGatewayPage + 1, true);
  }, {
    rootMargin: '400px'
  }).observe(gatewaySentinel);

  /* Page exports used by markup and the shared UI. */
  expose({
    get checkOtaUpdate() {
      return checkOtaUpdate;
    },
    get checkBatchOtaUpdate() {
      return checkBatchOtaUpdate;
    },
    get openBatchOta() {
      return openBatchOta;
    },
    get selectBatchOtaFile() {
      return selectBatchOtaFile;
    },
    get uploadBatchOta() {
      return uploadBatchOta;
    },
    get editSelectedGateways() {
      return editSelectedGateways;
    },
    get updateGatewaySelection() {
      return updateGatewaySelection;
    },
    get selectAllGateways() {
      return selectAllGateways;
    },
    get gatewaySelectionState() {
      return gatewaySelectionState;
    },
    get gatewayBatchState() {
      return gatewayBatchState;
    },
    get gatewaySelectionMatches() {
      return gatewaySelectionMatches;
    },
    get gatewayDeleteTitle() {
      return gatewayDeleteTitle;
    },
    get showEdit() {
      return showEdit;
    },
    get pickOtaFile() {
      return pickOtaFile;
    },
    get selectOtaFile() {
      return selectOtaFile;
    },
    get uploadOta() {
      return uploadOta;
    },
    get setAutoUpdate() {
      return setAutoUpdate;
    },
    get cancelEdit() {
      return cancelEdit;
    },
    get saveGateway() {
      return saveGateway;
    },
    get openGatewayOwner() {
      return openGatewayOwner;
    },
    get submitGatewayOwner() {
      return submitGatewayOwner;
    },
    get gatewayDeleteResolver() {
      return gatewayDeleteResolver;
    },
    set gatewayDeleteResolver(value) {
      gatewayDeleteResolver = value;
    },
    get resolveGatewayDelete() {
      return resolveGatewayDelete;
    },
    get chooseGatewayDelete() {
      return chooseGatewayDelete;
    },
    get sendGatewayDelete() {
      return sendGatewayDelete;
    },
    get deleteGateway() {
      return deleteGateway;
    },
    get cancelGatewayDelete() {
      return cancelGatewayDelete;
    },
    get transferGateway() {
      return transferGateway;
    },
    get renewGatewayPairing() {
      return renewGatewayPairing;
    },
    get deletePairing() {
      return deletePairing;
    },
    get removeSelectedGateways() {
      return removeSelectedGateways;
    },
    get createGateway() {
      return createGateway;
    },
    get showPairing() {
      return showPairing;
    },
    get renewPairing() {
      return renewPairing;
    },
    get closeKey() {
      return closeKey;
    },
    get fallbackCopy() {
      return fallbackCopy;
    },
    get copyKey() {
      return copyKey;
    },
    get systemStatusRefreshing() {
      return systemStatusRefreshing;
    },
    set systemStatusRefreshing(value) {
      systemStatusRefreshing = value;
    },
    get refreshSystemStatus() {
      return refreshSystemStatus;
    },
    get STATUS_TEXT() {
      return STATUS_TEXT;
    },
    get gatewaysRefreshing() {
      return gatewaysRefreshing;
    },
    set gatewaysRefreshing(value) {
      gatewaysRefreshing = value;
    },
    get lastGatewayIds() {
      return lastGatewayIds;
    },
    set lastGatewayIds(value) {
      lastGatewayIds = value;
    },
    get gatewayStructureChanged() {
      return gatewayStructureChanged;
    },
    set gatewayStructureChanged(value) {
      gatewayStructureChanged = value;
    },
    get latestGatewayStates() {
      return latestGatewayStates;
    },
    set latestGatewayStates(value) {
      latestGatewayStates = value;
    },
    get gatewayRefreshFailed() {
      return gatewayRefreshFailed;
    },
    set gatewayRefreshFailed(value) {
      gatewayRefreshFailed = value;
    },
    get applyGatewayStates() {
      return applyGatewayStates;
    },
    get refreshGateways() {
      return refreshGateways;
    },
    get gatewayActionMarkup() {
      return gatewayActionMarkup;
    },
    get gatewaySentinel() {
      return gatewaySentinel;
    },
    get activeGatewayPage() {
      return activeGatewayPage;
    },
    set activeGatewayPage(value) {
      activeGatewayPage = value;
    },
    get gatewayPageSize() {
      return gatewayPageSize;
    },
    get gatewayTotalPages() {
      return gatewayTotalPages;
    },
    set gatewayTotalPages(value) {
      gatewayTotalPages = value;
    },
    get gatewayListRequest() {
      return gatewayListRequest;
    },
    set gatewayListRequest(value) {
      gatewayListRequest = value;
    },
    get gatewayListLoading() {
      return gatewayListLoading;
    },
    set gatewayListLoading(value) {
      gatewayListLoading = value;
    },
    get gatewayListPending() {
      return gatewayListPending;
    },
    set gatewayListPending(value) {
      gatewayListPending = value;
    },
    get refreshGatewayList() {
      return refreshGatewayList;
    },
    get gatewaySearchTimer() {
      return gatewaySearchTimer;
    },
    set gatewaySearchTimer(value) {
      gatewaySearchTimer = value;
    }
  });
});
/* page-scope:end */
