/* Shared asynchronous dialogs and system-following color theme. */
(() => {
  if (typeof window === 'undefined' || typeof window.fetch !== 'function') return;
  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    const method = String(init.method || input.method || 'GET').toUpperCase();
    const mutation = url.origin === location.origin && !['GET', 'HEAD', 'OPTIONS'].includes(method);
    if (mutation) window.WolPage?.invalidate();
    if (url.origin === location.origin && !['GET', 'HEAD', 'OPTIONS'].includes(method) && !['/login', '/setup'].includes(url.pathname)) {
      const headers = new Headers(init.headers || input.headers);
      const csrf = document.cookie.split('; ').find(value => value.startsWith('wol_csrf='))?.slice('wol_csrf='.length);
      if (csrf) headers.set('X-CSRF-Token', csrf);
      const revisions = window.WolSync?.headers(url, init.body, init.wolVersions) || '{}';
      if (revisions.length > 6000 && (init.body instanceof URLSearchParams || init.body instanceof FormData)) {
        const body = init.body instanceof URLSearchParams ? new URLSearchParams(init.body) : new FormData();
        if (init.body instanceof FormData)
          for (const [key, value] of init.body) body.append(key, value);
        body.set('_wol_revisions', revisions);
        init = {
          ...init,
          body
        };
        headers.delete('X-WOL-Revisions');
      } else headers.set('X-WOL-Revisions', revisions);
      init = {
        ...init,
        headers
      };
    }
    let response = await originalFetch(input, init);
    if (mutation) window.WolPage?.invalidate();
    // Retry only transient reads. Mutations and power commands must never replay.
    if (url.origin === location.origin && method === 'GET' && [502, 503, 504].includes(response.status) && !init.signal?.aborted) {
      await new Promise(resolve => setTimeout(resolve, 400));
      if (!init.signal?.aborted) response = await originalFetch(input, init);
    }
    if (response.status === 401 && url.origin === location.origin && !['/login', '/setup'].includes(url.pathname)) {
      location.assign('/login');
    }
    if (response.status === 423 && url.origin === location.origin && url.pathname !== '/login') {
      location.assign('/account-disabled?next=' + encodeURIComponent(location.pathname + location.search));
    }
    if (response.status === 404 && url.origin === location.origin && /^\/api\/gateway\//.test(url.pathname)) window.WolPage?.navigate('/');
    const readJSON = response.json.bind(response);
    response.json = async () => {
      const data = await readJSON();
      if (!response.ok && data && !data.msg && typeof data.detail === 'string') data.msg = data.detail;
      window.WolSync?.observe(url, data);
      return data;
    };
    return response;
  };
})();

// Suspension keeps the session, but every protected endpoint denies operations.
document.addEventListener('DOMContentLoaded', () => {
  let checking = false;
  async function checkAccount() {
    if (checking || document.hidden || !document.getElementById('nav-more') || document.querySelector('.account-disabled')) return;
    checking = true;
    try {
      const response = await fetch('/api/account/status', {
        cache: 'no-store'
      });
      const data = await response.json();
      if (response.ok) window.WolPermissions?.update(data);
      if (response.ok && !data.enabled) location.assign('/account-disabled?next=' + encodeURIComponent(location.pathname + location.search));
    } catch (_) {} finally {
      checking = false;
    }
  }
  setInterval(checkAccount, 3000);
  window.addEventListener('focus', checkAccount);
  document.addEventListener('visibilitychange', checkAccount);
});

async function logoutAdmin() {
  if (!await uiConfirm('确认退出？下次访问需要重新登录。', {
      confirmText: '退出'
    })) return;
  const response = await fetch('/logout', {
    method: 'POST'
  });
  if (response.ok) location.assign('/login');
  else showToast('退出失败，请刷新后重试', {
    error: true
  });
}

async function openAccountPassword() {
  const form = document.getElementById('account-password-form');
  if (!form || form.dataset.saving) return;
  form.reset();
  openDialog('account-password-dialog');
  const submit = form.querySelector('[type=submit]');
  submit.disabled = true;
  try {
    const response = await fetch('/api/account', {
      cache: 'no-store'
    });
    const data = await response.json();
    if (!response.ok || !data.account) throw new Error(data.msg || '账号读取失败');
    const dialog = document.getElementById('account-password-dialog');
    dialog.dataset.syncKeys = 'user:' + data.account.public_id;
    window.WolSync.freeze(dialog);
    submit.disabled = false;
  } catch (error) {
    showToast(error.message, {
      error: true
    });
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const wrapper = document.getElementById('nav-more');
  if (!wrapper) return;
  const trigger = document.getElementById('nav-more-trigger');
  const menu = document.getElementById('nav-more-menu');
  const items = [...menu.querySelectorAll('[role=menuitem]')].filter(item => !item.hidden);
  let motion;

  function close() {
    if (trigger.getAttribute('aria-expanded') !== 'true') return;
    trigger.setAttribute('aria-expanded', 'false');
    menu.inert = true;
    const closing = motion = {};
    void animateControl(menu, false, 'menu').then(() => {
      if (motion === closing) menu.hidden = true;
    });
  }

  function open(last = false, focusItem = false) {
    document.dispatchEvent(new CustomEvent('select-open', {
      detail: wrapper
    }));
    motion = {};
    menu.hidden = false;
    menu.inert = false;
    trigger.setAttribute('aria-expanded', 'true');
    void animateControl(menu, true, 'menu');
    if (focusItem) items[last ? items.length - 1 : 0]?.focus({
      preventScroll: true
    });
  }
  trigger.addEventListener('click', event => trigger.getAttribute('aria-expanded') === 'true' ? close() : open(false, event.detail === 0));
  trigger.addEventListener('keydown', event => {
    if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
      event.preventDefault();
      open(event.key === 'ArrowUp', true);
      event.stopPropagation();
    }
  });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || trigger.getAttribute('aria-expanded') !== 'true') return;
    event.preventDefault();
    close();
    trigger.focus();
  });
  wrapper.addEventListener('keydown', event => {
    if (menu.hidden) return;
    if (event.key === 'Tab') setTimeout(() => {
      if (!wrapper.contains(document.activeElement)) close();
    }, 0);
    const index = items.indexOf(document.activeElement);
    let next;
    if (event.key === 'ArrowDown') next = (index + 1) % items.length;
    if (event.key === 'ArrowUp') next = (index + items.length - 1) % items.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = items.length - 1;
    if (next !== undefined) {
      event.preventDefault();
      items[next]?.focus();
    }
  });
  document.addEventListener('pointerdown', event => {
    if (!wrapper.contains(event.target)) close();
  });
  document.addEventListener('select-open', event => {
    if (event.detail !== wrapper) close();
  });
  document.addEventListener('wol:page-loaded', close);
  document.getElementById('account-password-trigger')?.addEventListener('click', () => {
    close();
    openAccountPassword();
  });
  document.getElementById('logout-button').addEventListener('click', () => {
    close();
    void logoutAdmin();
  });
});

