(() => {
  // Session-only public list URLs; never persist notes, forms or personal records.
  const read = key => { try { return JSON.parse(sessionStorage.getItem(key)); } catch (_) { return null; } };
  const write = (key, value) => { try { sessionStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* navigation works without storage */ } };
  const stockURL = value => {
    try {
      const url = new URL(value, location.href);
      return url.origin === location.origin && /^\/a\/stocks\/\d{6}\.(SH|SZ)(\/chart)?$/.test(url.pathname) ? url : null;
    } catch (_) { return null; }
  };
  const token = new URL(location.href).searchParams.get('research_context');
  const context = token && /^[a-z0-9-]{1,60}$/.test(token) ? read(`research-context:${token}`) : null;
  if (context && Array.isArray(context.entries)) {
    const current = stockURL(location.href);
    const index = context.entries.findIndex(entry => {
      const url = stockURL(entry);
      return url && current && url.pathname === current.pathname.replace(/\/chart$/, '') && url.searchParams.get('date') === current.searchParams.get('date') && url.searchParams.get('event_id') === current.searchParams.get('event_id');
    });
    const source = new URL(context.source, location.origin);
    if (source.origin === location.origin && /^\/a\//.test(source.pathname) && index >= 0) {
      document.querySelectorAll('[data-research-return]').forEach(link => { link.href = source.href; link.textContent = `返回${context.label || '来源列表'} →`; });
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
        const target = stockURL(context.entries[index + offset]);
        link.hidden = !target;
        if (!target) return;
        if (current.pathname.endsWith('/chart')) target.pathname += '/chart';
        target.searchParams.set('research_context', token);
        link.href = target.href;
        link.setAttribute('aria-label', offset < 0 ? '上一只（来源顺序）' : '下一只（来源顺序）');
        if (!link.querySelector('img')) link.textContent = offset < 0 ? '← 上一只' : '下一只 →';
      };
      setNeighbor(previous || document.querySelector('[data-research-previous]'), -1);
      setNeighbor(next || document.querySelector('[data-research-next]'), 1);
      document.querySelectorAll('a[href], iframe[src]').forEach(node => {
        const attr = node.tagName === 'IFRAME' ? 'src' : 'href';
        const url = stockURL(node.getAttribute(attr));
        if (!url) return;
        url.searchParams.set('research_context', token);
        node.setAttribute(attr, url.href);
      });
    }
  }
  document.querySelectorAll('.watchlist-table, .observation-list, .workspace-recent-list').forEach(table => {
    table.addEventListener('click', event => {
      const link = event.target.closest('a[href]');
      if (!link || !stockURL(link.href)) return;
      const entries = Array.from(table.querySelectorAll('tbody tr, .observation-item, .workspace-recent-list > article')).map(row => {
        const candidate = Array.from(row.querySelectorAll('a[href]')).find(item => stockURL(item.href));
        const url = candidate && stockURL(candidate.href);
        if (!url) return null;
        url.pathname = url.pathname.replace(/\/chart$/, '');
        url.searchParams.delete('research_context');
        return url.pathname + url.search;
      }).filter(Boolean);
      const id = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
      write(`research-context:${id}`, {entries, source: location.pathname + location.search + location.hash, label: table.dataset.researchSourceLabel || document.querySelector('h1')?.textContent.trim()});
      write(`research-scroll:${location.pathname}${location.search}`, scrollY);
      const target = stockURL(link.href);
      target.searchParams.set('research_context', id);
      link.href = target.href;
    });
  });
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
})();
