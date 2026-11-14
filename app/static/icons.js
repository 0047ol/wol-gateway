/* Local SVG icons; accessible names remain available to assistive technology. */
(() => {
  const paths = {
    '添加': 'M12 5v14M5 12h14',
    '归属': 'M15 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M12 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0m4 4h6m-3-3 3 3-3 3',
    '信息': 'M12 11v6m0-10v1M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
    '警告': 'm12 3 10 18H2L12 3Zm0 5v6m0 3v1',
    '配对': 'M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71',
    '下载': 'M12 3v12m-5-5 5 5 5-5M4 15v6h16v-6',
    '远程 OTA 升级': 'M12 16V3m-5 5 5-5 5 5M4 15v6h16v-6',
    '启用': 'm8 5 11 7-11 7V5Z',
    '禁用': 'M8 5v14M16 5v14',
    '网关管理': 'M4 5h16v12H4zM8 21h8M12 17v4M7 9h1m3 0h1m3 0h1',
    '设备管理': 'M4 3h16v11H4zM12 14v4M6 21v-3h12v3M3 21h6m6 0h6',
    '分享': 'm8 11 8-5m-8 7 8 5M8 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0M22 5a3 3 0 1 1-6 0 3 3 0 0 1 6 0m0 14a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
    '搜索': 'm16 16 5 5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
    '删除': 'M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7',
    '撤销删除': 'm9 4-5 5 5 5M4 9h9a7 7 0 0 1 7 7v4',
    '解绑WOL关机服务': 'm3 3 18 18M10 13l-2 2a4 4 0 0 1-6-6l3-3a4 4 0 0 1 5-1m4 6 2-2a4 4 0 0 0-6-6L8 5m3 11a4 4 0 0 0 5-1l3-3',
    '关联WOL关机服务': 'M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71',
    '配置WOL关机服务': 'M4 3h16v14H4zM8 21h8M12 7v6m-3-3h6',
    '子设备': 'M4 3h16v11H4zM12 14v4M6 21v-3h12v3M3 21h6m6 0h6',
    '日志': 'M5 3h14v18H5zM8 7h8M8 11h8m-8 4h5',
    '编辑': 'm15 4 5 5M4 20l5-1L21 7l-5-5L4 14v6Z',
    '权限设置': 'M12 3 3 7v5c0 5 9 9 9 9s9-4 9-9V7l-9-4Zm-4 9 3 3 5-6',
    '开机': 'M12 2v10M5 5a9 9 0 1 0 14 0',
    '关机': 'M12 2v10M5 5a9 9 0 1 0 14 0',
    '电源操作': 'M12 2v10M5 5a9 9 0 1 0 14 0',
    '扫描': 'M4 8V4h4m8 0h4v4m0 8v4h-4M8 20H4v-4M8 12h8m-4-4v8',
    '退出': 'M9 4H4v16h5m6-13 5 5-5 5m-7-5h12',
    '重新生成': 'M20 7V3m0 4h-4M4 17v4m0-4h4M5 8a8 8 0 0 1 13-3l2 2M4 17l2 2a8 8 0 0 0 13-3',
    '转移网关': 'M4 7h16m-5-5 5 5-5 5M20 17H4m5-5-5 5 5 5'
  };
  const english = {
    'Add': '添加',
    'Download': '下载',
    'Enable': '启用',
    'Disable': '禁用',
    'Share': '分享',
    'Search': '搜索',
    'Delete': '删除',
    'Cancel deletion': '撤销删除',
    'Unlink WOL shutdown service': '解绑WOL关机服务',
    'Link WOL shutdown service': '关联WOL关机服务',
    'Configure PC shutdown': '配置WOL关机服务',
    'Devices': '子设备',
    'Sub-devices': '子设备',
    'Logs': '日志',
    'Edit': '编辑',
    'Power on': '开机',
    'Shut down': '关机',
    'Power actions': '电源操作',
    'Scan': '扫描',
    'Clear': '删除',
    '清空': '删除',
    '立即关机': '关机',
    'Shut down now': '关机',
    'Sign out': '退出',
    'Regenerate': '重新生成',
    'Transfer gateway': '转移网关'
  };
  window.WolActionPaths = paths;
  window.setDialogSymbol = (target, label) => {
    if (!target) return;
    const name = paths[label] ? label : '信息';
    if (target.dataset.symbol === name && target.querySelector('svg')) return;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', paths[name]);
    svg.append(path);
    target.dataset.symbol = name;
    target.replaceChildren(svg);
  };
  window.dialogSymbolFor = options => {
    if (paths[options.symbol]) return options.symbol;
    const purpose = [options.title, options.confirmText].filter(Boolean).join(' ');
    const rules = [
      [/OTA|升级/, '远程 OTA 升级'],
      [/解绑/, '解绑WOL关机服务'],
      [/撤销删除/, '撤销删除'],
      [/删除|清空/, '删除'],
      [/转移/, '转移网关'],
      [/重新生成|重置/, '重新生成'],
      [/下载|安装/, '下载'],
      [/添加|创建/, '添加'],
      [/权限/, '权限设置'],
      [/分享/, '分享'],
      [/关联|绑定|配对/, '配对'],
      [/关机|关闭电源/, '关机'],
      [/开机|唤醒/, '开机'],
      [/禁用|停用/, '禁用'],
      [/启用/, '启用'],
      [/密码|编辑|修改|保存/, '编辑'],
      [/退出/, '退出']
    ];
    return rules.find(([pattern]) => pattern.test(purpose))?.[1] || (options.danger ? '警告' : '信息');
  };
  window.setActionIcon = (button, label) => {
    if (!button || button.matches('.select-option, .select-trigger, [role="option"]') || button.hasAttribute('data-no-icon') || !paths[label]) return;
    if (button.dataset.iconLabel === label && button.querySelector('svg')) return;
    const translated = typeof t === 'function' ? t(label) : label;
    button.setAttribute('aria-label', translated);
    button.title = translated;
    button.dataset.iconLabel = label;
    button.classList.add('icon-button');
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', paths[label]);
    svg.append(path);
    const text = document.createElement('span');
    text.className = 'visually-hidden';
    text.textContent = translated;
    button.replaceChildren(svg, text);
  };

  function render(root) {
    if (!root?.isConnected) return;
    const buttons = root.matches?.('button') ? [root] : root.querySelectorAll?.('button') || [];
    for (const button of buttons) {
      if (button.closest('#ui-dialog') || button.matches('.select-option, .select-trigger, [role="option"]') || button.hasAttribute('data-no-icon')) continue;
      const text = button.textContent.trim();
      const label = paths[text] ? text : english[text];
      if (label) window.setActionIcon(button, label);
    }
  }
  const observer = new MutationObserver(records => {
    for (const record of records) {
      if (record.type === 'characterData') render(record.target.parentElement?.closest('button') || record.target.parentElement);
      else
        for (const node of record.addedNodes) {
          if (node.nodeType === 1) render(node.closest('button') || node);
          else if (node.parentElement?.matches('button')) render(node.parentElement);
        }
    }
  });
  observer.observe(document.body, {
    childList: true,
    subtree: true,
    characterData: true
  });
  render(document.body);
})();
