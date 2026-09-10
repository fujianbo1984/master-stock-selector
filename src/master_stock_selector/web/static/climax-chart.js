/* One optional fact layer; deliberately creates no KC or moving-average series. */
window.createClimaxLayer = ({ root, candles, onChange, restoreKc }) => {
  const input = root.querySelector('[data-climax]');
  const panel = root.querySelector('[data-climax-panel]');
  const summary = root.querySelector('[data-climax-summary]');
  const detail = root.querySelector('[data-climax-detail]');
  const select = root.querySelector('[data-climax-event]');
  const refs = root.querySelector('[data-climax-references]');
  const warning = root.querySelector('[data-climax-kc-warning]');
  let payload = null, selectedId = '', lines = [];
  const storageKey = 'masterstock-chart:climax:v1';
  try { input.checked = localStorage.getItem(storageKey) === 'true'; } catch (_) { /* optional preference */ }
  const labels = {
    ONSET: '潜在高潮', FAILURE_ATTEMPT: '◇ 冲高失败尝试', MOMENTUM_ATTEMPT: '◇ 反向动量尝试',
    FAILURE_CONFIRMED: '冲高失败确认', MOMENTUM_CONFIRMED: '反向动量确认',
    UNKNOWN: '数据未知，后续推断中断', PAUSED: '停牌，待确认尝试清空', WINDOW_ENDED: '10日窗口结束',
  };
  const checkLabels = { history: '有效历史≥120日', st: 'ST覆盖明确且非ST', liquidity: '前20日成交额中位数达标',
    market_value: '市值数据有效', price: '收盘高于2个事前ATR', trend: '当前EMA上涨背景',
    background: '加速前上涨背景', speed: '5日涨幅≥8%', acceleration: '加速比≥1.5',
    distance: '偏离EMA20≥3个事前ATR', near_high: '距5日高点≤0.5个事前ATR', witness: '三种极端证据至少一种' };
  const number = (n, digits = 2) => Number.isFinite(n) ? n.toFixed(digits) : '—';
  const percent = n => Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : '—';
  const clearLines = () => { lines.forEach(line => candles.removePriceLine(line)); lines = []; };
  const visible = () => input.checked && payload?.interval === 'day';
  const element = (tag, text, className = '') => {
    const node = document.createElement(tag); node.textContent = text; node.className = className; return node;
  };
  const metricsText = m => `5日涨幅 ${percent(m.return5)} · 前15日 ${percent(m.pre15)} · 加速比 ${number(m.acceleration)} · EMA20偏离 ${number(m.distance)} 事前ATR · KC外 ${m.free_bars ?? '—'}/5根 · 最大TR ${number(m.max_tr)} ATR · 完整缺口 ${number(m.gap)} ATR`;
  const renderDetail = () => {
    clearLines(); detail.replaceChildren();
    const event = payload?.climax?.events?.find(e => e.id === selectedId);
    detail.hidden = !visible() || !event;
    refs.disabled = !event;
    if (!event || !visible()) return;
    detail.append(element('strong', `${event.date} · ${event.category === 'MATURE' ? '成熟上涨' : '局部上涨'} · 潜在高潮`));
    detail.append(element('p', metricsText(event.evidence)));
    const checks = document.createElement('details');
    checks.append(element('summary', '查看触发依据'));
    const list = document.createElement('ul');
    Object.entries(event.checks).forEach(([key, value]) => list.append(element('li', `${value ? '✓' : '—'} ${checkLabels[key] || key}`)));
    list.append(element('li', `加速前60日 ${percent(event.evidence.pre60)} / 20日 ${percent(event.evidence.pre20)}；距5日高点 ${number(event.evidence.near_high)} ATR；收盘位置 ${percent(event.evidence.clv)}`));
    list.append(element('li', '首次由假变真且距上一接受起点至少20个市场交易日；其他ATR使用此前20日TR简单平均，起点后冻结A0。'));
    checks.append(list); detail.append(checks);
    const timeline = document.createElement('ol'); timeline.className = 'climax-timeline';
    event.timeline.forEach(p => timeline.append(element('li', `${p.date} · ${labels[p.kind] || p.kind}${p.attempt_date ? `（跟进 ${p.attempt_date}）` : ''}`)));
    detail.append(timeline);
    const latest = payload.bars[payload.bars.length - 1];
    const since = payload.bars.filter(b => b.trade_date >= event.date);
    if (latest && event.evidence.close && since.length) {
      const peak = Math.max(...since.map(b => b.high));
      detail.append(element('p', `截至 ${latest.trade_date}，收盘较事件起点 ${percent(latest.close / event.evidence.close - 1)}；距图中事件后已观察高点 ${percent(latest.close / peak - 1)}。上行与回撤可以同时发生。`));
    }
    const last = event.timeline[event.timeline.length - 1];
    const ended = event.timeline.some(p => ['UNKNOWN', 'WINDOW_ENDED'].includes(p.kind));
    if (!ended) detail.append(element('p', `截至 ${payload.climax.current?.date || payload.as_of_date}，仍在10日观察窗口；尝试需紧接下一市场日跟进才能确认。`));
    if (last?.kind === 'WINDOW_ENDED' && !event.timeline.some(p => p.kind.endsWith('_CONFIRMED'))) detail.append(element('p', '窗口内未观察到规定确认，不代表没有下跌风险。'));
    if (refs.checked) {
      const reference = [...event.timeline].reverse().find(p => Number.isFinite(p.reference));
      [[event.evidence.close, '事件起点收盘', '#b97720'], [reference?.reference, '形态参考高点', '#a45252']].forEach(([price, title, color]) => {
        if (Number.isFinite(price)) lines.push(candles.createPriceLine({ price, title, color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true }));
      });
    }
  };
  const render = next => {
    payload = next; input.disabled = next.interval !== 'day'; input.title = input.disabled ? '扩张与衰竭仅适用于日线' : '';
    panel.hidden = !visible(); clearLines();
    if (!visible()) return;
    const layer = next.climax || { status: 'NOT_COMPUTED', events: [] };
    const status = { NOT_COMPUTED: '该股票尚未完成扩张计算', OUTSIDE_COVERAGE: '所选日期不在计算覆盖范围', STALE: `计算截至 ${layer.computed_through}，尚未更新至所选日期` }[layer.status];
    const current = layer.current;
    const currentReason = current?.reason === 'DATA_GAP' ? '行情或连续历史不足，无法完整判断' : current?.reason === 'QUALIFICATION_UNKNOWN' ? 'ST／市值／成交额资格数据不足' : current?.potential ? '当前潜在条件成立（持续成立不重复登记）' : '当前未满足全部潜在条件';
    summary.textContent = [status, current && `${current.date} · ${currentReason}`, current?.metrics && metricsText(current.metrics)].filter(Boolean).join('。');
    warning.hidden = next.keltner_config?.preset === 'ashare_daily_calibrated_v1';
    select.replaceChildren(new Option('点击图上标记查看事件依据', ''));
    [...(layer.events || [])].reverse().forEach(e => select.add(new Option(`${e.date} ${e.category === 'MATURE' ? '成熟' : '局部'}上涨`, e.id)));
    if (!(layer.events || []).some(e => e.id === selectedId)) selectedId = '';
    select.value = selectedId;
    renderDetail();
  };
  const markers = () => {
    if (!visible()) return [];
    const dates = new Set(payload.bars.map(b => b.trade_date));
    return (payload.climax?.events || []).flatMap(event => {
      const grouped = new Map();
      event.timeline.filter(p => dates.has(p.date) && (p.kind === 'ONSET' || p.kind.endsWith('_CONFIRMED') || (event.id === selectedId && p.kind.endsWith('_ATTEMPT')))).forEach(p => {
        const old = grouped.get(p.date);
        if (!old || p.kind.endsWith('_CONFIRMED')) grouped.set(p.date, p);
      });
      return [...grouped.values()].map(p => ({ priority: 3, marker: {
        id: `climax|${event.id}|${p.date}|${p.kind}`, time: p.date, position: 'aboveBar',
        shape: p.kind.endsWith('_CONFIRMED') ? 'square' : 'circle',
        color: p.kind === 'ONSET' ? '#b97720' : p.kind.endsWith('_ATTEMPT') ? '#8c8273' : '#a45252',
        size: p.kind.endsWith('_ATTEMPT') ? 0 : .65,
        text: p.kind === 'ONSET' ? '潜在高潮' : p.kind.endsWith('_CONFIRMED') ? '衰竭确认' : '◇',
      }}));
    });
  };
  input.addEventListener('change', () => {
    try { localStorage.setItem(storageKey, String(input.checked)); } catch (_) { /* optional preference */ }
    if (payload) render(payload); onChange();
  });
  select.addEventListener('change', () => { selectedId = select.value; renderDetail(); onChange(); });
  refs.addEventListener('change', renderDetail);
  root.querySelector('[data-climax-restore-kc]').addEventListener('click', restoreKc);
  return { render, markers, click(id) {
    if (!visible() || typeof id !== 'string' || !id.startsWith('climax|')) return false;
    selectedId = id.split('|')[1]; select.value = selectedId; renderDetail(); onChange(); return true;
  } };
};
