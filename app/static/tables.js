/* Shared accessible table headings; each page owns its full-data refresh. */
(() => {
  const bindings = new Map();
  const bindingKey = id => `${location.pathname}:${id}`;

  function paint(id) {
    const table = document.getElementById(id),
      binding = bindings.get(bindingKey(id));
    if (!table || !binding) return;
    table.querySelectorAll('th[data-sort-key]').forEach(th => {
      th.tabIndex = 0;
      th.classList.add('sortable-heading');
      if (!th.querySelector('.table-sort-icon')) {
        const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        icon.classList.add('table-sort-icon');
        icon.setAttribute('viewBox', '0 0 16 16');
        icon.setAttribute('aria-hidden', 'true');
        for (const [direction, pathData] of [
            ['asc', 'm3 6 5-4 5 4'],
            ['desc', 'm3 10 5 4 5-4']
          ]) {
          const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
          path.dataset.direction = direction;
          path.setAttribute('d', pathData);
          icon.append(path);
        }
        th.append(icon);
      }
      const value = th.dataset.sortKey === binding.key ? (binding.direction === 'asc' ? 'ascending' : 'descending') : 'none';
      if (th.getAttribute('aria-sort') !== value) th.setAttribute('aria-sort', value);
    });
  }

  function bind(id, refresh, initial = {}) {
    const table = document.getElementById(id);
    if (!table) return;
    const defaults = {
      key: initial.key || 'id',
      direction: initial.direction || 'desc'
    };
    const key = table.dataset.sort || defaults.key;
    const direction = table.dataset.direction === 'asc' ? 'asc' : defaults.direction;
    bindings.set(bindingKey(id), {
      key,
      direction,
      defaults,
      refresh
    });
    paint(id);
  }

  function reset(id) {
    const state = bindings.get(bindingKey(id));
    if (!state) return;
    Object.assign(state, state.defaults);
    paint(id);
  }

  function options(id) {
    const state = bindings.get(bindingKey(id));
    return {
      sort: state?.key || 'id',
      direction: state?.direction || 'desc'
    };
  }

  function query(id) {
    return '&' + new URLSearchParams(options(id));
  }

  function select(th) {
    const table = th.closest('table'),
      state = bindings.get(bindingKey(table.id));
    if (!state) return;
    state.direction = state.key === th.dataset.sortKey && state.direction === 'asc' ? 'desc' : 'asc';
    state.key = th.dataset.sortKey;
    paint(table.id);
    state.refresh();
  }

  function compare(a, b, key) {
    if (key === 'ip') {
      const ip = value => /^\d+\.\d+\.\d+\.\d+$/.test(value || '') ? value.split('.').reduce((n, part) => n * 256 + Number(part), 0) : -1;
      return ip(a) - ip(b);
    }
    if (key === 'id') return Number(String(a).replace(/^P-/i, '')) - Number(String(b).replace(/^P-/i, ''));
    const pieces = value => String(value ?? '').normalize('NFKC').toLowerCase().split(/(\d+)/).map(piece => /^\d+$/.test(piece) ? [1, Number(piece)] : [0, piece]);
    const left = pieces(a),
      right = pieces(b);
    for (let i = 0; i < Math.min(left.length, right.length); i++) {
      if (left[i][0] !== right[i][0]) return left[i][0] - right[i][0];
      if (left[i][1] !== right[i][1]) return left[i][1] < right[i][1] ? -1 : 1;
    }
    return left.length - right.length;
  }

  function reorder(id, values) {
    const table = document.getElementById(id);
    if (!table) return;
    const {
      sort,
      direction
    } = options(id);
    const th = table.querySelector(`th[data-sort-key="${CSS.escape(sort)}"]`);
    if (!th) return;
    const index = th.cellIndex,
      body = table.tBodies[0];
    const rows = [...body.rows].filter(row => row.cells.length === table.tHead.rows[0].cells.length);
    const value = row => values ? values(row)?.[sort] : row.cells[index].querySelector('input:not([type=checkbox])')?.value ?? row.cells[index].querySelector('[id^="device-name-"],[id^="gateway-name-"],[id^="pairing-name-"]')?.textContent ?? row.cells[index].textContent.trim();
    const sorted = [...rows].sort((a, b) => compare(value(a), value(b), sort) * (direction === 'asc' ? 1 : -1));
    sorted.forEach((row, i) => {
      if (body.children[i] !== row) body.insertBefore(row, body.children[i] || null);
    });
  }
  document.addEventListener('click', event => {
    const th = event.target.closest('th[data-sort-key]');
    if (th) select(th);
  });
  document.addEventListener('keydown', event => {
    if (['Enter', ' '].includes(event.key) && event.target.matches('th[data-sort-key]')) {
      event.preventDefault();
      select(event.target);
    }
  });
  const refresh = () => document.querySelectorAll('table[id]').forEach(table => paint(table.id));
  new MutationObserver(refresh).observe(document.body, {
    childList: true,
    subtree: true
  });
  window.WolTables = {
    bind,
    reset,
    options,
    query,
    reorder,
    compare,
    paint
  };
})();