async function openDownloads() {
  openDialog('downloads-dialog');
  const list = document.getElementById('downloads-list');
  if (list.dataset.loading) return;
  list.dataset.loading = 'true';
  list.textContent = t('正在读取版本…');
  try {
    const response = await fetch('/api/releases', {
      cache: 'no-store'
    });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error('版本读取失败，请重试');
    list.replaceChildren(...result.releases.map(item => {
      const row = document.createElement('div');
      row.className = 'download-release';
      const heading = document.createElement('div');
      heading.className = 'download-release-heading';
      const name = document.createElement('strong');
      name.textContent = t(item.name);
      const version = document.createElement('span');
      version.dataset.noI18n = '';
      version.textContent = 'v' + item.version;
      heading.append(name, version);
      row.append(heading);
      if (item.url) {
        const link = document.createElement('a');
        link.href = item.url;
        link.download = '';
        link.className = 'icon-button gray';
        link.title = t('下载') + ' ' + t(item.name);
        link.setAttribute('aria-label', link.title);
        link.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${window.WolActionPaths['下载']}"/></svg>`;
        row.append(link);
      }
      return row;
    }));
  } catch (error) {
    list.textContent = t(error.message);
    showToast(error.message, {
      error: true
    });
  } finally {
    delete list.dataset.loading;
  }
}

// Cancellation outlives the scan page and stays serialized per gateway.
window.WolScans = (() => {
  const tasks = new Map();

  function stop(gateway, id, startRequest = null) {
    const previous = tasks.get(gateway)?.promise || Promise.resolve();
    const task = {
      id,
      startRequest
    };
    task.promise = previous.catch(() => {}).then(async () => {
      if (!task.id && startRequest) {
        const command = await startRequest;
        if (!command.ok) return;
        task.id = command.cmd_id;
      }
      if (!task.id) return;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 12000);
      try {
        const response = await fetch(`/gateway/${gateway}/scan/cancel`, {
          method: 'POST',
          body: new URLSearchParams({
            cmd_id: task.id
          }),
          signal: controller.signal,
          keepalive: true
        });
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.msg || '取消扫描失败，请重试');
      } finally {
        clearTimeout(timer);
      }
    });
    tasks.set(gateway, task);
    task.promise.catch(() => {});
    return task.promise;
  }
  window.addEventListener('pagehide', () => {
    if (window.scanInProgress) window.stopPreviousScan?.(window.activeScanId, window.scanStartRequest)?.catch(() => {});
  });
  return {
    stop,
    setTimeout: window.setTimeout.bind(window),
    clearTimeout: window.clearTimeout.bind(window),
    pending: gateway => tasks.get(gateway)?.promise || Promise.resolve(),
    retry: gateway => {
      const task = tasks.get(gateway);
      return task ? stop(gateway, task.id, task.startRequest) : Promise.resolve();
    }
  };
})();

document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('account-password-form');
  if (!form) return;
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (form.dataset.saving) return;
    const data = new FormData(form);
    if (data.get('password') !== data.get('confirm_password')) {
      showToast('两次输入的密码不一致', {
        error: true
      });
      return;
    }
    form.dataset.saving = 'true';
    const submit = form.querySelector('[type=submit]');
    submit.disabled = true;
    try {
      const response = await fetch('/api/account', {
        method: 'POST',
        body: data
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '操作失败');
      closeDialog('account-password-dialog');
      await uiAlert('密码已修改，请重新登录');
      location.assign('/login');
    } catch (error) {
      showToast(error.message, {
        error: true
      });
    } finally {
      delete form.dataset.saving;
      submit.disabled = false;
    }
  });
  document.getElementById('account-password-dialog').addEventListener('close', () => form.reset());
  if (new URLSearchParams(location.search).get('password') === '1') openAccountPassword();
});

const uiEffects = new WeakMap();

