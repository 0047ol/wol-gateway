/* Shared title enhancement, including touch, keyboard and modal top layers. */
(() => {
  if (window.WolTooltip) return;
  const selector = '[title],[data-tooltip]';
  const actions = 'button,a[href],input,select,textarea,[contenteditable=true],[role=button],[role=tab],[role=option]';
  const tip = document.createElement('div');
  tip.id = 'ui-tooltip';
  tip.className = 'ui-tooltip';
  tip.setAttribute('role', 'tooltip');
  tip.setAttribute('data-no-i18n', '');
  tip.hidden = true;
  if (typeof tip.showPopover === 'function') tip.setAttribute('popover', 'manual');
  document.body.append(tip);

  let active, pinned = false,
    hoverTimer, leaveTimer, frame;
  let pointerInput = false,
    pointerStart;

  function text(element) {
    return element?.getAttribute('data-tooltip')?.trim() || '';
  }

  function passive(element) {
    const control = element.closest(actions);
    return !control || control.matches(':disabled,[aria-disabled=true]');
  }

  function enhance(element) {
    if (!(element instanceof Element) || element.closest('head,script,style,[data-native-title]')) return;
    // Keep title as the authoring convention; remove the native popup at runtime.
    if (element.hasAttribute('title')) {
      const value = element.getAttribute('title');
      element.removeAttribute('title');
      if (value.trim()) element.setAttribute('data-tooltip', value);
      else element.removeAttribute('data-tooltip');
    }
    if (text(element)) {
      if (!element.matches(actions) && !element.hasAttribute('tabindex') && !element.closest('[aria-hidden=true]')) {
        element.tabIndex = 0;
        element.dataset.tooltipFocusable = 'true';
      }
    } else if (element.dataset.tooltipFocusable) {
      if (element.getAttribute('tabindex') === '0') element.removeAttribute('tabindex');
      delete element.dataset.tooltipFocusable;
    }
  }

  function refresh(root = document.body) {
    if (root.matches?.(selector)) enhance(root);
    root.querySelectorAll?.(selector).forEach(enhance);
  }

  function describe(element, adding) {
    const ids = new Set((element.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean));
    if (adding) ids.add(tip.id);
    else ids.delete(tip.id);
    if (ids.size) element.setAttribute('aria-describedby', [...ids].join(' '));
    else element.removeAttribute('aria-describedby');
  }

  function close() {
    clearTimeout(hoverTimer);
    clearTimeout(leaveTimer);
    cancelAnimationFrame(frame);
    if (active) describe(active, false);
    active = null;
    pinned = false;
    try {
      tip.hidePopover?.();
    } catch (_) {}
    tip.hidden = true;
    if (tip.parentElement !== document.body) document.body.append(tip);
  }

  function position() {
    frame = null;
    if (!active) return;
    const value = text(active);
    const modal = active.closest('dialog[open]');
    if (!active.isConnected || !value || !active.getClientRects().length || active.closest('[hidden],dialog[data-motion-closing]') || document.querySelector('dialog[open]') && !modal) {
      close();
      return;
    }
    if (tip.textContent !== value) tip.textContent = value;
    const viewport = window.visualViewport;
    const left = viewport?.offsetLeft || 0,
      top = viewport?.offsetTop || 0;
    const width = viewport?.width || innerWidth,
      height = viewport?.height || innerHeight;
    const margin = 12,
      gap = 8;
    const rect = active.getBoundingClientRect();
    if (rect.bottom < top || rect.top > top + height || rect.right < left || rect.left > left + width) {
      close();
      return;
    }
    tip.style.maxWidth = Math.max(0, Math.min(340, width - 2 * margin)) + 'px';
    tip.style.maxHeight = Math.max(0, height - 2 * margin) + 'px';
    const measured = tip.getBoundingClientRect();
    const above = rect.top - top - gap - margin;
    const below = top + height - rect.bottom - gap - margin;
    const useAbove = above >= measured.height || above > below;
    const available = useAbove ? above : below;
    tip.style.maxHeight = Math.max(32, available) + 'px';
    const box = tip.getBoundingClientRect();
    const x = Math.max(left + margin, Math.min(rect.left + (rect.width - box.width) / 2, left + width - margin - box.width));
    const y = Math.max(top + margin, Math.min(useAbove ? rect.top - gap - box.height : rect.bottom + gap, top + height - margin - box.height));
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  }

  function schedulePosition() {
    if (active && !frame) frame = requestAnimationFrame(position);
  }

  function show(element, pin = false) {
    enhance(element);
    if (!text(element)) return;
    clearTimeout(hoverTimer);
    clearTimeout(leaveTimer);
    if (active === element && !tip.hidden) {
      pinned ||= pin;
      position();
      return;
    }
    close();
    active = element;
    pinned = pin;
    const parent = element.closest('dialog[open]') || document.body;
    if (tip.parentElement !== parent) parent.append(tip);
    tip.textContent = text(element);
    tip.hidden = false;
    try {
      tip.showPopover?.();
    } catch (_) {}
    describe(element, true);
    position();
  }

  function target(event) {
    const element = event.target instanceof Element ? event.target.closest(selector) : null;
    if (element) enhance(element);
    return element && text(element) ? element : null;
  }

  function toggle(element) {
    if (active === element && pinned) close();
    else show(element, true);
  }

  function leave() {
    if (!pinned) {
      clearTimeout(leaveTimer);
      leaveTimer = setTimeout(close, 140);
    }
  }

  document.addEventListener('pointerover', event => {
    if (event.pointerType === 'touch') return;
    const element = target(event);
    if (!element || element.contains(event.relatedTarget)) return;
    clearTimeout(hoverTimer);
    clearTimeout(leaveTimer);
    hoverTimer = setTimeout(() => show(element), 300);
  }, true);
  document.addEventListener('pointerout', event => {
    if (event.pointerType === 'touch') return;
    const element = target(event);
    if (!element || element.contains(event.relatedTarget)) return;
    clearTimeout(hoverTimer);
    if (active === element) leave();
  }, true);
  tip.addEventListener('pointerenter', () => clearTimeout(leaveTimer));
  tip.addEventListener('pointerleave', leave);
  document.addEventListener('pointerdown', event => {
    pointerInput = true;
    pointerStart = {
      element: target(event),
      x: event.clientX,
      y: event.clientY
    };
    if (!active?.contains(event.target) && !tip.contains(event.target)) close();
  }, true);
  // Disabled native buttons do not dispatch click consistently, but do emit pointerup.
  document.addEventListener('pointerup', event => {
    const element = target(event);
    if (element?.closest(':disabled') && pointerStart?.element === element && Math.hypot(event.clientX - pointerStart.x, event.clientY - pointerStart.y) < 10) toggle(element);
    pointerStart = null;
  }, true);
  document.addEventListener('pointercancel', () => {
    pointerStart = null;
  }, true);
  document.addEventListener('click', event => {
    if (tip.contains(event.target)) return;
    const element = target(event);
    if (element?.closest(':disabled')) return;
    if (element && passive(element)) toggle(element);
    else close();
  }, true);
  document.addEventListener('focusin', event => {
    const element = target(event);
    if (element && !pointerInput) show(element);
  });
  document.addEventListener('focusout', event => {
    if (active?.contains(event.target) && !active.contains(event.relatedTarget) && !tip.contains(event.relatedTarget)) close();
  });
  document.addEventListener('keydown', event => {
    pointerInput = false;
    if (event.key === 'Escape' && active) {
      close();
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    const element = target(event);
    if (element && passive(element) && ['Enter', ' '].includes(event.key)) {
      event.preventDefault();
      if (!event.repeat) toggle(element);
    }
  }, true);
  document.addEventListener('scroll', event => {
    if (!tip.contains(event.target)) close();
  }, true);
  window.addEventListener('resize', schedulePosition);
  window.visualViewport?.addEventListener('resize', schedulePosition);
  window.visualViewport?.addEventListener('scroll', schedulePosition);
  window.addEventListener('pagehide', close);
  document.addEventListener('close', event => {
    if (event.target.contains?.(active)) close();
  }, true);
  document.addEventListener('wol:page-loaded', close);
  document.addEventListener('wol:language-changed', schedulePosition);
  new MutationObserver(records => {
    let changed = false;
    for (const record of records) {
      if (record.target === tip || tip.contains(record.target)) continue;
      changed = true;
      if (record.type === 'childList') record.addedNodes.forEach(node => {
        if (node.nodeType === Node.ELEMENT_NODE && node !== tip) refresh(node);
      });
      else if (['title', 'data-tooltip'].includes(record.attributeName)) enhance(record.target);
    }
    if (changed) schedulePosition();
  }).observe(document.body, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ['title', 'data-tooltip', 'open', 'hidden', 'class']
  });
  refresh();
  window.WolTooltip = {
    refresh,
    close,
    text
  };
})();
