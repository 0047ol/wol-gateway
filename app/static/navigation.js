/* Server-rendered pages share one shell; every mounted page owns its effects. */
(() => {
  const nativeWindow = window;
  let page;
  let loading = null;
  let committedURL = location.href;
  history.replaceState({
    ...history.state,
    wolIndex: 0
  }, '', location.href);
  let committedState = history.state;
  let queuedNavigation;
  let undoingPop = false;
  const scriptCache = new Map();
  const documentCache = new Map();
  const nativeRead = window.fetch.bind(window);
  const cacheTTL = 5000;
  let prefetchTimer;
  let transition;
  const globals = new Map();
  let scrollTimer;
  if ('scrollRestoration' in history) history.scrollRestoration = 'manual';

  function createPage() {
    const abort = new AbortController();
    const timers = new Set();
    const observers = new Set();
    const listeners = [];
    const names = new Set();
    const ready = [];
    let active = true;
    const live = () => active;
    const publish = (name, descriptor) => {
      Object.defineProperty(nativeWindow, name, {
        configurable: true,
        ...descriptor
      });
      globals.set(name, scope);
      names.add(name);
    };
    const proxyWindow = new Proxy(nativeWindow, {
      get(target, key) {
        if (key === 'window' || key === 'globalThis') return proxyWindow;
        if (key === 'addEventListener') return (type, listener, options) => {
          if (!active) return;
          target.addEventListener(type, listener, options);
          listeners.push(() => target.removeEventListener(type, listener, options));
        };
        if (['removeEventListener', 'dispatchEvent'].includes(key)) return target[key].bind(target);
        if (['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'].includes(key)) return scope[key];
        return Reflect.get(target, key, target);
      },
      set(target, key, value) {
        if (active) {
          const descriptor = Object.getOwnPropertyDescriptor(target, key);
          if (globals.get(key) === scope && descriptor?.set) descriptor.set(value);
          else publish(key, {
            writable: true,
            value
          });
        }
        return true;
      }
    });
    const proxyDocument = new Proxy(document, {
      get(target, key) {
        if (key === 'addEventListener') return (type, listener, options) => {
          if (type === 'DOMContentLoaded') ready.push(listener);
          else {
            target.addEventListener(type, listener, options);
            listeners.push(() => target.removeEventListener(type, listener, options));
          }
        };
        const value = Reflect.get(target, key, target);
        return typeof value === 'function' ? value.bind(target) : value;
      }
    });
    const scopedFetch = async (input, init = {}) => {
      if (!active) return new Promise(() => {});
      const controller = new AbortController();
      const parentSignal = init.signal || (input instanceof Request ? input.signal : null);
      const cancel = () => controller.abort();
      abort.signal.addEventListener('abort', cancel, {
        once: true
      });
      parentSignal?.addEventListener('abort', cancel, {
        once: true
      });
      if (parentSignal?.aborted) cancel();
      try {
        const response = await nativeWindow.fetch(input, {
          ...init,
          signal: controller.signal
        });
        if (!active) return new Promise(() => {});
        // Reading the body is another asynchronous boundary during navigation.
        return new Proxy(response, {
          get(target, key) {
            const value = Reflect.get(target, key, target);
            if (['json', 'text', 'blob', 'arrayBuffer', 'formData'].includes(key)) return async (...args) => {
              const result = await value.apply(target, args);
              return active ? result : new Promise(() => {});
            };
            return typeof value === 'function' ? value.bind(target) : value;
          }
        });
      } catch (error) {
        if (!active) return new Promise(() => {});
        throw error;
      } finally {
        abort.signal.removeEventListener('abort', cancel);
        parentSignal?.removeEventListener('abort', cancel);
      }
    };

    function timer(repeat, callback, delay, ...args) {
      if (!active) return;
      const id = nativeWindow[repeat ? 'setInterval' : 'setTimeout'](() => {
        if (!repeat) timers.delete(id);
        if (active) callback(...args);
      }, delay);
      timers.add(id);
      return id;
    }
    const scope = {
      window: proxyWindow,
      globalThis: proxyWindow,
      document: proxyDocument,
      fetch: scopedFetch,
      history: new Proxy(history, {
        get(target, key) {
          if (['replaceState', 'pushState'].includes(key)) return (state, title, url) => {
            if (!active || loading) return;
            target[key]({
              ...target.state,
              ...state
            }, title, url);
            committedURL = location.href;
            committedState = target.state;
          };
          const value = Reflect.get(target, key, target);
          return typeof value === 'function' ? value.bind(target) : value;
        }
      }),
      setInterval: (fn, ms, ...args) => timer(true, fn, ms, ...args),
      setTimeout: (fn, ms, ...args) => timer(false, fn, ms, ...args),
      clearInterval: id => {
        timers.delete(id);
        nativeWindow.clearInterval(id);
      },
      clearTimeout: id => {
        timers.delete(id);
        nativeWindow.clearTimeout(id);
      },
      IntersectionObserver: typeof IntersectionObserver === 'function' ? class extends IntersectionObserver {
        constructor(callback, options) {
          super((...args) => {
            if (active) callback(...args);
          }, options);
          observers.add(this);
        }
      } : undefined,
      showToast: (...args) => {
        if (active) nativeWindow.showToast(...args);
      },
      postForm: (url, data, options = {}) => nativeWindow.postForm(url, data, {
        ...options,
        fetch: scopedFetch,
        live
      }),
      uiConfirm: async (...args) => active && await nativeWindow.uiConfirm(...args) && active,
      uiAlert: (...args) => active ? nativeWindow.uiAlert(...args) : Promise.resolve(false),
      expose(descriptors) {
        for (const [name, descriptor] of Object.entries(Object.getOwnPropertyDescriptors(descriptors))) publish(name, descriptor);
      },
      live
    };
    return {
      scope,
      ready() {
        for (const callback of ready.splice(0))
          if (active) callback();
      },
      dispose() {
        active = false;
        abort.abort();
        for (const id of timers) {
          clearTimeout(id);
          clearInterval(id);
        }
        for (const observer of observers) observer.disconnect();
        for (const remove of listeners) remove();
        for (const name of names)
          if (globals.get(name) === scope) {
            delete nativeWindow[name];
            globals.delete(name);
          }
      }
    };
  }

  function supported(url) {
    return url.origin === location.origin && (['/', '/users'].includes(url.pathname) || /^\/gateway\/g_[0-9a-f]{32}\/(devices|logs|scan)$/.test(url.pathname));
  }

  function saveScroll() {
    const scroll = {
      x: scrollX,
      y: scrollY,
      elements: {}
    };
    document.querySelectorAll('main [id]').forEach(node => {
      if (node.scrollTop || node.scrollLeft) scroll.elements[node.id] = [node.scrollLeft, node.scrollTop];
    });
    history.replaceState({
      ...history.state,
      wolScroll: scroll
    }, '', location.href);
    committedState = history.state;
  }

  function restoreScroll(scroll) {
    scrollTo(scroll?.x || 0, scroll?.y || 0);
    for (const [id, [x, y]] of Object.entries(scroll?.elements || {})) {
      const node = document.getElementById(id);
      if (node) {
        node.scrollLeft = x;
        node.scrollTop = y;
      }
    }
  }
  async function prepareScripts(doc) {
    const scripts = [...doc.querySelectorAll('main script, script[data-page-script]')];
    return Promise.all(scripts.map(async source => {
      if (!source.src) return source.textContent;
      const url = new URL(source.getAttribute('src'), location.href);
      if (url.origin !== location.origin) throw new Error('页面加载失败，请重试');
      if (!scriptCache.has(url.href)) {
        scriptCache.set(url.href, fetch(url.href, {
          signal: AbortSignal.timeout(15000)
        }).then(async response => {
          if (!response.ok) throw new Error('页面加载失败，请重试');
          return response.text();
        }).catch(error => {
          scriptCache.delete(url.href);
          throw error;
        }));
      }
      return scriptCache.get(url.href);
    }));
  }
  async function readPage(target, signal, speculative = false) {
    const response = await (speculative ? nativeRead : fetch)(target.href, {
      cache: 'no-store',
      headers: {
        Accept: 'text/html',
        'X-Requested-With': 'pjax'
      },
      signal: AbortSignal.any([signal, AbortSignal.timeout(15000)])
    });
    if (!response.ok) throw new Error('页面加载失败，请重试');
    const url = new URL(response.url || target.href);
    if (!supported(url)) return {
      url
    };
    const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
    if (!doc.querySelector('main')) throw new Error('页面加载失败，请重试');
    return {
      url,
      doc,
      scripts: await prepareScripts(doc)
    };
  }

  function invalidate() {
    clearTimeout(prefetchTimer);
    for (const entry of documentCache.values()) entry.controller.abort();
    documentCache.clear();
  }

  function prefetch(value) {
    const target = new URL(value, location.href);
    const network = navigator.connection;
    if (document.hidden || network?.saveData || /(^|-)2g$/.test(network?.effectiveType || '') || nativeWindow.scanInProgress || !supported(target) || target.hash || target.href === location.href) return Promise.resolve();
    const existing = documentCache.get(target.href);
    if (existing && performance.now() - existing.created < cacheTTL) return existing.promise;
    if (existing) existing.controller.abort();
    documentCache.delete(target.href);
    while (documentCache.size >= 8) {
      const key = documentCache.keys().next().value;
      documentCache.get(key).controller.abort();
      documentCache.delete(key);
    }
    const entry = {
      created: performance.now(),
      controller: new AbortController()
    };
    entry.promise = readPage(target, entry.controller.signal, true).then(result => {
      if (!result.doc) throw new Error('页面加载失败，请重试');
      return result;
    }).catch(() => {
      if (documentCache.get(target.href) === entry) documentCache.delete(target.href);
      // Speculative reads may be cancelled or fail while leaving a page.
      // Explicit navigation retries normally and reports any actual failure.
      return undefined;
    });
    documentCache.set(target.href, entry);
    return entry.promise;
  }

  function prefetchNearby() {
    clearTimeout(prefetchTimer);
    prefetchTimer = setTimeout(() => {
      const links = [...document.querySelectorAll('main [data-page-prefetch], main a[href]')].filter(anchor => {
        const url = new URL(anchor.dataset.pagePrefetch || anchor.href, location.href);
        return supported(url) && !url.hash && !anchor.target && !anchor.hasAttribute('download') && !anchor.hasAttribute('data-native-navigation') && anchor.getClientRects().length;
      });
      links.sort((a, b) => Number(b.hasAttribute('data-page-prefetch')) - Number(a.hasAttribute('data-page-prefetch')));
      for (const anchor of links.slice(0, 2)) void prefetch(anchor.dataset.pagePrefetch || anchor.href).catch(() => {});
    }, 350);
  }

  async function animatePage(node, signal) {
    if (!node || signal.aborted) return;
    const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduced) return;
    const frames = [{
      opacity: .34,
      transform: 'translateY(12px) scale(.985)'
    }, {
      opacity: 1,
      transform: 'translateY(0) scale(1)'
    }];
    const animation = nativeWindow.runUIAnimation(node, frames, {
      duration: 240,
      easing: 'cubic-bezier(.2,.7,.3,1)',
      fill: 'both'
    });
    const cancel = () => animation.cancel();
    signal.addEventListener('abort', cancel, {
      once: true
    });
    try {
      await animation.finished;
    } catch (_) {
      // A newer navigation cancels the transition and keeps the current page visible.
    } finally {
      signal.removeEventListener('abort', cancel);
      animation.cancel();
    }
  }
  async function navigate(value, options = {}) {
    const target = new URL(value, location.href);
    if (!supported(target)) {
      location.assign(target.href);
      return false;
    }
    if (loading) {
      queuedNavigation = [value, options];
      loading.abort();
      return false;
    }
    const controller = new AbortController();
    loading = controller;
    transition?.abort();
    clearTimeout(prefetchTimer);
    const previousURL = committedURL;
    const previousState = committedState;
    clearTimeout(scrollTimer);
    let committed = false;
    try {
      if (nativeWindow.scanAdding) return false;
      if (nativeWindow.scanInProgress) {
        if (!await uiConfirm('扫描仍在进行，关闭将停止等待并放弃本次结果，确认退出？', {
            title: '退出扫描',
            confirmText: '确认'
          })) return false;
        nativeWindow.stopPreviousScan?.(nativeWindow.activeScanId, nativeWindow.scanStartRequest)
          .catch(error => nativeWindow.showToast(t('退出扫描失败，设备可能仍在扫描：') + error.message, {
            error: true
          }));
        nativeWindow.cancelActiveScan?.();
      }
      document.documentElement.dataset.navigation = 'loading';
      document.querySelectorAll('[data-page-prefetch]').forEach(button => {
        if (new URL(button.dataset.pagePrefetch, location.href).href === target.href) button.setAttribute('aria-busy', 'true');
      });
      const cached = options.fresh ? null : documentCache.get(target.href);
      documentCache.delete(target.href);
      let prepared;
      if (cached && performance.now() - cached.created < cacheTTL) {
        const cancel = () => cached.controller.abort();
        controller.signal.addEventListener('abort', cancel, {
          once: true
        });
        try {
          prepared = await cached.promise;
        } catch (error) {
          if (controller.signal.aborted) throw error;
          // A failed speculative read must not prevent an explicit navigation.
        } finally {
          controller.signal.removeEventListener('abort', cancel);
        }
      } else cached?.controller.abort();
      prepared ||= await readPage(target, controller.signal);
      const {
        url: finalURL,
        doc,
        scripts
      } = prepared;
      if (!supported(finalURL)) {
        location.assign(finalURL.href);
        return true;
      }
      const nextMain = doc.querySelector('main');
      if (!nextMain) throw new Error('页面加载失败，请重试');
      // A newly deployed shell may require new shared scripts/CSS.
      if (doc.querySelector('link[rel="stylesheet"]')?.getAttribute('href') !== document.querySelector('link[rel="stylesheet"]')?.getAttribute('href')) {
        location.assign(finalURL.href);
        return true;
      }
      if (controller.signal.aborted) return false;
      if (!options.pop) saveScroll();
      const toast = document.getElementById('toast-region');
      if (toast) document.body.append(toast);
      document.querySelectorAll('dialog[open]').forEach(dialog => {
        dialog.returnValue = 'cancel';
        dialog.close();
      });
      page?.dispose();
      page = createPage();
      document.querySelector('main').replaceWith(nextMain);
      nextMain.querySelectorAll('script').forEach(script => script.remove());
      document.title = doc.title;
      if (options.pop) history.replaceState(options.state, '', finalURL.href);
      else if (options.replace) history.replaceState(history.state, '', finalURL.href);
      else history.pushState({
        wolIndex: (previousState?.wolIndex || 0) + 1
      }, '', finalURL.href);
      committedURL = location.href;
      committedState = history.state;
      committed = true;
      nativeWindow.localizeUI?.(nextMain);
      for (const code of scripts) {
        const script = document.createElement('script');
        script.textContent = code;
        document.body.append(script);
        script.remove();
      }
      page.ready();
      restoreScroll(options.pop ? options.state?.wolScroll : options.keepScroll ? previousState?.wolScroll : null);
      document.dispatchEvent(new CustomEvent('wol:page-loaded'));
      transition = new AbortController();
      void animatePage(nextMain, transition.signal);
      prefetchNearby();
      return true;
    } catch (error) {
      if (!controller.signal.aborted) showToast(error.message || '页面加载失败，请重试', {
        error: true
      });
      return false;
    } finally {
      loading = null;
      document.documentElement.dataset.navigation = 'ready';
      document.querySelectorAll('[data-page-prefetch][aria-busy]').forEach(button => button.removeAttribute('aria-busy'));
      if (options.pop && !committed && !queuedNavigation) {
        const delta = previousState?.wolIndex - options.state?.wolIndex;
        if (Number.isFinite(delta) && delta) {
          undoingPop = true;
          history.go(delta);
        } else history.replaceState(previousState, '', previousURL);
      }
      if (queuedNavigation) {
        const next = queuedNavigation;
        queuedNavigation = null;
        navigate(...next);
      }
    }
  }
  nativeWindow.WolPage = {
    mount(callback) {
      (page ||= createPage());
      callback(page.scope);
    },
    navigate,
    prefetch,
    invalidate,
    refresh() {
      saveScroll();
      invalidate();
      return navigate(location.href, {
        replace: true,
        keepScroll: true,
        fresh: true
      });
    }
  };
  nativeWindow.addEventListener('pagehide', event => {
    invalidate();
    if (!event.persisted) page?.dispose();
  });
  document.addEventListener('DOMContentLoaded', () => {
    page?.ready();
    prefetchNearby();
  }, {
    once: true
  });
  for (const eventName of ['pointerover', 'focusin', 'pointerdown', 'touchstart']) {
    document.addEventListener(eventName, event => {
      const anchor = event.target.closest?.('[data-page-prefetch], a[href]');
      if (anchor && !anchor.disabled && !anchor.target && !anchor.hasAttribute('download') && !anchor.hasAttribute('data-native-navigation')) void prefetch(anchor.dataset.pagePrefetch || anchor.href).catch(() => {});
    }, {
      passive: true
    });
  }
  document.addEventListener('click', event => {
    const anchor = event.target.closest?.('a[href]');
    if (!anchor || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || anchor.hasAttribute('download') || anchor.target && anchor.target !== '_self') return;
    const url = new URL(anchor.href, location.href);
    if (!supported(url) || url.hash || anchor.getAttribute('href')?.startsWith('#') || anchor.hasAttribute('data-native-navigation')) return;
    event.preventDefault();
    navigate(url.href);
  });
  window.addEventListener('popstate', event => {
    if (undoingPop) {
      undoingPop = false;
      return;
    }
    navigate(location.href, {
      pop: true,
      state: event.state
    });
  });
  document.addEventListener('scroll', () => {
    clearTimeout(scrollTimer);
    if (!loading) scrollTimer = setTimeout(saveScroll, 100);
  }, true);
})();