function runUIAnimation(node, frames, options) {
  // Flush the newly opened dialog/page before Safari starts its first frame.
  node.getBoundingClientRect();
  if (typeof node.animate === 'function') {
    try {
      const animation = node.animate(frames, options);
      if (animation?.finished && typeof animation.finished.then === 'function') return animation;
      animation?.cancel();
    } catch (_) {}
  }
  const previous = {
    opacity: node.style.opacity,
    transform: node.style.transform,
    transition: node.style.transition
  };
  let finish;
  const finished = new Promise(resolve => {
    finish = resolve;
  });
  let timer, frame;
  let cancelled = false;
  const cancel = () => {
    if (cancelled) return;
    cancelled = true;
    clearTimeout(timer);
    cancelAnimationFrame(frame);
    Object.assign(node.style, previous);
    finish();
  };
  node.style.transition = 'none';
  for (const key of ['opacity', 'transform'])
    if (frames[0][key] !== undefined) node.style[key] = frames[0][key];
  node.getBoundingClientRect();
  frame = requestAnimationFrame(() => {
    if (cancelled) return;
    node.style.transition = `opacity ${options.duration}ms ease, transform ${options.duration}ms ${options.easing}`;
    const last = frames[frames.length - 1];
    for (const key of ['opacity', 'transform'])
      if (last[key] !== undefined) node.style[key] = last[key];
    timer = setTimeout(finish, options.duration);
  });
  return {
    finished,
    cancel
  };
}

function animateControl(node, entering, kind = 'dialog') {
  const previous = uiEffects.get(node);
  previous?._surfaceMotion?.cancel();
  previous?.cancel();
  uiEffects.delete(node);
  if (!node) return Promise.resolve();
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduced) return Promise.resolve();
  const offset = kind === 'menu' ? -4 : kind === 'toast' ? -8 : 8;
  const frames = kind === 'dialog' ? [{
    opacity: 0
  }, {
    opacity: 1
  }] : [{
    opacity: 0,
    transform: `translateY(${offset}px) scale(.98)`
  }, {
    opacity: 1,
    transform: 'translateY(0) scale(1)'
  }];
  const animation = runUIAnimation(node, entering ? frames : [...frames].reverse(), {
    duration: entering ? (kind === 'menu' ? 140 : kind === 'toast' ? 180 : 260) : 120,
    easing: 'cubic-bezier(.2,.7,.3,1)',
    fill: 'both'
  });
  if (kind === 'dialog') {
    const surface = node.querySelector(':scope > .dialog-surface');
    if (surface) animation._surfaceMotion = runUIAnimation(surface, entering ? [{
      transform: 'translateY(18px) scale(.94)',
      offset: 0
    }, {
      transform: 'translateY(-2px) scale(1.012)',
      offset: .72
    }, {
      transform: 'translateY(0) scale(1)',
      offset: 1
    }] : [{
      transform: 'translateY(0) scale(1)'
    }, {
      transform: 'translateY(8px) scale(.97)'
    }], {
      duration: entering ? 260 : 120,
      easing: 'cubic-bezier(.2,.7,.3,1)',
      fill: 'both'
    });
  }
  uiEffects.set(node, animation);
  return animation.finished.catch(() => {}).finally(() => {
    if (uiEffects.get(node) === animation) uiEffects.delete(node);
    animation._surfaceMotion?.cancel();
    animation.cancel();
  });
}

