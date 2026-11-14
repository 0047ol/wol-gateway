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
  /* HASS.Agent matches by MAC. A button press is not proof of OS shutdown. */

  let deviceId = null,
    revision = 0,
    busy = false,
    current = null,
    states = {},
    loading = null,
    refreshPending = false;
  const watchedCommands = new Map();
  const node = id => document.getElementById(id);
  const powerCapable = (id, state) => window.getDeviceRow?.(id)?.power_capable !== false && state?.power_capable !== false;
  const needsLink = state => Boolean(state?.provider === 'native' && state.installed && state.linked === false);
  const nativeReady = state => Boolean(state?.provider === 'native' && state.installed && state.online && !state.upgrade_required);
  const nativeLinked = state => nativeReady(state) && state.linked === true;
  const allowsOffline = state => nativeLinked(state) || (nativeReady(state) && needsLink(state) && state.can_link);
  window.powerNeedsLink = id => needsLink(states[String(id)]);
  window.powerAllowsOffline = id => Boolean(allowsOffline(states[String(id)]));

  function powerButtonAction(id) {
    const state = states[String(id)];
    if (window.deviceGatewayCanManage === false) return 'disabled';
    if (window.deviceGatewayDeleting || !powerCapable(id, state)) return 'disabled';
    if (window.deviceGatewayOnline === false) {
      if (nativeLinked(state)) return 'shutdown';
      return needsLink(state) && state.can_link && nativeReady(state) ? 'link' : 'disabled';
    }
    // A discovered but unlinked service must not replace an offline PC's wake action.
    if (nativeLinked(state)) return 'shutdown';
    const badge = node('dev-status-' + id);
    const status = badge?.dataset?.powerStatus || (badge?.classList.contains('online') ? 'online' : badge?.classList.contains('offline') ? 'offline' : 'unknown');
    if (status === 'offline') return 'wake';
    if (needsLink(state)) return 'link';
    if (status !== 'online' || !state || state.provider === 'native' && state.linked === true) return 'disabled';
    return state.installed ? 'shutdown' : 'setup';
  }
  window.powerButtonAction = powerButtonAction;

  function deviceReason(state, id) {
    const badge = node('dev-status-' + id);
    if (badge?.dataset?.powerStatus) return badge.dataset.powerStatus === 'online' ? '' : badge.dataset.powerStatus === 'unknown' ? '电脑状态未知，无法操作电源' : '设备离线，无法关机';
    if (badge) return badge.classList.contains('online') ? '' : badge.classList.contains('unknown') ? (badge.title || '设备状态未知，无法操作电源') : '设备离线，无法关机';
    if (state?.device_online !== undefined) return state.device_online ? '' : '设备离线，无法关机';
    return '正在读取电脑端状态…';
  }

  function reason(state, id) {
    if (window.deviceGatewayCanManage === false) return window.deviceGatewayActionError || '网关状态异常，仅可查看设备和日志';
    if (window.deviceGatewayDeleting) return '网关正在等待删除，请先撤销删除';
    if (window.deviceGatewayOnline === false && !nativeLinked(state)) return '网关离线，无法操作设备';
    if (!powerCapable(id, state)) return '仅电脑和笔记本支持开关机';
    if (!state) return '正在读取电脑端状态…';
    if (state.upgrade_required) return state.error;
    if (!state.installed || !['native', 'hass'].includes(state.provider)) return '当前仅支持Windows电脑，请先安装并运行WOL关机服务。';
    if (needsLink(state)) return '请先确认关联WOL关机服务';
    if (!nativeLinked(state) && deviceReason(state, id)) return deviceReason(state, id);
    if (!state.online) return state.provider === 'native' ? 'WOL关机服务离线，未下发关机指令' : 'HASS.Agent 离线或尚未确认在线，无法关机';
    return '';
  }
  window.refreshPowerButtons = () => {
    document.querySelectorAll('[id^="device-power-"]').forEach(button => {
      const id = button.id.slice('device-power-'.length);
      button.hidden = false;
      const badge = node('dev-status-' + id);
      const action = powerButtonAction(id);
      const linking = action === 'link' || action === 'disabled' && needsLink(states[id]);
      const capable = powerCapable(id, states[id]);
      button.disabled = !capable || action === 'disabled' || action === 'link' && !states[id]?.can_link || busy && String(deviceId) === id;
      button.classList.remove('danger');
      button.classList.toggle('gray', action !== 'shutdown');
      window.setActionIcon?.(button, !capable ? '电源操作' : linking ? '关联WOL关机服务' : action === 'wake' ? '开机' : action === 'setup' ? '配置WOL关机服务' : '关机');
      button.dataset.action = action;
      window.WolPermissions?.apply();
      button.title = t(window.deviceGatewayActionError || (window.deviceGatewayDeleting ? '网关正在等待删除，请先撤销删除' : !capable ? '仅电脑和笔记本支持开关机' : action === 'link' && states[id]?.can_link ? '确认关联WOL关机服务' : action === 'link' || linking ? 'WOL关机服务未在线，请启动服务后重试关联' : action === 'wake' ? '开机' : action === 'setup' ? '未配置关机服务，点击查看配置方法' : action === 'shutdown' ? '关机' : window.deviceGatewayOnline === false ? '网关离线，无法操作设备' : badge?.getAttribute('data-tooltip') || badge?.title || '设备状态未知，无法操作电源'));
    });
    window.refreshBatchPower?.();
  };

  function controls() {
    const needsInstall = Boolean(current && !current.installed && powerCapable(deviceId, current));
    const linking = needsLink(current);
    const unavailable = Boolean(current && reason(current, deviceId));
    node('power-shutdown').disabled = busy || Boolean(reason(current, deviceId));
    node('power-shutdown').hidden = !current?.installed || unavailable;
    const symbol = node('power-symbol');
    // Keep the same title structure for install, link and shutdown states.
    // Hiding the symbol for unavailable states made the mobile dialogs jump.
    if (symbol) symbol.hidden = false;
    window.setDialogSymbol?.(symbol, linking ? '关联WOL关机服务' : needsInstall ? '配置WOL关机服务' : unavailable ? '警告' : '关机');
    const close = node('power-close');
    if (close) close.textContent = t(!needsInstall && unavailable ? '我知道了' : '取消');
    const download = node('power-download');
    if (download) download.hidden = !needsInstall;
    const link = node('power-link');
    if (link) {
      link.hidden = !linking;
      link.disabled = busy || window.deviceGatewayDeleting || !current?.can_link || (window.deviceGatewayOnline === false && !allowsOffline(current));
    }
    if (linking && close) close.textContent = t('取消');
    node('power-title').textContent = linking ? t('关联WOL关机服务') : current?.installed ? t('关闭{名称}').replace('{名称}', node('device-name-' + deviceId)?.textContent || '') : t('未安装WOL关机服务');
    node('power-title').toggleAttribute('data-no-i18n', Boolean(current?.installed && !linking));
    window.refreshPowerButtons();
  }

  function downloadService() {
    const link = document.createElement('a');
    link.href = '/downloads/pc-service.exe';
    link.download = 'WolPCService.exe';
    document.body.append(link);
    link.click();
    link.remove();
  }
  window.closePowerManager = () => {
    closeDialog('power-dialog');
  };
  window.downloadPowerService = () => {
    if (!current || current.installed || !powerCapable(deviceId, current)) return;
    downloadService();
    closeDialog('power-dialog');
  };

  function refresh() {
    if (loading) {
      refreshPending = true;
      return loading;
    }
    loading = loadStates().finally(() => {
      loading = null;
      if (refreshPending) {
        refreshPending = false;
        void refresh();
      }
    });
    return loading;
  }
  window.refreshPowerStates = refresh;
  async function loadStates() {
    try {
      const ids = [...new Set([...(window.deviceRows || []).map(row => String(row.id)), ...watchedCommands.keys(), ...(deviceId === null ? [] : [String(deviceId)])])];
      if (!ids.length) return true;
      const response = await fetch(`/api/gateway/${node('power-dialog').dataset.gatewayId}/power?ids=${encodeURIComponent(ids.join(','))}`, {
        cache: 'no-store',
        signal: AbortSignal.timeout(8000)
      });
      if (!response.ok) throw new Error('电脑端状态读取失败');
      states = await response.json();
      for (const [id, commandId] of watchedCommands) {
        const command = states[id]?.command;
        if (!command || command.id !== commandId) continue;
        if (['submitted', 'failed', 'rejected', 'unconfirmed', 'uncertain'].includes(command.status)) {
          showToast(command.message + (Number.isInteger(command.exit_code) ? ' (' + command.exit_code + ')' : ''), {
            success: command.status === 'submitted',
            error: command.status !== 'submitted',
            duration: 8000
          });
          watchedCommands.delete(id);
        }
      }
      window.refreshPowerButtons();
      if (deviceId === null || !node('power-dialog').open) return true;
      current = states[String(deviceId)] || null;
      if (!powerCapable(deviceId, current)) {
        closeDialog('power-dialog');
        return true;
      }
      node('power-state').textContent = t(needsLink(current) ? current.upgrade_required ? current.error : current.can_link ? window.deviceGatewayOnline === false ? '网关离线，设备状态未知。已发现同一 MAC 的WOL关机服务在线；确认关联后才能由服务判断在线并关机。' : '已发现此设备的WOL关机服务。确认关联后由服务判断在线状态，并允许网页关机。' : 'WOL关机服务未在线，请启动服务后重试关联' : reason(current, deviceId) || '确认立即关机选中的在线电脑？请先保存工作。');
      node('power-shutdown').title = t(reason(current, deviceId));
      controls();
      return true;
    } catch (error) {
      states = {};
      window.refreshPowerButtons();
      if (deviceId !== null && node('power-dialog').open) {
        current = null;
        node('power-state').textContent = t(error.message);
        controls();
      }
      return false;
    }
  }
  window.openPowerManager = async id => {
    // The first click may arrive before the initial service-status request completes.
    if (!states[String(id)] && !await refresh()) return;
    if (!['link', 'shutdown', 'setup'].includes(powerButtonAction(id))) return;
    if (busy || !powerCapable(id, states[String(id)])) return;
    const why = deviceReason(states[String(id)], id);
    if (why && !needsLink(states[String(id)]) && !nativeLinked(states[String(id)])) {
      showToast(why, {
        error: true
      });
      return;
    }
    deviceId = id;
    revision++;
    current = states[String(id)] || null;
    node('power-dialog').dataset.syncKeys = `gateway:${node('power-dialog').dataset.gatewayId} device:${id}`;
    node('power-state').textContent = t('正在读取电脑端状态…');
    controls();
    openDialog('power-dialog');
    await refresh();
  };
  window.linkPowerService = async () => {
    if (window.deviceGatewayDeleting || (window.deviceGatewayOnline === false && !allowsOffline(current))) return;
    if (busy || deviceId === null || !powerCapable(deviceId, current) || !needsLink(current) || !current.can_link) return;
    const id = deviceId,
      request = revision;
    busy = true;
    controls();
    try {
      if (!await refresh() || revision !== request || deviceId !== id || !node('power-dialog').open) return;
      if (window.deviceGatewayDeleting || (window.deviceGatewayOnline === false && !allowsOffline(current))) throw new Error('服务关联状态已变化，请刷新后重试');
      if (!needsLink(current) || !current.can_link || powerButtonAction(id) !== 'link') throw new Error('服务关联状态已变化，请刷新后重试');
      const response = await fetch(`/gateway/${node('power-dialog').dataset.gatewayId}/devices/${id}/windows-service`, {
        method: 'POST',
        body: new URLSearchParams({
          action: 'bind'
        }),
        signal: AbortSignal.timeout(15000)
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.detail || result.msg || '操作失败');
      showToast(result.msg, {
        success: true
      });
      closeDialog('power-dialog');
      await window.refreshDevices?.();
      await refresh();
    } catch (error) {
      showToast(error.message || '操作失败', {
        error: true
      });
    } finally {
      busy = false;
      controls();
    }
  };
  window.powerAction = async action => {
    if (busy || deviceId === null || action !== 'shutdown') return;
    const id = deviceId,
      request = revision;
    const why = reason(current, id);
    if (why) {
      showToast(why, {
        error: true
      });
      return;
    }
    busy = true;
    controls();
    try {
      if (!await refresh() || revision !== request || deviceId !== id || !node('power-dialog').open) return;
      const refreshedReason = reason(current, id);
      if (refreshedReason || powerButtonAction(id) !== 'shutdown') throw new Error(refreshedReason || '设备状态已变化，请重新确认');
      const base = `/gateway/${node('power-dialog').dataset.gatewayId}/devices/${id}`;
      const response = await fetch(`${base}/shutdown`, {
        method: 'POST',
        cache: 'no-store',
        signal: AbortSignal.timeout(15000)
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.detail || result.msg || '操作失败');
      showToast(result.msg, {
        success: true
      });
      if (result.cmd_id) watchedCommands.set(String(id), result.cmd_id);
      closeDialog('power-dialog');
      await refresh();
    } catch (error) {
      showToast(error.message || '操作失败', {
        error: true
      });
    } finally {
      busy = false;
      controls();
    }
  };
  window.shutdownDeviceBatch = async ids => {
    if (busy || ids.some(id => !powerCapable(id, states[id]))) return;
    busy = true;
    controls();
    try {
      if (!window.batchSelectionMatches?.(ids, true)) return;
      if (!await refresh()) {
        showToast('电脑端状态读取失败', {
          error: true
        });
        return;
      }
      if (!window.batchSelectionMatches?.(ids, true)) {
        showToast('设备选择或状态已变化，请重新确认', {
          error: true
        });
        return;
      }
      if (ids.some(id => !powerCapable(id, states[id]))) return;
      const ready = ids.filter(id => !reason(states[id], id));
      const unavailable = ids.filter(id => reason(states[id], id));
      const skippedGroup = {
        title: `暂不可关机（${unavailable.length} 台）`,
        targets: unavailable.map(id => deviceTargets([id])[0] + '\n' + t(reason(states[id], id)))
      };
      if (!ready.length) {
        const download = await uiConfirm('选中的电脑均暂不可关机，请按列表中的原因完成配置或等待上线。', {
          title: '无法批量关机',
          confirmText: '下载安装服务',
          skipCountdown: true,
          targetGroups: [skippedGroup]
        });
        if (download) downloadService();
        return;
      }
      const partial = unavailable.length > 0;
      const options = {
        title: '确认关机',
        confirmText: partial ? `关机（${ready.length} 台）` : '关机'
      };
      if (partial) options.targetGroups = [{
        title: `可关机（${ready.length} 台）`,
        targets: deviceTargets(ready)
      }, skippedGroup];
      else options.targets = deviceTargets(ready);
      if (!await uiConfirm(partial ? '仅对可关机列表中的电脑发送关机指令，其余设备将跳过。请先保存工作。' : '确认立即关机选中的在线电脑？请先保存工作。', options)) return;
      if (!await refresh()) {
        showToast('电脑端状态读取失败', {
          error: true
        });
        return;
      }
      if (!window.batchSelectionMatches?.(ids, true) || ready.some(id => reason(states[id], id))) {
        showToast('设备选择或状态已变化，请重新确认', {
          error: true
        });
        return;
      }
      const results = [];
      for (const id of ready) {
        const why = reason(states[id], id);
        if (why) {
          results.push({
            id,
            error: why
          });
          continue;
        }
        try {
          const response = await fetch(`/gateway/${node('power-dialog').dataset.gatewayId}/devices/${id}/shutdown`, {
            method: 'POST',
            cache: 'no-store',
            signal: AbortSignal.timeout(15000)
          });
          const result = await response.json();
          if (response.ok && result.ok && result.cmd_id) watchedCommands.set(String(id), result.cmd_id);
          results.push({
            id,
            error: response.ok && result.ok ? '' : result.detail || result.msg || '操作失败'
          });
        } catch (error) {
          results.push({
            id,
            error: error.message || '操作失败'
          });
        }
      }
      const failed = results.filter(result => result.error);
      showToast(t('关机指令已发送：') + (results.length - failed.length) + ' / ' + results.length + (partial ? t('；跳过：') + unavailable.length : ''), {
        success: !failed.length,
        error: Boolean(failed.length)
      });
      if (failed.length) await uiAlert(failed.map(result => (node('device-name-' + result.id)?.textContent || '') + ': ' + t(result.error)).join('\n'));
      await refresh();
    } finally {
      busy = false;
      controls();
    }
  };
  document.addEventListener('DOMContentLoaded', () => {
    refresh();
    node('power-dialog')?.addEventListener('close', () => {
      if (node('power-dialog').open) return;
      revision++;
      deviceId = null;
      current = null;
    });
  });
  setInterval(refresh, 5000);

});
/* page-scope:end */
