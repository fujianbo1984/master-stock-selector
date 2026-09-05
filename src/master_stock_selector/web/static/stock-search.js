(() => {
  const form = document.querySelector('[data-stock-search]');
  if (!form) return;
  const input = form.querySelector('[name=q]');
  const list = form.querySelector('[role=listbox]');
  const status = form.querySelector('[role=status]');
  let timer, controller, rows = [], active = -1, revision = 0;
  const close = () => {
    revision += 1;
    clearTimeout(timer);
    controller?.abort();
    list.hidden = true;
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
    active = -1;
  };
  const select = (index) => {
    active = index;
    Array.from(list.children).forEach((node, i) => node.setAttribute('aria-selected', String(i === active)));
    if (active >= 0) {
      input.setAttribute('aria-activedescendant', list.children[active].id);
      list.children[active].scrollIntoView({block: 'nearest'});
    }
  };
  input.addEventListener('input', () => {
    close();
    const query = input.value.trim();
    rows = [];
    status.textContent = '';
    status.classList.remove('sr-only');
    if (!query) return;
    const current = revision;
    timer = setTimeout(async () => {
      controller = new AbortController();
      status.textContent = '正在查询在库股票…';
      try {
        const params = new URLSearchParams(new FormData(form));
        const response = await fetch(`/api/a/stocks/search?${params}`, {signal: controller.signal});
        if (!response.ok) throw new Error('search');
        const data = await response.json();
        if (current !== revision) return;
        rows = data.rows;
        list.replaceChildren();
        rows.forEach((row, i) => {
          const option = document.createElement('li');
          option.id = `stock-search-option-${i}`;
          option.setAttribute('role', 'option');
          option.setAttribute('aria-selected', 'false');
          const name = document.createElement('strong');
          name.textContent = `${row.name || '名称待补'} · ${row.symbol}`;
          const info = document.createElement('small');
          info.textContent = `${row.industry || '行业待补'} · ${row.data_status}${row.is_st ? ' · 风险警示' : ''}${row.is_suspended ? ' · 停牌' : ''}`;
          option.append(name, info);
          option.addEventListener('pointerdown', event => { event.preventDefault(); window.location.assign(row.url); });
          list.append(option);
        });
        list.hidden = !rows.length;
        input.setAttribute('aria-expanded', String(!!rows.length));
        status.classList.toggle('sr-only', rows.length > 0);
        status.textContent = rows.length ? `${rows.length} 个匹配 · ↑↓选择 · 回车打开` : '该日期库内未找到匹配股票';
      } catch (error) {
        if (error.name !== 'AbortError' && current === revision) status.textContent = '查询暂不可用，请重试或按回车进入查询页';
      }
    }, 220);
  });
  input.addEventListener('keydown', event => {
    if (event.isComposing) return;
    if (event.key === 'Escape') { close(); status.textContent = ''; }
    if (!list.hidden && rows.length) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        select(active < 0 ? (event.key === 'ArrowDown' ? 0 : rows.length - 1) : (active + (event.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length);
      } else if (event.key === 'Enter' && active >= 0) {
        event.preventDefault();
        window.location.assign(rows[active].url);
      }
    }
  });
  form.addEventListener('focusout', event => { if (!form.contains(event.relatedTarget)) close(); });
  document.addEventListener('pointerdown', event => { if (!form.contains(event.target)) close(); });
})();