let dialogPointerTarget;
document.documentElement.dataset.inputMethod = 'pointer';
document.addEventListener('pointerdown', event => {
  document.documentElement.dataset.inputMethod = 'pointer';
  dialogPointerTarget = event.target.closest?.('button,a[href],input,select,textarea,[tabindex]');
}, true);
document.addEventListener('keydown', event => {
  if (['Tab', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(event.key)) {
    document.documentElement.dataset.inputMethod = 'keyboard';
  }
}, true);
document.addEventListener('close', event => {
  const dialog = event.target;
  if (!(dialog instanceof HTMLDialogElement) || dialog.open) return;
  const target = dialog._returnFocus;
  const samePage = dialog._pageRoot === document.querySelector('main');
  const modal = [...document.querySelectorAll('dialog[open]')].pop();
  if (samePage && target?.isConnected && !target.matches(':disabled') && (!modal || modal.contains(target))) {
    target.focus({
      preventScroll: true
    });
  }
  if (samePage) dialog._restorePageScroll?.();
  delete dialog._returnFocus;
  delete dialog._restorePageScroll;
  delete dialog._pageRoot;
}, true);

function openDialog(id) {
  window.WolTooltip?.close();
  const dialog = document.getElementById(id);
  if (!dialog.open) {
    dialog._pageRoot = document.querySelector('main');
    const active = document.activeElement;
    dialog._returnFocus = document.documentElement.dataset.inputMethod === 'pointer' && dialogPointerTarget?.isConnected ? dialogPointerTarget : active;
    const x = scrollX,
      y = scrollY,
      ancestors = [];
    for (let node = dialog._returnFocus; node instanceof HTMLElement; node = node.parentElement) {
      if (node.scrollHeight > node.clientHeight || node.scrollWidth > node.clientWidth) {
        ancestors.push([node, node.scrollLeft, node.scrollTop]);
      }
    }
    dialog._restorePageScroll = () => {
      for (const [node, left, top] of ancestors) {
        if (node.isConnected) {
          node.scrollLeft = left;
          node.scrollTop = top;
        }
      }
      if (scrollX !== x || scrollY !== y) scrollTo(x, y);
    };
    window.WolSync?.seed();
    window.WolSync?.freeze(dialog);
  }
  if (dialog.classList.contains('ui-dialog') && !dialog.querySelector(':scope > .dialog-surface')) {
    const children = [...dialog.children].filter(child => !child.classList.contains('toast-region'));
    if (children.length === 1 && children[0].tagName === 'FORM') children[0].classList.add('dialog-surface');
    else {
      const surface = document.createElement('div');
      surface.className = 'dialog-surface';
      surface.append(...children);
      dialog.prepend(surface);
    }
  }
  if (!dialog._motionCancel) {
    dialog._motionCancel = event => {
      if (event.target !== dialog || event.defaultPrevented) return;
      event.preventDefault();
      closeDialog(dialog.id);
    };
    dialog.addEventListener('cancel', dialog._motionCancel);
  }
  delete dialog.dataset.motionClosing;
  // Set the focus destination before showModal performs native autofocus.
  dialog.setAttribute('tabindex', '-1');
  dialog.setAttribute('autofocus', '');
  if (!dialog.open) dialog.showModal();
  // Keep the initial focus on the dialog surface. This avoids a browser
  // focus ring appearing on Cancel/Close while preserving Tab navigation.
  dialog.focus({
    preventScroll: true
  });
  dialog._restorePageScroll?.();
  void animateControl(dialog, true);
}

function closeDialog(id, returnValue) {
  const dialog = document.getElementById(id);
  if (!dialog?.open || dialog.dataset.motionClosing) return;
  const closing = {};
  dialog._closingMotion = closing;
  dialog.dataset.motionClosing = 'true';
  void animateControl(dialog, false).then(() => {
    if (dialog._closingMotion !== closing || !dialog.dataset.motionClosing) return;
    delete dialog.dataset.motionClosing;
    if (returnValue === undefined) dialog.close();
    else dialog.close(returnValue);
  });
}

document.addEventListener('submit', event => {
  const form = event.target;
  if (event.defaultPrevented || String(event.submitter?.formMethod || form.method).toLowerCase() !== 'dialog') return;
  const dialog = form.closest('dialog');
  if (!dialog) return;
  event.preventDefault();
  closeDialog(dialog.id, event.submitter?.value || '');
});
document.addEventListener('close', event => {
  if (event.target.tagName !== 'DIALOG' || event.target.open) return;
  const animation = uiEffects.get(event.target);
  animation?._surfaceMotion?.cancel();
  animation?.cancel();
  delete event.target.dataset.motionClosing;
}, true);

function renderConfirmationTargets(container, targets = []) {
  container.replaceChildren(...targets.map(value => {
    const item = document.createElement('li');
    item.textContent = String(value);
    return item;
  }));
  container.hidden = !targets.length;
}

function describeTargetRow(nameNode) {
  const row = nameNode?.closest('tr');
  return [nameNode?.textContent.trim(), row?.cells[3]?.textContent.trim(), row?.cells[4]?.textContent.trim(), row?.querySelector('.badge')?.textContent.trim()]
    .filter(value => value && value !== '-' && value !== '等待上报').join(' · ');
}

function deviceTargets(ids) {
  return ids.map(id => describeTargetRow(document.getElementById('device-name-' + id)));
}

function showToast(message, options = {}) {
  const region = document.getElementById('toast-region');
  if (!region) return;
  // A regular fixed element also works on iOS versions without Popover support.
  region.removeAttribute('popover');
  if (!region._pending) {
    region._pending = [];
    region._active = new Map();
    region._drain = () => {
      for (const [toast, timers] of region._active) {
        if (toast.parentElement !== region) {
          timers.forEach(clearTimeout);
          region._active.delete(toast);
        }
      }
      while (region.children.length < 3 && region._pending.length) {
        const item = region._pending.shift();
        const toast = document.createElement('div');
        toast.className = 'toast ' + (item.error ? 'error' : item.success ? 'success' : '');
        toast.textContent = t(item.message);
        toast.setAttribute('role', item.error ? 'alert' : 'status');
        const timers = [];
        region._active.set(toast, timers);
        toast.style.animation = 'none';
        region.append(toast);
        void animateControl(toast, true, 'toast');
        timers.push(setTimeout(() => {
          toast.classList.add('toast-leaving');
          void animateControl(toast, false, 'toast').then(() => {
            toast.remove();
            region._active.delete(toast);
            region._drain();
          });
        }, item.duration));
      }
    };
  }
  region._pending.push({
    message: String(message || '操作完成'),
    error: options.error,
    success: options.success,
    duration: Number.isFinite(options.duration) && options.duration > 0 ? options.duration : 3600
  });
  region._drain();
  if (!region._sync) {
    const sync = () => {
      const modal = [...document.querySelectorAll('dialog[open]')].pop();
      const parent = modal || document.body;
      if (region.parentElement !== parent) parent.append(region);
      const viewport = window.visualViewport;
      const height = viewport?.height || window.innerHeight;
      const width = viewport?.width || window.innerWidth;
      region.style.setProperty('--toast-height', height + 'px');
      const toastWidth = Math.max(0, Math.min(380, width - 32));
      const mobile = matchMedia('(max-width: 600px), (pointer: coarse)').matches;
      region.style.width = toastWidth + 'px';
      region.style.left = ((viewport?.offsetLeft || 0) + (mobile ? (width - toastWidth) / 2 : width - toastWidth - 16)) + 'px';
      region.style.top = (viewport?.offsetTop || 0) + 'px';
    };
    region._sync = sync;
    new MutationObserver(() => {
      sync();
      region._drain();
    }).observe(document.body, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ['open']
    });
    window.addEventListener('resize', sync);
    window.visualViewport?.addEventListener('resize', sync);
    window.visualViewport?.addEventListener('scroll', sync);
    document.addEventListener('close', sync, true);
  }
  region._sync();
}

function setSearchAvailable(id, hasData) {
  const form = document.getElementById(id);
  if (!form) return;
  const available = hasData || Boolean(form.querySelector('input')?.value.trim());
  window.setAnimatedHidden?.(form, !available);
  form.dataset.hasData = String(hasData);
  form.querySelectorAll('input,button').forEach(control => {
    control.disabled = !available;
  });
}

