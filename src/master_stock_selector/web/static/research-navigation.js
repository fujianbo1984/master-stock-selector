(() => {
  // Only navigation URLs, scoped to the signed-in user, expire after two hours.
  const scope = document.body.dataset.researchUser || 'anonymous';
  const prefix = `research-context:${scope}:`;
  const ttl = 2 * 60 * 60 * 1000;
  const readContext = token => {
    try {
      const value = JSON.parse(localStorage.getItem(prefix + token));
      return value?.expires > Date.now() ? value : null;
    } catch (_) { return null; }
  };
  try {
    Object.keys(localStorage).filter(key => key.startsWith('research-context:')).forEach(key => {
      try { if (!(JSON.parse(localStorage.getItem(key))?.expires > Date.now())) localStorage.removeItem(key); }
      catch (_) { localStorage.removeItem(key); }
    });
  } catch (_) { /* Storage may be unavailable. */ }
  const read = key => { try { return JSON.parse(sessionStorage.getItem(key)); } catch (_) { return null; } };
  const write = (key, value) => { try { sessionStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* navigation works without storage */ } };
  const stockURL = value => {
    try {
      const url = new URL(value, location.href);
      return url.origin === location.origin && /^\/a\/stocks\/\d{6}\.(SH|SZ)(\/(chart|realtime))?$/.test(url.pathname) ? url : null;
    } catch (_) { return null; }
  };
  const token = new URL(location.href).searchParams.get('research_context');
  const context = token && /^[a-z0-9-]{1,60}$/.test(token) ? readContext(token) : null;
  if (token && !context && stockURL(location.href)) {
    const message = document.createElement('small');
    message.textContent = '来源列表已过期，请返回列表重新打开。';
    document.querySelector('.chart-hero-actions')?.append(message);
  }
  if (context && Array.isArray(context.entries)) {
    const current = stockURL(location.href);
    const selectedIndex = Number(new URL(location.href).searchParams.get('research_index'));
    const matches = entry => {
      const url = stockURL(entry);
      return url && current && url.pathname === current.pathname.replace(/\/(chart|realtime)$/, '') && url.searchParams.get('date') === current.searchParams.get('date') && url.searchParams.get('event_id') === current.searchParams.get('event_id');
    };
    const index = matches(context.entries[selectedIndex]) ? selectedIndex : context.entries.findIndex(matches);
    const source = new URL(context.source, location.origin);
    if (source.origin === location.origin && /^\/a\//.test(source.pathname) && index >= 0) {
      document.querySelectorAll('[data-research-return]').forEach(link => { link.href = source.href; link.textContent = `返回${context.label || '来源列表'} →`; });
      if (!document.querySelector('.hero-navigation-context, .chart-navigation-context')) {
        const badge = document.createElement('span'); badge.className = 'hero-navigation-context';
        document.querySelector('.chart-list-navigation, .chart-hero-actions')?.append(badge);
      }
      document.querySelectorAll('.hero-navigation-context, .chart-navigation-context').forEach(node => { node.textContent = `${context.label || '来源列表'} · ${index + 1} / ${context.entries.length}`; });
      document.querySelectorAll('.chart-list-navigation .is-disabled').forEach(node => node.remove());
      const previous = document.querySelector('[data-chart-previous], .stock-hero-actions a[data-research-previous]');
      const next = document.querySelector('[data-chart-next], .stock-hero-actions a[data-research-next]');
      // Existing detail navigation anchors use explicit data attributes below.
      const setNeighbor = (link, offset) => {
        if (!link) {
          const container = document.querySelector('.chart-list-navigation, .chart-hero-actions');
          if (!container) return;
          link = document.createElement('a');
          link.className = 'button button-secondary button-compact';
          container.append(link);
        }
        link.setAttribute(offset < 0 ? 'data-research-previous' : 'data-research-next', '');
        const target = stockURL(context.entries[index + offset]);
        link.hidden = !target;
        if (!target) return;
        if (current.pathname.endsWith('/chart')) target.pathname += '/chart';
        target.searchParams.set('research_context', token);
        target.searchParams.set('research_index', index + offset);
        link.href = target.href;
        link.setAttribute('aria-label', offset < 0 ? '上一只（来源顺序）' : '下一只（来源顺序）');
        link.title = link.getAttribute('aria-label');
        if (!link.querySelector('img')) link.textContent = offset < 0 ? '← 上一只' : '下一只 →';
      };
      setNeighbor(previous || document.querySelector('[data-research-previous]'), -1);
      setNeighbor(next || document.querySelector('[data-research-next]'), 1);
      document.querySelectorAll('a[href], iframe[src], input[name=return_to]').forEach(node => {
        const attr = node.tagName === 'IFRAME' ? 'src' : node.tagName === 'INPUT' ? 'value' : 'href';
        const url = stockURL(node.getAttribute(attr));
        if (!url) return;
        url.searchParams.set('research_context', token);
        if (url.pathname.replace(/\/(chart|realtime)$/, '') === current.pathname.replace(/\/(chart|realtime)$/, '') && !node.hasAttribute('data-research-previous') && !node.hasAttribute('data-research-next') && !node.hasAttribute('data-chart-previous') && !node.hasAttribute('data-chart-next')) {
          url.searchParams.set('research_index', index);
          const entry = stockURL(context.entries[index]);
          ['date', 'event_id'].forEach(key => { if (entry.searchParams.has(key)) url.searchParams.set(key, entry.searchParams.get(key)); });
        }
        node.setAttribute(attr, url.href);
      });
    }
  }
  const normalize = value => {
    const url = stockURL(value);
    if (!url) return null;
    url.pathname = url.pathname.replace(/\/(chart|realtime)$/, '');
    ['research_context', 'research_index'].forEach(key => url.searchParams.delete(key));
    return url.pathname + url.search;
  };
  const makeContext = (entries, source, label, listIndex = -1) => {
    const id = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
    const back = new URL(source, location.origin);
    back.searchParams.set('research_context', id);
    try { localStorage.setItem(prefix + id, JSON.stringify({entries, source: back.pathname + back.search + back.hash, label, listIndex, scroll: scrollY, expires: Date.now() + ttl})); }
    catch (_) { return null; }
    return id;
  };
  window.researchNavigation = {
    search(rows, index, source) {
      const target = stockURL(rows[index]?.url);
      if (!target) return;
      const id = makeContext(rows.map(row => normalize(row.url)), source, '股票查询');
      if (id) { target.searchParams.set('research_context', id); target.searchParams.set('research_index', index); }
      location.assign(target.href);
    }
  };
  const containers = '.watchlist-table, .observation-list, #industry-members table, [data-research-list]';
  const rowSelector = 'tbody tr, .observation-item, [data-research-row]';
  const listRows = table => {
    const rows = Array.from(table.querySelectorAll(rowSelector));
    return (rows.length ? rows : Array.from(table.children)).filter(row => !row.hidden && !row.closest('[hidden]'));
  };
  const rowURL = row => normalize(row.matches('a[href]') ? row.href : Array.from(row.querySelectorAll('a[href]')).find(a => stockURL(a.href) && !a.classList.contains('trade-action'))?.href);
  const capture = event => {
    const link = event.target.closest('a[href]');
    const table = link?.closest(containers);
    if (!table || !stockURL(link.href) || link.classList.contains('trade-action')) return;
    const rows = listRows(table).filter(row => rowURL(row));
    const entries = rows.map(rowURL);
    const index = rows.findIndex(row => row === link || row.contains(link));
    if (index < 0) return;
    const id = makeContext(entries, location.href, table.dataset.researchSourceLabel || document.querySelector('h1')?.textContent.trim(), Array.from(document.querySelectorAll(containers)).indexOf(table));
    write(`research-scroll:${location.pathname}${location.search}`, scrollY);
    if (!id) return;
    const target = stockURL(link.href);
    target.searchParams.set('research_context', id);
    target.searchParams.set('research_index', index);
    link.href = target.href;
  };
  // Capture before default navigation, including middle-click and context-menu opening.
  ['click', 'auxclick', 'contextmenu'].forEach(type => document.addEventListener(type, capture, true));
  const savedScroll = read(`research-scroll:${location.pathname}${location.search}`);
  if (savedScroll && performance.getEntriesByType('navigation')[0]?.type !== 'reload') {
    requestAnimationFrame(() => window.scrollTo(0, savedScroll));
  }
  document.querySelectorAll('.stock-workspace-nav a, .stock-method-summary a').forEach(link => {
    link.addEventListener('click', () => {
      const target = document.getElementById(link.hash.slice(1));
      if (target?.tagName === 'DETAILS') target.open = true;
    });
  });

  // Remember only public research locations, never private notes or form values.
  const discoveryPaths = ['/a/daily', '/a/brooks-second-leg'];
  if (discoveryPaths.includes(location.pathname)) {
    write('research-last-discovery', location.pathname + location.search);
  }
  document.querySelectorAll('[data-discovery-entry]').forEach(link => {
    const saved = read('research-last-discovery');
    if (typeof saved !== 'string') return;
    const url = new URL(saved, location.origin);
    if (url.origin !== location.origin || !discoveryPaths.includes(url.pathname)) return;
    const date = new URL(link.href).searchParams.get('date');
    if (date) url.searchParams.set('date', date);
    link.href = url.href;
  });
  const currentStock = stockURL(location.href);
  if (currentStock) {
    const url = new URL(currentStock);
    url.pathname = url.pathname.replace(/\/(chart|realtime)$/, '');
    const publicURL = url.pathname + (url.searchParams.has('date') ? `?date=${encodeURIComponent(url.searchParams.get('date'))}` : '');
    const prior = read('research-recent-stocks');
    const entries = Array.isArray(prior) ? prior.filter(item => item?.url !== publicURL && stockURL(item?.url)) : [];
    write('research-recent-stocks', [{url: publicURL, label: document.querySelector('h1')?.textContent.trim() || url.pathname.split('/').pop()}, ...entries].slice(0, 6));
  }
  const recent = document.querySelector('[data-recent-research]');
  const recentItems = read('research-recent-stocks');
  if (recent && Array.isArray(recentItems)) {
    const list = recent.querySelector('[data-recent-research-list]');
    recentItems.forEach(item => {
      const url = stockURL(item?.url);
      if (!url) return;
      const a = document.createElement('a');
      url.pathname += '/chart';
      a.href = url.href;
      a.textContent = `${item.label || url.pathname.split('/').pop()} · ${url.searchParams.get('date') || '最新可用日'}`;
      list.append(a);
    });
    recent.hidden = !list.children.length;
  }
  // Rebuild the visible source order after following the return link.
  if (context && !currentStock) {
    const table = document.querySelectorAll(containers)[context.listIndex];
    if (table) {
      const remaining = listRows(table).filter(row => rowURL(row));
      const ordered = context.entries.map(entry => {
        const i = remaining.findIndex(row => rowURL(row) === entry);
        return i < 0 ? null : remaining.splice(i, 1)[0];
      }).filter(Boolean);
      // Do not reorder mixed feeds with date headings; only sortable tables.
      const tbody = table.querySelector('tbody');
      if (tbody) [...ordered, ...remaining].forEach(row => tbody.append(row));
      requestAnimationFrame(() => window.scrollTo(0, context.scroll || 0));
    }
  }
  document.querySelectorAll('[data-persist-disclosure]').forEach(details => {
    const key = `research-help:${details.dataset.persistDisclosure}`;
    details.open = read(key) === true;
    details.addEventListener('toggle', () => write(key, details.open));
  });
  document.addEventListener('click', event => {
    document.querySelectorAll('.utility-menu[open]').forEach(menu => { if (!menu.contains(event.target)) menu.open = false; });
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') document.querySelectorAll('.utility-menu[open], .kc-settings[open]').forEach(menu => { menu.open = false; menu.querySelector('summary')?.focus(); });
  });
  const planEditor = document.getElementById('plan-editor');
  document.querySelector('[data-open-plan-editor]')?.addEventListener('click', () => { if (planEditor) planEditor.open = true; });
  if (planEditor && new URL(location.href).searchParams.has('session')) {
    planEditor.open = true;
    requestAnimationFrame(() => (document.getElementById(location.hash.slice(1)) || planEditor).scrollIntoView());
  }
})();