// Keep list updates perceptible without delaying data or changing layout.
window.setListSelection = function setListSelection(toolbar, selected) {
  if (!toolbar) return;
  for (const [selector, hidden] of [
      ['[data-toolbar-default]', selected],
      ['[data-toolbar-selected]', !selected]
    ]) {
    const group = toolbar.querySelector(selector);
    if (!group) continue;
    if (group.hidden !== Boolean(hidden)) group.hidden = Boolean(hidden);
    if (group.inert !== Boolean(hidden)) group.inert = Boolean(hidden);
    if (group.getAttribute('aria-hidden') !== String(Boolean(hidden))) group.setAttribute('aria-hidden', String(Boolean(hidden)));
  }
};

window.setAnimatedHidden = function setAnimatedHidden(node, hidden) {
  if (!node) return;
  hidden = Boolean(hidden);
  if (node._animatedHiddenTarget === hidden && (node.hidden === hidden || node._animatedHiddenTimer || node._animatedHiddenFrame)) return;
  node._animatedHiddenTarget = hidden;
  clearTimeout(node._animatedHiddenTimer);
  if (node._animatedHiddenFrame) cancelAnimationFrame(node._animatedHiddenFrame);
  node._animatedHiddenTimer = 0;
  node._animatedHiddenFrame = 0;
  // Toolbar actions swap as a group in a stable area, without delayed reflow.
  if (node.closest('.list-toolbar')) {
    node.hidden = hidden;
    node.classList.remove('is-anim-hidden');
    node.setAttribute('aria-hidden', String(hidden));
    return;
  }
  if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
    clearTimeout(node._animatedHiddenTimer);
    node.hidden = Boolean(hidden);
    node.classList.remove('is-anim-hidden');
    node.setAttribute('aria-hidden', String(Boolean(hidden)));
    return;
  }
  if (node.hidden === hidden && !node.classList.contains('is-anim-hidden')) return;
  node.setAttribute('aria-hidden', String(Boolean(hidden)));
  if (hidden) {
    node.classList.add('is-anim-hidden');
    node._animatedHiddenTimer = setTimeout(() => {
      node._animatedHiddenTimer = 0;
      if (node.classList.contains('is-anim-hidden')) node.hidden = true;
    }, 170);
  } else {
    node.hidden = false;
    node._animatedHiddenFrame = requestAnimationFrame(() => {
      node._animatedHiddenFrame = 0;
      if (node._animatedHiddenTarget === false) node.classList.remove('is-anim-hidden');
    });
  }
};

window.animateTableRow = function animateTableRow(row) {
  if (!row) return;
  clearTimeout(row._motionRemoveTimer);
  row.classList.remove('table-row-exit');
  row.dataset.motionRemoving = 'false';
  if (row.dataset.motionSeen === 'true') return;
  row.dataset.motionSeen = 'true';
  if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
  row.classList.add('table-row-enter');
  row.addEventListener('animationend', () => row.classList.remove('table-row-enter'), {
    once: true
  });
};

window.removeTableRow = function removeTableRow(row) {
  if (!row || row.dataset.motionRemoving === 'true') return;
  row.dataset.motionRemoving = 'true';
  if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
    row.remove();
    return;
  }
  row.classList.add('table-row-exit');
  row._motionRemoveTimer = setTimeout(() => row.remove(), 170);
};
(() => {
  const root = document.documentElement;
  const themeSelect = document.getElementById('theme-select');
  const media = matchMedia('(prefers-color-scheme: dark)');
  const themeKey = 'wol-theme-mode';

  function themeMode() {
    try {
      const mode = localStorage.getItem(themeKey);
      return mode === 'dark' || mode === 'light' ? mode : 'auto';
    } catch (_) {
      return 'auto';
    }
  }

  function refreshTheme() {
    const mode = themeMode();
    const dark = mode === 'dark' || (mode === 'auto' && media.matches);
    root.dataset.theme = dark ? 'dark' : 'light';
    themeSelect.value = mode;
    themeSelect.setAttribute('aria-label', t('主题'));
  }
  themeSelect.addEventListener('change', () => {
    try {
      if (themeSelect.value === 'auto') localStorage.removeItem(themeKey);
      else localStorage.setItem(themeKey, themeSelect.value);
    } catch (_) {}
    refreshTheme();
  });
  media.addEventListener('change', () => {
    if (themeMode() === 'auto') refreshTheme();
  });
  window.addEventListener('storage', event => {
    if (event.key === themeKey) refreshTheme();
  });
  refreshTheme();

  // Keep native selects as the source of truth while presenting a consistent,
  // keyboard-accessible listbox in both color themes.
  const selectInstances = new Map();

  function enhanceSelects() {
    for (const [select, dispose] of selectInstances) {
      if (!select.isConnected) {
        dispose();
        selectInstances.delete(select);
      }
    }
    document.querySelectorAll('select').forEach(select => {
      if (select.dataset.native === 'true' || selectInstances.has(select)) return;
      const controller = new AbortController();
      const eventOptions = {
        signal: controller.signal
      };
      const initialWrapper = select.closest('.custom-select');
      const wrapper = initialWrapper || document.createElement('div');
      wrapper.className = 'custom-select';
      wrapper.dataset.selectId = select.id;
      const trigger = initialWrapper?.querySelector('.select-trigger') || document.createElement('button');
      trigger.type = 'button';
      trigger.className = 'select-trigger gray';
      trigger.id = select.id + '-trigger';
      trigger.setAttribute('aria-haspopup', 'listbox');
      trigger.setAttribute('aria-expanded', 'false');
      trigger.setAttribute('aria-label', select.getAttribute('aria-label') || select.labels?.[0]?.textContent.trim() || select.id);
      const label = initialWrapper?.querySelector('.select-trigger span') || document.createElement('span');
      label.classList.add('select-value');
      if (select.closest('.nav-setting')) {
        trigger.classList.add('nav-icon-trigger');
        trigger.title = select.getAttribute('aria-label');
        if (!initialWrapper) {
          const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
          icon.setAttribute('viewBox', '0 0 24 24');
          icon.setAttribute('aria-hidden', 'true');
          icon.innerHTML = select.id === 'language-select' ?
            '<path d="M3 5h12M9 3v2M5 5c0 6 4 9 8 11M13 5c0 6-4 9-10 11M14 21l4-10 4 10M16 17h4"/>' :
            '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>';
          trigger.append(icon);
        }
        label.classList.add('visually-hidden');
      }
      if (select.id === 'language-select') label.dataset.noI18n = '';
      if (!initialWrapper) {
        const chevron = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        chevron.classList.add('select-chevron');
        chevron.setAttribute('viewBox', '0 0 24 24');
        chevron.setAttribute('aria-hidden', 'true');
        const arrow = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        arrow.setAttribute('d', 'm6 9 6 6 6-6');
        chevron.append(arrow);
        trigger.append(label, chevron);
      }
      const menu = initialWrapper?.querySelector('.select-menu') || document.createElement('div');
      menu.className = 'select-menu';
      menu.id = select.id + '-listbox';
      menu.setAttribute('role', 'listbox');
      menu.setAttribute('aria-labelledby', trigger.id);
      menu.hidden = true;
      trigger.setAttribute('aria-controls', menu.id);
      const searchable = select.dataset.searchable === 'true';
      const optionList = searchable ? document.createElement('div') : menu;
      let searchInput, emptyMessage;
      if (searchable) {
        optionList.className = 'select-options';
        menu.setAttribute('role', 'group');
        optionList.setAttribute('role', 'listbox');
        optionList.setAttribute('aria-labelledby', trigger.id);
        const searchBox = document.createElement('div');
        searchBox.className = 'select-search';
        searchInput = document.createElement('input');
        searchInput.type = 'search';
        searchInput.autocomplete = 'off';
        searchInput.placeholder = t(select.dataset.searchPlaceholder || '搜索用户');
        searchInput.setAttribute('aria-label', searchInput.placeholder);
        searchBox.append(searchInput);
        emptyMessage = document.createElement('div');
        emptyMessage.className = 'select-empty';
        emptyMessage.textContent = t('没有匹配的用户');
        emptyMessage.hidden = true;
        menu.append(searchBox, optionList, emptyMessage);
      }
      if (!initialWrapper) {
        select.before(wrapper);
        wrapper.append(select, trigger, menu);
      }
      select.hidden = true;
      select.tabIndex = -1;
      let options = [],
        signature = '';

      function filterOptions() {
        const query = (searchInput?.value || '').trim().toLocaleLowerCase();
        let found = false;
        options.forEach(button => {
          button.hidden = Boolean(query && !button.textContent.toLocaleLowerCase().includes(query));
          if (!button.hidden && !button.disabled) found = true;
        });
        if (emptyMessage) emptyMessage.hidden = found;
        positionMenu();
      }
      searchInput?.addEventListener('input', filterOptions, eventOptions);

      function setOptionContent(target, option) {
        const text = option?.textContent || '';
        const path = option?.dataset.iconPath || '';
        if (target.textContent === text && (target.querySelector('.select-type-icon path')?.getAttribute('d') || '') === path) return;
        target.textContent = text;
        if (!path) return;
        const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        icon.classList.add('select-type-icon');
        icon.setAttribute('viewBox', '0 0 24 24');
        icon.setAttribute('aria-hidden', 'true');
        const shape = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        shape.setAttribute('d', path);
        icon.append(shape);
        target.prepend(icon);
      }

      function sync() {
        if (searchInput) {
          searchInput.placeholder = t(select.dataset.searchPlaceholder || '搜索用户');
          searchInput.setAttribute('aria-label', searchInput.placeholder);
          emptyMessage.textContent = t('没有匹配的用户');
        }
        const current = JSON.stringify([...select.options].map(option => [option.value, option.textContent, option.disabled, option.hasAttribute('data-no-i18n'), option.dataset.iconPath]));
        if (current !== signature) {
          signature = current;
          options = [...select.options].map(option => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'select-option';
            button.setAttribute('role', 'option');
            button.tabIndex = -1;
            setOptionContent(button, option);
            button.dataset.value = option.value;
            button.disabled = option.disabled;
            if (select.id === 'language-select' || option.hasAttribute('data-no-i18n')) button.dataset.noI18n = '';
            button.addEventListener('click', () => {
              select.value = option.value;
              closeMenu();
              trigger.focus();
              select.dispatchEvent(new Event('change', {
                bubbles: true
              }));
            });
            return button;
          });
          optionList.replaceChildren(...options);
          filterOptions();
        }
        setOptionContent(label, select.dataset.triggerLabel ? {
          textContent: t(select.dataset.triggerLabel),
          dataset: {}
        } : select.selectedOptions[0]);
        label.toggleAttribute('data-no-i18n', !select.dataset.triggerLabel && (select.id === 'language-select' || Boolean(select.selectedOptions[0]?.hasAttribute('data-no-i18n'))));
        trigger.disabled = select.matches(':disabled');
        if (select.hasAttribute('title')) trigger.title = select.title;
        options.forEach(button => button.setAttribute('aria-selected', String(button.dataset.value === select.value)));
        if (trigger.disabled) closeMenu();
      }

      let menuMotion;

      function positionMenu() {
        if (menu.hidden || !menu.classList.contains('select-menu-overlay')) return;
        const rect = trigger.getBoundingClientRect();
        const viewport = window.visualViewport;
        const left = viewport?.offsetLeft || 0;
        const top = viewport?.offsetTop || 0;
        const width = viewport?.width || innerWidth;
        const bottom = top + (viewport?.height || innerHeight);
        const below = Math.max(0, bottom - rect.bottom - 16);
        const above = Math.max(0, rect.top - top - 16);
        const upward = below < Math.min(180, menu.scrollHeight) && above > below;
        menu.style.width = Math.min(Math.max(rect.width, 150), width - 16) + 'px';
        menu.style.maxHeight = Math.min(260, upward ? above : below) + 'px';
        menu.style.left = Math.max(left + 8, Math.min(rect.left, left + width - menu.offsetWidth - 8)) + 'px';
        menu.style.top = (upward ? rect.top - menu.offsetHeight - 8 : rect.bottom + 8) + 'px';
        menu.style.transformOrigin = upward ? 'bottom left' : 'top left';
      }

      function closeMenu() {
        if (menu.hidden || trigger.getAttribute('aria-expanded') !== 'true') return;
        const closing = {};
        menuMotion = closing;
        menu.inert = true;
        trigger.setAttribute('aria-expanded', 'false');
        void animateControl(menu, false, 'menu').then(() => {
          if (menuMotion === closing) {
            menu.hidden = true;
            wrapper.append(menu);
            menu.classList.remove('select-menu-overlay');
            menu.removeAttribute('style');
          }
        });
      }

      function openMenu(keyboard = false) {
        if (searchInput) searchInput.value = '';
        sync();
        filterOptions();
        if (trigger.disabled) return;
        document.dispatchEvent(new CustomEvent('select-open', {
          detail: wrapper
        }));
        const modal = select.closest('dialog');
        (modal || document.body).append(menu);
        menu.classList.add('select-menu-overlay');
        menu.hidden = false;
        menuMotion = {};
        menu.inert = false;
        trigger.setAttribute('aria-expanded', 'true');
        positionMenu();
        void animateControl(menu, true, 'menu');
        const selected = options.find(button => button.dataset.value === select.value && !button.disabled) || options.find(button => !button.disabled);
        if (keyboard) selected?.focus({
          preventScroll: true
        });
        if (selected) menu.scrollTop = Math.max(0, selected.offsetTop - menu.clientHeight / 2);
      }
      trigger.addEventListener('click', event => trigger.getAttribute('aria-expanded') === 'true' ? closeMenu() : openMenu(event.detail === 0), eventOptions);
      trigger.addEventListener('keydown', event => {
        if (trigger.getAttribute('aria-expanded') === 'true' && ['Escape', 'Tab'].includes(event.key)) {
          if (event.key === 'Escape') event.preventDefault();
          closeMenu();
          return;
        }
        if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
          event.preventDefault();
          openMenu(true);
        }
      }, eventOptions);
      menu.addEventListener('keydown', event => {
        if (event.target === searchInput && event.key === 'Enter') {
          event.preventDefault();
          options.find(button => !button.disabled && !button.hidden)?.click();
          return;
        }
        if (event.target === searchInput && !['ArrowDown', 'Escape', 'Tab'].includes(event.key)) return;
        const enabled = options.filter(button => !button.disabled && !button.hidden);
        const index = enabled.indexOf(document.activeElement);
        let next;
        if (event.key === 'ArrowDown') next = (index + 1) % enabled.length;
        if (event.key === 'ArrowUp') next = (index + enabled.length - 1) % enabled.length;
        if (event.key === 'Home') next = 0;
        if (event.key === 'End') next = enabled.length - 1;
        if (next !== undefined) {
          event.preventDefault();
          enabled[next]?.focus();
        }
        if (event.key === 'Escape') {
          event.preventDefault();
          closeMenu();
          trigger.focus();
        }
        if (event.key === 'Tab') {
          closeMenu();
          trigger.focus();
        }
      }, eventOptions);
      document.addEventListener('pointerdown', event => {
        if (!wrapper.contains(event.target) && !menu.contains(event.target)) closeMenu();
      }, eventOptions);
      document.addEventListener('keydown', event => {
        if (trigger.getAttribute('aria-expanded') !== 'true' || !['Escape', 'Tab'].includes(event.key)) return;
        if (event.key === 'Escape') {
          event.preventDefault();
          trigger.focus({
            preventScroll: true
          });
        }
        closeMenu();
      }, eventOptions);
      document.addEventListener('select-open', event => {
        if (event.detail !== wrapper) closeMenu();
      }, eventOptions);
      select.addEventListener('change', sync, eventOptions);
      const selectObserver = new MutationObserver(sync);
      selectObserver.observe(select, {
        subtree: true,
        childList: true,
        characterData: true,
        attributes: true,
        attributeFilter: ['disabled', 'selected', 'label', 'value', 'title', 'data-icon-path', 'data-trigger-label']
      });
      const fieldset = select.closest('fieldset');
      const fieldsetObserver = new MutationObserver(sync);
      if (fieldset) fieldsetObserver.observe(fieldset, {
        attributes: true,
        attributeFilter: ['disabled']
      });
      select.form?.addEventListener('reset', () => queueMicrotask(sync), eventOptions);
      document.addEventListener('close', closeMenu, {
        ...eventOptions,
        capture: true
      });
      document.addEventListener('wol:page-loaded', closeMenu, eventOptions);
      document.addEventListener('wol:language-changed', sync, eventOptions);
      document.addEventListener('scroll', event => {
        if (!menu.contains(event.target)) positionMenu();
      }, {
        ...eventOptions,
        capture: true
      });
      window.addEventListener('resize', positionMenu, eventOptions);
      window.visualViewport?.addEventListener('resize', positionMenu, eventOptions);
      window.visualViewport?.addEventListener('scroll', positionMenu, eventOptions);
      window.addEventListener('storage', sync, eventOptions);
      selectInstances.set(select, () => {
        controller.abort();
        selectObserver.disconnect();
        fieldsetObserver.disconnect();
        menuMotion = null;
        menu.remove();
      });
      sync();
    });
  }
  enhanceSelects();
  document.addEventListener('wol:page-loaded', enhanceSelects);
  document.addEventListener('wol:controls-changed', enhanceSelects);

  const dialog = document.getElementById('ui-dialog');
  let queue = Promise.resolve();

  window.startConfirmationCountdown = (target, buttons, seconds = 3) => {
    const labels = buttons.map(button => button.textContent);
    const until = Date.now() + seconds * 1000;
    let timer;
    let stopped = false;
    const update = () => {
      const left = Math.max(0, Math.ceil((until - Date.now()) / 1000));
      buttons.forEach((button, i) => {
        button.disabled = left > 0;
        button.textContent = labels[i] + (left ? ` (${left}s)` : '');
      });
      target.dataset.confirmWaiting = left ? 'true' : 'false';
      if (!left) clearInterval(timer);
    };
    const stop = () => {
      if (stopped) return;
      stopped = true;
      clearInterval(timer);
      buttons.forEach((button, i) => {
        button.disabled = false;
        button.textContent = labels[i];
      });
      delete target.dataset.confirmWaiting;
    };
    timer = setInterval(update, 100);
    target.addEventListener('close', stop, {
      once: true
    });
    target.querySelector('form')?.addEventListener('submit', event => {
      if (target.dataset.confirmWaiting === 'true' && event.submitter?.value !== 'cancel') event.preventDefault();
    }, {
      signal: (() => {
        const controller = new AbortController();
        target.addEventListener('close', () => controller.abort(), {
          once: true
        });
        return controller.signal;
      })()
    });
    update();
    return stop;
  };

  function show(message, confirmation, options = {}) {
    const result = queue.then(() => new Promise(resolve => {
      window.setDialogSymbol?.(dialog.querySelector('.dialog-symbol'), window.dialogSymbolFor?.(options));
      const title = document.getElementById('dialog-title');
      title.textContent = options.titleText || t(options.title || (confirmation ? '请确认操作' : '操作提示'));
      title.toggleAttribute('data-no-i18n', Boolean(options.titleText));
      document.getElementById('dialog-message').textContent = t(String(message || '操作完成'));
      renderConfirmationTargets(document.getElementById('dialog-targets'), options.targets || []);
      const groups = document.getElementById('dialog-target-groups');
      groups.replaceChildren(...(options.targetGroups || []).map(group => {
        const section = document.createElement('section');
        const heading = document.createElement('h3');
        heading.textContent = t(group.title);
        const list = document.createElement('ul');
        list.className = 'confirmation-targets';
        renderConfirmationTargets(list, group.targets);
        section.append(heading, list);
        return section;
      }));
      groups.hidden = !groups.childElementCount;
      const cancel = document.getElementById('dialog-cancel');
      const ok = document.getElementById('dialog-ok');
      cancel.hidden = !confirmation;
      ok.textContent = t(options.confirmText || (confirmation ? '确认继续' : '知道了'));
      ok.className = options.danger ? 'danger' : '';
      dialog.returnValue = '';
      let stopCountdown;
      dialog.addEventListener('close', () => {
        stopCountdown?.();
        resolve(dialog.returnValue === 'ok');
      }, {
        once: true
      });
      const countdown = confirmation && !options.skipCountdown && (
        options.countdown || options.targets?.length || options.targetGroups?.length ||
        !/^撤销/.test(options.confirmText || '') && /删除|delete/i.test(options.confirmText || '')
      );
      if (countdown) stopCountdown = window.startConfirmationCountdown(dialog, [ok], Number(options.countdown) || 3);
      else ok.disabled = false;
      openDialog('ui-dialog');
    }));
    queue = result.catch(() => {});
    return result;
  }
  window.uiAlert = (message, options) => show(message, false, options);
  window.uiConfirm = (message, options) => show(message, true, options);
  // Catch asynchronous event-handler failures so a failed request is visible.
  window.addEventListener('unhandledrejection', event => {
    event.preventDefault();
    showToast('操作未完成：' + (event.reason?.message || '请求失败，请稍后重试'), {
      error: true
    });
  });
})();

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-toast-error]').forEach(node => showToast(node.dataset.toastError, {
    error: true
  }));
});

async function postForm(url, data, context = {}) {
  const pageURL = location.href;
  const live = context.live || (() => location.href === pageURL);
  try {
    const response = await (context.fetch || fetch)(url, {
      method: 'POST',
      wolVersions: context.wolVersions,
      body: new URLSearchParams(data)
    });
    const result = await response.json();
    if (!live()) return;
    showToast(result.msg || (result.ok ? '操作完成' : '操作失败'), {
      success: result.ok,
      error: !result.ok
    });
    if (result.ok) {
      // Refresh the current list in place so dialogs, search, and selection state survive.
      if (typeof refreshDeviceList === 'function') {
        await refreshDeviceList(typeof activeDevicePage !== 'undefined' ? activeDevicePage : 1, false);
      } else if (typeof refreshGatewayList === 'function') {
        await refreshGatewayList(typeof activeGatewayPage !== 'undefined' ? activeGatewayPage : 1, false);
        if (typeof refreshSystemStatus === 'function') await refreshSystemStatus();
      }
    }
    return result;
  } catch (error) {
    if (live()) showToast('请求失败：' + error.message, {
      error: true
    });
  }
}
