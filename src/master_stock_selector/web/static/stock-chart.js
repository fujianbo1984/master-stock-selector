(() => {
  const root = document.querySelector("[data-stock-chart]");
  if (!root || !window.LightweightCharts) return;
  const chartNode = root.querySelector("[data-chart-canvas]");
  const overlay = root.querySelector("[data-drawing-overlay]");
  const context = overlay.getContext("2d");
  const textLayer = root.querySelector("[data-drawing-text-layer]");
  const textEditor = root.querySelector("[data-drawing-text-editor]");
  const textInput = root.querySelector("[data-drawing-text-input]");
  const textCount = root.querySelector("[data-drawing-text-count]");
  const textSaveButton = root.querySelector("[data-drawing-text-save]");
  const textCancelButton = root.querySelector("[data-drawing-text-cancel]");
  const volumeInput = root.querySelector("[data-volume]");
  const keltnerInput = root.querySelector("[data-keltner]");
  const kcSummary = root.querySelector("[data-kc-summary]");
  const kcPresetInput = root.querySelector("[data-kc-preset]");
  const kcPresets = JSON.parse(root.querySelector("[data-keltner-presets]").textContent);
  const changeReadout = root.querySelector("[data-chart-period-change]");
  const changeDate = root.querySelector("[data-chart-change-date]");
  const changeValue = root.querySelector("[data-chart-change-value]");
  const drawingActions = root.querySelector("[data-drawing-actions]");
  const drawingHint = root.querySelector("[data-drawing-hint]");
  const deleteSelectedButton = root.querySelector("[data-delete-selected]");
  const editSelectedButton = root.querySelector("[data-edit-selected]");
  const methodEventDetail = root.querySelector("[data-method-event-detail]");
  const frameworkPanel = root.querySelector("[data-chart-framework-panel]");
  const timelineList = root.querySelector("[data-chart-method-timeline-list]");
  const loadState = root.querySelector("[data-chart-load-state]");
  const stateOverlay = root.querySelector("[data-chart-state-overlay]");
  const stateTitle = root.querySelector("[data-chart-state-title]");
  const stateDetail = root.querySelector("[data-chart-state-detail]");
  const retryButton = root.querySelector("[data-chart-retry]");
  const tradeLegend = root.querySelector("[data-trade-legend]");
  const openValue = root.querySelector("[data-chart-open]");
  const highValue = root.querySelector("[data-chart-high]");
  const lowValue = root.querySelector("[data-chart-low]");
  const closeValue = root.querySelector("[data-chart-close]");
  const volumeValue = root.querySelector("[data-chart-volume]");
  const anomalyRoot = root.querySelector("[data-chart-anomaly-evidence]");
  const anomalyLabel = root.querySelector("[data-anomaly-label]");
  const anomalyMetrics = root.querySelector("[data-anomaly-metrics]");
  const anomalyConclusion = root.querySelector("[data-anomaly-conclusion]");
  const anomalyDetails = root.querySelector("[data-anomaly-details]");
  const anomalyPolicy = root.querySelector("[data-anomaly-policy]");
  const symbol = root.dataset.symbol;
  const date = root.dataset.date;
  const privateOverlayUrl = root.dataset.privateOverlayUrl || "";
  const csrfToken = root.dataset.csrfToken || "";
  let limit = 260;
  let interval = "day";
  let payload = null;
  let drawings = [];
  let activeTool = "browse";
  let pendingAnchor = null;
  let previewAnchor = null;
  let selectedDrawingId = null;
  let editorAnchor = null;
  let editingDrawingId = null;
  let requestVersion = 0;
  let requestController = null;
  let adjacentPrefetchSignature = "";
  let methodEventsById = new Map();
  const canonicalDrawingAnchors = new Map();
  const chartViewStorageKey = "masterstock-chart:view";
  const chartLayersStorageKey = "masterstock-chart:layers:v3";
  const chartAppearanceStorageKey = "masterstock-chart:appearance:v1";
  const candleStyles = {
    monochrome: {
      label: "黑白：阳线空心 · 阴线实心",
      candles: {
        upColor: "#fffdfa", downColor: "#202020", borderVisible: true,
        borderUpColor: "#202020", borderDownColor: "#202020",
        wickUpColor: "#202020", wickDownColor: "#202020",
      },
      volumeUpColor: "rgba(90,90,90,.28)",
      volumeDownColor: "rgba(32,32,32,.58)",
    },
    colored: {
      label: "红绿：红涨 · 绿跌",
      candles: {
        upColor: "#c94e47", downColor: "#258460", borderVisible: false,
        borderUpColor: "#c94e47", borderDownColor: "#258460",
        wickUpColor: "#c94e47", wickDownColor: "#258460",
      },
      volumeUpColor: "rgba(201,78,71,.48)",
      volumeDownColor: "rgba(37,132,96,.48)",
    },
  };
  let candleStyle = "monochrome";
  const frameworkForInterval = () => interval === "week" ? "weinstein" : "minervini";
  const defaultKcPresetForInterval = () => interval === "week" ? "adam_original_reference_v1" : "ashare_daily_calibrated_v1";
  const applyKcPreset = (name) => {
    const preset = kcPresets[name];
    if (!preset) return;
    kcPresetInput.value = name;
    root.querySelector("[data-kc-length]").value = preset.length;
    root.querySelector("[data-kc-atr-length]").value = preset.atr_length;
    root.querySelector("[data-kc-multiplier]").value = preset.multiplier;
    root.querySelector("[data-kc-source]").value = preset.source;
    root.querySelector("[data-kc-ma]").value = preset.use_ema ? "ema" : "sma";
    root.querySelector("[data-kc-style]").value = preset.band_style;
  };
  const frameworkLayerVisible = () => root.querySelector(`[data-framework-layer="${frameworkForInterval()}"]`)?.checked === true;
  const syncChartViewControls = () => {
    root.querySelectorAll("[data-chart-interval]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.chartInterval === interval)));
    root.querySelectorAll("[data-chart-limit]").forEach(button => {
      const buttonLimit = button.dataset.chartLimit === "all" ? null : Number(button.dataset.chartLimit);
      button.setAttribute("aria-pressed", String(buttonLimit === limit));
    });
    const framework = frameworkForInterval();
    root.querySelectorAll("[data-framework-indicator], [data-framework-legend]").forEach(item => { item.hidden = item.dataset.frameworkIndicator !== undefined ? item.dataset.frameworkIndicator !== framework : item.dataset.frameworkLegend !== framework; });
    root.querySelectorAll("[data-framework-status]").forEach(item => { item.hidden = item.dataset.frameworkStatus !== framework; });
  };
  const restoreChartView = () => {
    try {
      const saved = JSON.parse(window.sessionStorage.getItem(chartViewStorageKey) || "null");
      if (!saved || typeof saved !== "object") return;
      if (["day", "week"].includes(saved.interval)) interval = saved.interval;
      if (saved.limit === null || saved.limit === 260) limit = saved.limit;
    } catch (_) { /* Browser storage can be unavailable or contain stale data. */ }
  };
  const storeChartView = () => {
    try { window.sessionStorage.setItem(chartViewStorageKey, JSON.stringify({ interval, limit })); }
    catch (_) { /* Browser storage can be unavailable. */ }
  };
  const restoreLayerSettings = () => {
    try {
      const saved = JSON.parse(window.sessionStorage.getItem(chartLayersStorageKey) || "null");
      if (!saved || typeof saved !== "object") return;
      root.querySelectorAll("[data-framework-layer]").forEach(input => {
        if (typeof saved[input.dataset.frameworkLayer] === "boolean") input.checked = saved[input.dataset.frameworkLayer];
      });
      if (typeof saved.keltner === "boolean") keltnerInput.checked = saved.keltner;
      if (typeof saved.volume === "boolean") volumeInput.checked = saved.volume;
    } catch (_) { /* Browser storage can be unavailable or contain stale data. */ }
  };
  const storeLayerSettings = () => {
    const saved = { keltner: keltnerInput.checked, volume: volumeInput.checked };
    root.querySelectorAll("[data-framework-layer]").forEach(input => { saved[input.dataset.frameworkLayer] = input.checked; });
    try { window.sessionStorage.setItem(chartLayersStorageKey, JSON.stringify(saved)); }
    catch (_) { /* Browser storage can be unavailable. */ }
  };
  const restoreAppearance = () => {
    try {
      const saved = window.sessionStorage.getItem(chartAppearanceStorageKey);
      if (saved && Object.hasOwn(candleStyles, saved)) candleStyle = saved;
    } catch (_) { /* Browser storage can be unavailable. */ }
  };
  const storeAppearance = () => {
    try { window.sessionStorage.setItem(chartAppearanceStorageKey, candleStyle); }
    catch (_) { /* Browser storage can be unavailable. */ }
  };
  const syncAppearanceControls = () => {
    root.querySelectorAll("[data-candle-style]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.candleStyle === candleStyle)));
    const label = root.querySelector("[data-candle-style-label]");
    if (label) label.textContent = candleStyles[candleStyle].label;
  };
  restoreChartView();
  restoreLayerSettings();
  restoreAppearance();
  applyKcPreset(defaultKcPresetForInterval());
  syncChartViewControls();
  syncAppearanceControls();
  const chart = LightweightCharts.createChart(chartNode, {
    layout: { background: { color: "#fffdfa" }, textColor: "#142d4c" },
    grid: { vertLines: { color: "#eee8de" }, horzLines: { color: "#eee8de" } },
    rightPriceScale: { borderColor: "#d8d0c3" }, timeScale: { borderColor: "#d8d0c3", timeVisible: true },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    localization: { locale: "zh-CN" },
  });
  const candles = chart.addSeries(LightweightCharts.CandlestickSeries, candleStyles[candleStyle].candles);
  const upper = chart.addSeries(LightweightCharts.LineSeries, { color: "#a9b0b7", lineWidth: 1, lastValueVisible: false, priceLineVisible: false });
  const basis = chart.addSeries(LightweightCharts.LineSeries, { color: "#b9bec4", lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, lastValueVisible: false, priceLineVisible: false });
  const lower = chart.addSeries(LightweightCharts.LineSeries, { color: "#a9b0b7", lineWidth: 1, lastValueVisible: false, priceLineVisible: false });
  const maM50 = chart.addSeries(LightweightCharts.LineSeries, { color: "#5b6fc7", lineWidth: 1, lastValueVisible: false, priceLineVisible: false });
  const maM150 = chart.addSeries(LightweightCharts.LineSeries, { color: "#d28b18", lineWidth: 1, lastValueVisible: false, priceLineVisible: false });
  const maM200 = chart.addSeries(LightweightCharts.LineSeries, { color: "#9a5c9d", lineWidth: 1, lastValueVisible: false, priceLineVisible: false });
  const maW10 = chart.addSeries(LightweightCharts.LineSeries, { color: "#5b6fc7", lineWidth: 1, lastValueVisible: false, priceLineVisible: false });
  const maW30 = chart.addSeries(LightweightCharts.LineSeries, { color: "#d28b18", lineWidth: 1, lastValueVisible: false, priceLineVisible: false });
  const initialVolumePane = chart.addPane();
  initialVolumePane.setHeight(132);
  const volume = chart.addSeries(LightweightCharts.HistogramSeries, { priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false, base: 0 }, initialVolumePane.paneIndex());
  volume.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0 } });
  const markersApi = LightweightCharts.createSeriesMarkers(candles, []);

  const minerviniCheckLabels = {
    close_above_sma50: "收盘价高于50日均线",
    close_above_sma150: "收盘价高于150日均线",
    close_above_sma200: "收盘价高于200日均线",
    sma50_above_sma150: "50日均线高于150日均线",
    sma50_above_sma200: "50日均线高于200日均线",
    sma150_above_sma200: "150日均线高于200日均线",
    sma200_rising_20d: "200日均线近20日上升",
    close_30pct_above_52w_low: "收盘价高于52周低点至少30%",
    close_within_25pct_52w_high: "收盘价距52周高点不超过25%",
    rs_252d_percentile_at_least_70: "252日相对强度百分位至少70",
  };

  const settings = () => ({
    length: root.querySelector("[data-kc-length]").value,
    multiplier: root.querySelector("[data-kc-multiplier]").value,
    source: root.querySelector("[data-kc-source]").value,
    use_ema: root.querySelector("[data-kc-ma]").value === "ema",
    band_style: root.querySelector("[data-kc-style]").value,
    atr_length: root.querySelector("[data-kc-atr-length]").value,
    ...(limit === null ? {} : { limit }),
    interval,
  });
  const updateKcSummary = () => {
    const source = root.querySelector("[data-kc-source]").selectedOptions[0].text;
    const basis = root.querySelector("[data-kc-ma]").value === "ema" ? "指数移动平均" : "简单移动平均";
    const style = ({ atr: "平均真实波幅", tr: "真实波幅", range: "最高最低价差" })[root.querySelector("[data-kc-style]").value];
    const periodUnit = interval === "week" ? "周" : "日";
    kcSummary.textContent = `肯特纳通道：${root.querySelector("[data-kc-length]").value}${periodUnit}${basis} · ${root.querySelector("[data-kc-atr-length]").value}${periodUnit}${style} · ${root.querySelector("[data-kc-multiplier]").value}倍 · ${source}`;
  };
  const formatNumber = (value) => Number.isFinite(value) ? value.toFixed(2) : "数据不足";
  const movingAverageFallback = (length) => payload.bars.map((row, index, rows) => {
    if (index + 1 < length) return null;
    const window = rows.slice(index + 1 - length, index + 1);
    return { time: row.trade_date, value: window.reduce((sum, item) => sum + item.close, 0) / length };
  }).filter(Boolean);
  const movingAverage = (length) => (payload.moving_averages?.[String(length)] || movingAverageFallback(length)).map(row => ({
    time: row.time || row.trade_date,
    value: row.value,
  }));
  const latestAverage = (length) => {
    const series = movingAverage(length);
    return series.length ? series[series.length - 1].value : null;
  };
  const relativeText = (current, reference) => current !== null && reference !== null ? `${((current / reference - 1) * 100).toFixed(1)}%` : "数据不足";
  const renderFrameworkPanel = () => {
    if (!payload || !frameworkPanel) return;
    const framework = frameworkForInterval();
    const last = payload.bars[payload.bars.length - 1];
    if (!last) return;
    if (framework === "minervini") {
      const ma50 = latestAverage(50), ma150 = latestAverage(150), ma200 = latestAverage(200);
      const trailing = payload.bars.slice(-260);
      const high52 = trailing.length >= 250 ? Math.max(...trailing.map(row => row.high)) : null;
      const state = root.querySelector('[data-framework-status="minervini"]')?.textContent || "M大师：数据不足";
      const rows = [
        ["规则状态", state], ["收盘 / 50日线", `${formatNumber(last.close)} / ${formatNumber(ma50)} (${relativeText(last.close, ma50)})`],
        ["收盘 / 150日线", `${formatNumber(last.close)} / ${formatNumber(ma150)} (${relativeText(last.close, ma150)})`],
        ["收盘 / 200日线", `${formatNumber(last.close)} / ${formatNumber(ma200)} (${relativeText(last.close, ma200)})`],
        ["150日线高于200日线", ma150 !== null && ma200 !== null ? (ma150 > ma200 ? "是" : "否") : "数据不足"],
        ["距52周高点", high52 === null ? "数据不足" : `${((last.close / high52 - 1) * 100).toFixed(1)}%`],
      ];
      frameworkPanel.innerHTML = `<header><strong>日线 · M大师</strong><span>Minervini 条件参考</span></header><dl>${rows.map(([label, value]) => `<div><dt>${label}</dt><dd>${value}</dd></div>`).join("")}</dl><p>仅呈现规则条件与数据位置，不构成买卖指令。</p>`;
    } else {
      const ma10 = latestAverage(10), ma30 = latestAverage(30);
      const statusNode = root.querySelector('[data-framework-status="weinstein"]');
      const state = statusNode?.textContent || "W大师：数据不足";
      const stage = statusNode?.dataset.frameworkStage || "数据不足";
      const rows = [
        ["当前阶段", stage], ["规则状态", state], ["收盘 / 10周线", `${formatNumber(last.close)} / ${formatNumber(ma10)} (${relativeText(last.close, ma10)})`],
        ["收盘 / 30周线", `${formatNumber(last.close)} / ${formatNumber(ma30)} (${relativeText(last.close, ma30)})`],
        ["10周线高于30周线", ma10 !== null && ma30 !== null ? (ma10 > ma30 ? "是" : "否") : "数据不足"],
      ];
      frameworkPanel.innerHTML = `<header><strong>周线 · W大师</strong><span>Weinstein 阶段参考</span></header><dl>${rows.map(([label, value]) => `<div><dt>${label}</dt><dd>${value}</dd></div>`).join("")}</dl><p>阶段结论以观察池规则状态为准，不构成买卖指令。</p>`;
    }
  };
  const renderTimeline = () => {
    if (!timelineList || !payload) return;
    const events = (payload.method_events || []).filter(item => item.method === frameworkForInterval()).slice().reverse();
    timelineList.replaceChildren(...events.map((event) => {
      const item = document.createElement("li");
      const state = ({ ENTERED: "进入观察", REENTERED: "重新进入", EXITED: "移出观察", DATA_GAP: "数据中断" })[event.state] || event.state;
      item.textContent = `${event.as_of_date} · ${state}${event.summary ? `：${event.summary}` : ""}`;
      return item;
    }));
    if (!events.length) {
      const item = document.createElement("li"); item.textContent = "当前显示区间内没有可用的状态变化记录。"; timelineList.append(item);
    }
  };
  const renderMethodEventDetail = (event) => {
    if (!event) { methodEventDetail.hidden = true; methodEventDetail.replaceChildren(); return; }
    const title = document.createElement("strong");
    const methodName = event.method === "weinstein" ? "Weinstein" : "Minervini";
    const stateName = ({ ENTERED: "进入观察状态", REENTERED: "重新进入观察状态", EXITED: "移出观察状态", DATA_GAP: "判定中断" })[event.state] || event.state;
    title.textContent = `${event.as_of_date} · ${methodName} ${stateName}`;
    const summary = document.createElement("span");
    const failed = (event.failed_checks || []).map(key => minerviniCheckLabels[key] || key);
    summary.textContent = `${event.summary}${failed.length ? `；未满足：${failed.join("、")}` : ""}`;
    const metadata = document.createElement("small");
    const effective = event.method === "weinstein" && event.effective_date !== event.as_of_date
      ? ` · 依据完成周线截至 ${event.effective_date}` : "";
    metadata.textContent = `规则版本：${event.policy_version}${effective}`;
    methodEventDetail.replaceChildren(title, summary, metadata);
    methodEventDetail.hidden = false;
  };
  const renderMarkers = () => {
    if (!payload) return;
    methodEventsById = new Map((payload.method_events || []).map(item => [item.id, item]));
    const trades = (payload.trade_overlay?.executions || []).map(item => ({
      priority: 0,
      marker: {
        id: `trade:${item.execution_id}`, time: item.traded_on,
        position: item.side === "BUY" ? "belowBar" : "aboveBar",
        shape: item.side === "BUY" ? "arrowUp" : "arrowDown",
        color: item.side === "BUY" ? "#c94e47" : "#258460",
        text: `${item.side === "BUY" ? "买" : "卖"} ${item.price} ×${item.quantity}`,
      },
    }));
    const methods = (payload.method_events || [])
      .filter(item => frameworkLayerVisible() && item.method === frameworkForInterval())
      .map(item => ({
        priority: item.method === "weinstein" ? 1 : 2,
        marker: {
          id: item.id,
          time: item.plot_date,
          position: ["ENTERED", "REENTERED"].includes(item.state) ? "belowBar" : "aboveBar",
          shape: item.method === "weinstein" ? "circle" : "square",
          color: item.state === "DATA_GAP" ? "#7a7f86" : (item.method === "weinstein" ? "#315a9a" : "#986719"),
          text: item.label,
          size: 0.8,
        },
      }));
    markersApi.setMarkers([...trades, ...methods]
      .sort((left, right) => left.marker.time.localeCompare(right.marker.time) || left.priority - right.priority || left.marker.id.localeCompare(right.marker.id))
      .map(item => item.marker));
  };
  const selectedDrawing = () => drawings.find(drawing => drawing.drawing_id === selectedDrawingId) || null;
  const updateDrawingActions = () => {
    drawingActions.hidden = drawings.length === 0;
    deleteSelectedButton.disabled = !selectedDrawing();
    editSelectedButton.hidden = selectedDrawing()?.tool !== "text";
  };
  const setActiveTool = (tool) => {
    activeTool = tool;
    pendingAnchor = null;
    previewAnchor = null;
    overlay.style.pointerEvents = activeTool === "browse" ? "none" : "auto";
    overlay.style.cursor = activeTool === "browse" ? "default" : "crosshair";
    root.querySelectorAll("[data-draw-tool]").forEach(item => item.setAttribute("aria-pressed", String(item.dataset.drawTool === activeTool)));
    drawingHint.textContent = ({ browse: "拖拽缩放或查看价格", select: "点击标记选中；按删除键、退格键或右键删除", trendline: "点击起点，再点击终点", horizontal: "点击图表设置水平线", text: "点击 K 线或形态附近添加文字" })[activeTool] || "选择工具后在图上点击";
    renderDrawings();
  };
  const syncVolumePane = () => {
    const targetPaneIndex = volumeInput.checked ? 1 : 0;
    if (volume.getPane().paneIndex() !== targetPaneIndex) volume.moveToPane(targetPaneIndex);
    volume.applyOptions({ visible: volumeInput.checked });
  };
  const resize = () => {
    const rect = chartNode.getBoundingClientRect();
    const chartHeight = Math.max(520, Math.floor(rect.height || 640));
    const volumeHeight = volumeInput.checked ? Math.max(104, Math.round(chartHeight * 0.2)) : 0;
    const drawingHeight = chartHeight - volumeHeight;
    chart.applyOptions({ width: Math.max(320, Math.floor(rect.width)), height: chartHeight });
    if (volumeInput.checked) volume.getPane().setHeight(volumeHeight);
    const ratio = window.devicePixelRatio || 1;
    overlay.width = Math.floor(rect.width * ratio); overlay.height = Math.floor(drawingHeight * ratio);
    overlay.style.width = `${rect.width}px`; overlay.style.height = `${drawingHeight}px`;
    if (textLayer) { textLayer.style.width = `${rect.width}px`; textLayer.style.height = `${drawingHeight}px`; }
    context.setTransform(ratio, 0, 0, ratio, 0, 0); renderDrawings();
  };
  const fitBarsToViewport = () => {
    if (!payload?.bars?.length) return;
    const width = Math.max(320, chartNode.getBoundingClientRect().width);
    const padding = 10;
    chart.timeScale().applyOptions({
      barSpacing: Math.max(2, Math.min(10, width / (payload.bars.length + padding * 2))),
      rightOffset: padding,
    });
    chart.timeScale().setVisibleLogicalRange({ from: -padding, to: payload.bars.length + padding });
  };
  const logicalForAnchor = (anchor) => {
    if (typeof anchor.logical_from_end === "number") return payload.bars.length - 1 + anchor.logical_from_end;
    if (typeof anchor.logical_from_start === "number") return anchor.logical_from_start;
    return anchor.logical;
  };
  const coord = (anchor) => ({
    x: anchor.date
      ? chart.timeScale().timeToCoordinate(anchor.date)
      : chart.timeScale().logicalToCoordinate(logicalForAnchor(anchor)),
    y: candles.priceToCoordinate(anchor.price),
  });
  const canonicalizeDrawing = (drawing) => {
    const cached = canonicalDrawingAnchors.get(drawing.drawing_id);
    if (cached) return { ...drawing, anchors: cached };
    const lastLogical = payload.bars.length - 1;
    const anchors = drawing.anchors.map((anchor) => {
      if (anchor.date || typeof anchor.logical !== "number") return anchor;
      if (anchor.logical > lastLogical) return { logical_from_end: Number((anchor.logical - lastLogical).toFixed(4)), price: anchor.price };
      if (anchor.logical < 0) return { logical_from_start: anchor.logical, price: anchor.price };
      return anchor;
    });
    canonicalDrawingAnchors.set(drawing.drawing_id, anchors);
    return { ...drawing, anchors };
  };
  const drawOne = (drawing, highlight = false) => {
    if (drawing.tool === "text") return;
    const anchors = drawing.anchors.map(coord);
    if (anchors.some(point => point.x === null || point.y === null)) return;
    const stroke = () => {
      context.beginPath();
      if (drawing.tool !== "trendline") { context.moveTo(0, anchors[0].y); context.lineTo(overlay.clientWidth, anchors[0].y); }
      else { context.moveTo(anchors[0].x, anchors[0].y); context.lineTo(anchors[1].x, anchors[1].y); }
      context.stroke();
    };
    context.save();
    const styles = { horizontal: ["#8d6c31", [5, 4]] };
    if (highlight) {
      context.strokeStyle = "#fffdfa"; context.lineWidth = 7; context.setLineDash([]); stroke();
      context.strokeStyle = "#c83b2b"; context.lineWidth = 3; context.setLineDash(drawing.tool !== "trendline" ? [7, 4] : []); stroke();
      anchors.forEach(point => { context.beginPath(); context.fillStyle = "#fffdfa"; context.strokeStyle = "#c83b2b"; context.lineWidth = 2; context.arc(point.x, point.y, 5, 0, Math.PI * 2); context.fill(); context.stroke(); });
    } else {
      context.strokeStyle = (styles[drawing.tool] || ["#8d6c31"])[0]; context.lineWidth = 1.5; context.setLineDash((styles[drawing.tool] || [null, []])[1] || []); stroke();
    }
    context.restore();
  };
  const renderDrawings = () => {
    context.clearRect(0, 0, overlay.clientWidth, overlay.clientHeight);
    drawings.forEach(drawing => drawOne(drawing, drawing.drawing_id === selectedDrawingId));
    if (pendingAnchor && previewAnchor) drawOne({ tool: "trendline", anchors: [pendingAnchor, previewAnchor] }, true);
    renderTextDrawings();
    positionTextEditor();
  };
  const chartRequest = (targetSymbol, targetDate, targetSettings) => {
    const query = new URLSearchParams({ date: targetDate, ...targetSettings });
    return `/api/a/stocks/${encodeURIComponent(targetSymbol)}/chart?${query}`;
  };
  const chartCacheKey = (url) => `masterstock-chart:${url}`;
  const chartCacheIndexKey = "masterstock-chart:index:v2";
  const chartCacheLimit = 12;
  const cachedPayload = (url) => {
    const key = chartCacheKey(url);
    try { return JSON.parse(window.sessionStorage.getItem(key) || "null"); }
    catch (_) {
      try { window.sessionStorage.removeItem(key); } catch (_) { /* Storage can be unavailable. */ }
      return null;
    }
  };
  const storePayload = (url, value) => {
    const key = chartCacheKey(url);
    const serialized = JSON.stringify(value);
    const write = () => {
      window.sessionStorage.setItem(key, serialized);
      const prior = JSON.parse(window.sessionStorage.getItem(chartCacheIndexKey) || "[]");
      const index = [key, ...prior.filter(item => item !== key)];
      index.slice(chartCacheLimit).forEach(item => window.sessionStorage.removeItem(item));
      window.sessionStorage.setItem(chartCacheIndexKey, JSON.stringify(index.slice(0, chartCacheLimit)));
    };
    try { write(); }
    catch (_) {
      try {
        const staleKeys = [];
        for (let index = 0; index < window.sessionStorage.length; index += 1) {
          const item = window.sessionStorage.key(index);
          if (item?.startsWith("masterstock-chart:") && item !== chartCacheIndexKey) staleKeys.push(item);
        }
        staleKeys.forEach(item => window.sessionStorage.removeItem(item));
        window.sessionStorage.removeItem(chartCacheIndexKey);
        write();
      } catch (_) { /* Browser storage can be unavailable or too small. */ }
    }
  };
  const chartDate = (value) => typeof value === "string" ? value : `${value.year}-${String(value.month).padStart(2, "0")}-${String(value.day).padStart(2, "0")}`;
  const compactVolume = (value) => {
    if (!Number.isFinite(value)) return "—";
    if (Math.abs(value) >= 100000000) return `${(value / 100000000).toFixed(2)}亿`;
    if (Math.abs(value) >= 10000) return `${(value / 10000).toFixed(1)}万`;
    return String(Math.round(value));
  };
  const updateChangeReadout = (row) => {
    if (!row) return;
    const label = interval === "week" ? "本周涨跌幅" : "当日涨跌幅";
    const value = typeof row.change_pct === "number" ? row.change_pct : null;
    changeDate.textContent = row.trade_date;
    changeValue.textContent = `${label} ${value === null ? "—" : `${value > 0 ? "+" : ""}${value.toFixed(2)}%`}`;
    changeReadout.classList.toggle("is-up", value !== null && value > 0);
    changeReadout.classList.toggle("is-down", value !== null && value < 0);
    openValue.textContent = formatNumber(Number(row.open));
    highValue.textContent = formatNumber(Number(row.high));
    lowValue.textContent = formatNumber(Number(row.low));
    closeValue.textContent = formatNumber(Number(row.close));
    volumeValue.textContent = compactVolume(Number(row.volume));
  };
  const chartFailureText = (reason) => ({
    INSUFFICIENT_DAILY_OHLC: "当前日期之前的有效行情不足，无法绘制价格图。",
    INCONSISTENT_PRICE_SCALE: "前复权价格尺度不一致，已停止混合展示。",
  })[reason] || "图表数据暂时不可用，请稍后重试。";
  const setChartState = (mode, detail = "") => {
    const loading = mode === "loading";
    root.setAttribute("aria-busy", String(loading));
    stateOverlay.hidden = mode === "idle" || mode === "refreshing";
    retryButton.hidden = mode !== "error";
    if (loading) {
      stateTitle.textContent = "正在载入图表";
      stateDetail.textContent = detail || "正在读取前复权行情与方法事实…";
    } else if (mode === "error") {
      stateTitle.textContent = "图表暂时无法显示";
      stateDetail.textContent = detail;
    }
  };
  const renderAnomalyEvidence = (evidence) => {
    if (!anomalyRoot) return;
    anomalyRoot.setAttribute("aria-busy", "false");
    anomalyLabel.textContent = evidence.label || "数据不足";
    anomalyLabel.className = `tag ${evidence.status === "REVIEW" ? "tag-risk" : evidence.status === "CLEAR" ? "tag-pass" : "tag-caution"}`;
    anomalyConclusion.textContent = evidence.conclusion || "异常证据暂不可用。";
    anomalyMetrics.replaceChildren(...(evidence.evidence || []).map((item) => {
      const metric = document.createElement("span");
      metric.title = item.detail || "";
      const label = document.createElement("small"); label.textContent = item.label || "";
      const value = document.createElement("b"); value.textContent = item.value === null || item.value === undefined ? "—" : `${item.value}${item.unit || ""}`;
      const state = document.createElement("em"); state.className = `anomaly-state-${String(item.state || "insufficient").toLowerCase()}`; state.textContent = item.state_label || "数据不足";
      metric.append(label, value, state);
      return metric;
    }));
    anomalyDetails.replaceChildren(...(evidence.evidence || []).map((item) => {
      const row = document.createElement("li");
      const label = document.createElement("strong"); label.textContent = item.label || "";
      const detail = document.createElement("span"); detail.textContent = item.detail || "";
      row.append(label, detail);
      return row;
    }));
    anomalyPolicy.textContent = `规则版本：${evidence.policy_version || "数据不足"}。只提供复核证据，不改变 Weinstein 或 Minervini 结论，不构成操纵认定或买卖指令。`;
  };
  const loadAnomalyEvidence = async () => {
    if (!anomalyRoot?.dataset.anomalyUrl) return;
    try {
      const response = await fetch(anomalyRoot.dataset.anomalyUrl, { credentials: "same-origin" });
      if (!response.ok) throw new Error("anomaly request failed");
      renderAnomalyEvidence(await response.json());
    } catch (_) {
      renderAnomalyEvidence({ status: "DATA_INSUFFICIENT", label: "暂不可用", conclusion: "异常证据加载失败，不影响 K 线与大师方法事实。", evidence: [] });
    }
  };
  const emptyPrivateOverlay = () => ({ drawings: [], trade_overlay: { executions: [], open_stops: [] } });
  const loadPrivateOverlay = async (publicPayload) => {
    if (!privateOverlayUrl || publicPayload.status !== "OK") return emptyPrivateOverlay();
    const query = new URLSearchParams({
      date,
      price_scale_id: publicPayload.price_scale_id,
      interval: publicPayload.interval || interval,
    });
    try {
      const response = await fetch(`${privateOverlayUrl}?${query}`, { credentials: "same-origin" });
      if (!response.ok) return emptyPrivateOverlay();
      return await response.json();
    } catch (_) {
      return emptyPrivateOverlay();
    }
  };
  const withPrivateOverlay = (publicPayload, privateOverlay) => ({
    ...publicPayload,
    ...emptyPrivateOverlay(),
    ...privateOverlay,
  });
  const drawPayload = (nextPayload) => {
    payload = nextPayload;
    candles.setData(payload.bars.map(row => ({ time: row.trade_date, open: row.open, high: row.high, low: row.low, close: row.close })));
    const values = (key) => keltnerInput.checked ? payload.keltner.filter(row => row[key] !== null).map(row => ({ time: row.date, value: row[key] })) : [];
    upper.setData(values("upper")); basis.setData(values("basis")); lower.setData(values("lower"));
    const framework = frameworkForInterval();
    const showFrameworkLayer = frameworkLayerVisible();
    root.querySelectorAll("[data-framework-legend]").forEach(item => { item.hidden = item.dataset.frameworkLegend !== framework || !showFrameworkLayer; });
    root.querySelector("[data-keltner-legend]").hidden = !keltnerInput.checked;
    maM50.setData(framework === "minervini" && showFrameworkLayer ? movingAverage(50) : []);
    maM150.setData(framework === "minervini" && showFrameworkLayer ? movingAverage(150) : []);
    maM200.setData(framework === "minervini" && showFrameworkLayer ? movingAverage(200) : []);
    maW10.setData(framework === "weinstein" && showFrameworkLayer ? movingAverage(10) : []);
    maW30.setData(framework === "weinstein" && showFrameworkLayer ? movingAverage(30) : []);
    const activeCandleStyle = candleStyles[candleStyle];
    candles.applyOptions(activeCandleStyle.candles);
    volume.setData(payload.bars.map(row => ({
      time: row.trade_date,
      value: row.volume || 0,
      color: row.close >= row.open ? activeCandleStyle.volumeUpColor : activeCandleStyle.volumeDownColor,
    })));
    if (tradeLegend) tradeLegend.hidden = !(payload.trade_overlay?.executions || []).length;
    syncVolumePane();
    renderMarkers();
    renderFrameworkPanel();
    renderTimeline();
    renderMethodEventDetail(null);
    drawings = (payload.drawings || []).map(canonicalizeDrawing);
    if (!selectedDrawing()) selectedDrawingId = null;
    updateChangeReadout(payload.bars[payload.bars.length - 1]);
    updateDrawingActions(); resize(); fitBarsToViewport();
  };
  const request = async () => {
    const version = ++requestVersion;
    requestController?.abort();
    const controller = new AbortController();
    requestController = controller;
    const url = chartRequest(symbol, date, settings());
    const cached = cachedPayload(url);
    let overlayRequest = null;
    let freshPayloadDrawn = false;
    setChartState(cached?.status === "OK" ? "refreshing" : "loading");
    loadState.textContent = cached?.status === "OK" ? "正在检查更新…" : "";
    try {
      if (cached?.status === "OK") {
        drawPayload(cached);
        overlayRequest = loadPrivateOverlay(cached);
        void overlayRequest.then((privateOverlay) => {
          if (version === requestVersion && !freshPayloadDrawn) drawPayload(withPrivateOverlay(cached, privateOverlay));
        });
      }
      const response = await fetch(url, { signal: controller.signal });
      const nextPayload = await response.json();
      if (version !== requestVersion) return;
      if (!response.ok || nextPayload.status !== "OK") {
        if (cached?.status === "OK") {
          loadState.textContent = "已显示缓存数据；更新数据不足";
          setChartState("idle");
        } else {
          loadState.textContent = "";
          setChartState("error", chartFailureText(nextPayload.reason));
        }
        return;
      }
      storePayload(url, nextPayload);
      if (version === requestVersion) {
        freshPayloadDrawn = true;
        drawPayload(nextPayload);
        setChartState("idle");
        scheduleAdjacentChartPrefetch();
        const canReuseOverlay = overlayRequest
          && cached?.price_scale_id === nextPayload.price_scale_id
          && (cached.interval || interval) === (nextPayload.interval || interval);
        const privateOverlay = await (canReuseOverlay ? overlayRequest : loadPrivateOverlay(nextPayload));
        if (version === requestVersion) drawPayload(withPrivateOverlay(nextPayload, privateOverlay));
      }
    } catch (error) {
      if (error?.name === "AbortError") return;
      console.error("Could not render stock chart", error);
      if (version === requestVersion) {
        if (cached?.status === "OK") {
          loadState.textContent = "已显示缓存数据";
          setChartState("idle");
        } else {
          loadState.textContent = "";
          setChartState("error", "网络或服务异常，请重试。");
        }
      }
    } finally {
      if (version === requestVersion) {
        requestController = null;
        root.setAttribute("aria-busy", "false");
        if (loadState.textContent === "正在检查更新…") loadState.textContent = "";
      }
    }
  };
  const prefetchAdjacentCharts = async () => {
    const links = [root.querySelector("[data-chart-next]"), root.querySelector("[data-chart-previous]")].filter(Boolean);
    for (const link of links) {
      const match = new URL(link.href, window.location.origin).pathname.match(/\/a\/stocks\/([^/]+)\/chart$/);
      if (!match) continue;
      const url = chartRequest(decodeURIComponent(match[1]), date, settings());
      if (cachedPayload(url)?.status === "OK") continue;
      try {
        const response = await fetch(url);
        const nextPayload = await response.json();
        if (response.ok && nextPayload.status === "OK") storePayload(url, nextPayload);
      } catch (_) { /* Prefetch is opportunistic. */ }
    }
  };
  const scheduleAdjacentChartPrefetch = () => {
    const signature = JSON.stringify(settings());
    if (adjacentPrefetchSignature === signature) return;
    adjacentPrefetchSignature = signature;
    const run = () => { void prefetchAdjacentCharts(); };
    if ("requestIdleCallback" in window) window.requestIdleCallback(run, { timeout: 1500 });
    else window.setTimeout(run, 350);
  };
  const pointFromEvent = (event) => {
    const rect = overlay.getBoundingClientRect(); const x = event.clientX - rect.left; const y = event.clientY - rect.top;
    const time = chart.timeScale().coordinateToTime(x);
    const logical = chart.timeScale().coordinateToLogical(x);
    const price = candles.coordinateToPrice(y);
    if (!price) return null;
    if (time) {
      return {
        date: typeof time === "string" ? time : `${time.year}-${String(time.month).padStart(2, "0")}-${String(time.day).padStart(2, "0")}`,
        price: Number(price.toFixed(6)),
      };
    }
    if (typeof logical !== "number") return null;
    const lastLogical = payload.bars.length - 1;
    if (logical > lastLogical) return { logical_from_end: Number((logical - lastLogical).toFixed(4)), price: Number(price.toFixed(6)) };
    if (logical < 0) return { logical_from_start: Number(logical.toFixed(4)), price: Number(price.toFixed(6)) };
    return { logical: Number(logical.toFixed(4)), price: Number(price.toFixed(6)) };
  };
  const saveDrawing = async (drawing) => {
    if (!privateOverlayUrl) throw new Error("请先登录");
    const response = await fetch(`/api/me/stocks/${encodeURIComponent(symbol)}/chart/drawings`, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken }, body: JSON.stringify({ ...drawing, price_scale_id: payload.price_scale_id }) });
    if (!response.ok) throw new Error("保存画线失败");
    const saved = await response.json();
    canonicalDrawingAnchors.set(saved.drawing_id, saved.anchors);
    const existingIndex = drawings.findIndex(item => item.drawing_id === saved.drawing_id);
    if (existingIndex === -1) drawings.push(saved);
    else drawings.splice(existingIndex, 1, saved);
    selectedDrawingId = saved.drawing_id;
    updateDrawingActions(); renderDrawings();
  };
  const removeDrawing = async (drawing) => {
    if (!privateOverlayUrl) throw new Error("请先登录");
    const response = await fetch(`/api/me/stocks/${encodeURIComponent(symbol)}/chart/drawings/${encodeURIComponent(drawing.drawing_id)}?price_scale_id=${encodeURIComponent(payload.price_scale_id)}`, { method: "DELETE", headers: { "X-CSRF-Token": csrfToken } });
    if (!response.ok) throw new Error("删除画线失败");
    drawings = drawings.filter(item => item.drawing_id !== drawing.drawing_id);
    canonicalDrawingAnchors.delete(drawing.drawing_id);
    if (selectedDrawingId === drawing.drawing_id) selectedDrawingId = null;
    updateDrawingActions(); renderDrawings();
  };
  const nearestDrawing = (event) => {
    const rect = overlay.getBoundingClientRect(); const x = event.clientX - rect.left; const y = event.clientY - rect.top;
    return drawings.find((drawing) => { if (drawing.tool === "text") return false; const points = drawing.anchors.map(coord); if (points.some(point => point.x === null || point.y === null)) return false; if (drawing.tool === "horizontal") return Math.abs(y - points[0].y) < 8; const [a, b] = points; const distance = Math.abs((b.y - a.y) * x - (b.x - a.x) * y + b.x * a.y - b.y * a.x) / Math.hypot(b.y - a.y, b.x - a.x); return distance < 8; });
  };
  const selectDrawingAt = (event) => {
    const drawing = nearestDrawing(event);
    selectedDrawingId = drawing ? drawing.drawing_id : null;
    updateDrawingActions(); renderDrawings();
    drawingHint.textContent = drawing ? "已选中；按删除键、退格键或右键删除" : "未选中画线";
    return drawing;
  };
  function positionTextEditor() {
    if (!textEditor || textEditor.hidden || !editorAnchor) return;
    const point = coord(editorAnchor);
    if (point.x === null || point.y === null) { textEditor.hidden = true; return; }
    const width = textEditor.offsetWidth || 310;
    const height = textEditor.offsetHeight || 170;
    const maxLeft = Math.max(8, overlay.clientWidth - width - 8);
    const maxTop = Math.max(8, overlay.clientHeight - height - 8);
    textEditor.style.left = `${Math.min(Math.max(8, point.x + 12), maxLeft)}px`;
    textEditor.style.top = `${Math.min(Math.max(8, point.y + 10), maxTop)}px`;
  }
  function closeTextEditor() {
    if (!textEditor) return;
    textEditor.hidden = true;
    editorAnchor = null;
    editingDrawingId = null;
    textInput.value = "";
    textCount.textContent = "0";
  }
  function openTextEditor(anchor, drawing = null) {
    if (!textEditor) return;
    editorAnchor = anchor;
    editingDrawingId = drawing?.drawing_id || null;
    textInput.value = drawing?.content_text || "";
    textCount.textContent = String(textInput.value.length);
    textEditor.hidden = false;
    positionTextEditor();
    window.requestAnimationFrame(() => { textInput.focus(); textInput.setSelectionRange(textInput.value.length, textInput.value.length); });
  }
  function renderTextDrawings() {
    if (!textLayer) return;
    const fragment = document.createDocumentFragment();
    drawings.filter(drawing => drawing.tool === "text").forEach((drawing) => {
      const point = coord(drawing.anchors[0]);
      if (point.x === null || point.y === null) return;
      const label = document.createElement("button");
      label.type = "button";
      label.className = `drawing-text-label${drawing.drawing_id === selectedDrawingId ? " is-selected" : ""}`;
      label.dataset.drawingId = drawing.drawing_id;
      label.textContent = drawing.content_text;
      label.title = drawing.content_text;
      label.setAttribute("aria-label", `文字标记：${drawing.content_text}`);
      label.style.left = `${Math.min(Math.max(8, point.x + 10), Math.max(8, overlay.clientWidth - 228))}px`;
      label.style.top = `${Math.max(8, point.y - 10)}px`;
      label.addEventListener("click", (event) => {
        event.stopPropagation();
        selectedDrawingId = drawing.drawing_id;
        updateDrawingActions(); renderDrawings();
      });
      label.addEventListener("dblclick", (event) => {
        event.preventDefault(); event.stopPropagation();
        selectedDrawingId = drawing.drawing_id;
        updateDrawingActions(); openTextEditor(drawing.anchors[0], drawing);
      });
      label.addEventListener("contextmenu", async (event) => {
        event.preventDefault(); event.stopPropagation();
        try { await removeDrawing(drawing); drawingHint.textContent = "已删除文字标记"; }
        catch (_) { drawingHint.textContent = "删除失败，请重试"; }
      });
      fragment.append(label);
    });
    textLayer.replaceChildren(fragment);
    textLayer.querySelectorAll(".drawing-text-label").forEach((label) => {
      const top = Number.parseFloat(label.style.top);
      if (top + label.offsetHeight > overlay.clientHeight - 8) label.style.top = `${Math.max(8, overlay.clientHeight - label.offsetHeight - 8)}px`;
    });
  }
  const persistTextEditor = async () => {
    if (!editorAnchor || !textInput) return;
    const content = textInput.value.trim();
    if (!content) { textInput.setCustomValidity("请输入文字标记"); textInput.reportValidity(); return; }
    textInput.setCustomValidity("");
    textSaveButton.disabled = true;
    try {
      await saveDrawing({ drawing_id: editingDrawingId || undefined, tool: "text", anchors: [editorAnchor], content_text: content, interval_scope: interval });
      closeTextEditor(); setActiveTool("select"); drawingHint.textContent = "文字标记已保存，已切换至选择";
    } catch (_) { drawingHint.textContent = "文字标记保存失败，请重试"; }
    finally { textSaveButton.disabled = false; }
  };
  overlay.addEventListener("pointermove", (event) => {
    if (!pendingAnchor || activeTool !== "trendline") return;
    previewAnchor = pointFromEvent(event);
    renderDrawings();
  });
  overlay.addEventListener("pointerdown", async (event) => {
    if (!payload || activeTool === "browse") return;
    if (activeTool === "select") { selectDrawingAt(event); return; }
    const point = pointFromEvent(event); if (!point) return;
    try {
      if (activeTool === "horizontal") { await saveDrawing({ tool: activeTool, anchors: [point] }); setActiveTool("select"); drawingHint.textContent = "水平线已保存，已切换至选择"; return; }
      if (activeTool === "text") { openTextEditor(point); return; }
      if (!pendingAnchor) { pendingAnchor = point; previewAnchor = point; drawingHint.textContent = "已选起点，移动鼠标预览，点击终点"; renderDrawings(); }
      else { const first = pendingAnchor; pendingAnchor = null; previewAnchor = null; await saveDrawing({ tool: "trendline", anchors: [first, point] }); setActiveTool("select"); drawingHint.textContent = "趋势线已保存，已切换至选择"; }
    } catch (_) { drawingHint.textContent = "保存失败，请重试"; }
  });
  root.querySelector(".stock-chart-stage").addEventListener("contextmenu", async (event) => {
    if (!payload) return;
    const drawing = nearestDrawing(event);
    if (!drawing) return;
    event.preventDefault();
    try { await removeDrawing(drawing); drawingHint.textContent = "已删除画线"; } catch (_) { drawingHint.textContent = "删除失败，请重试"; }
  });
  document.addEventListener("keydown", async (event) => {
    if (!["Delete", "Backspace"].includes(event.key) || !selectedDrawing() || ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) return;
    event.preventDefault();
    try { await removeDrawing(selectedDrawing()); drawingHint.textContent = "已删除选中画线"; } catch (_) { drawingHint.textContent = "删除失败，请重试"; }
  });
  root.querySelectorAll("[data-draw-tool]").forEach(button => button.addEventListener("click", () => { if (!textEditor?.hidden) closeTextEditor(); setActiveTool(button.dataset.drawTool); }));
  editSelectedButton.addEventListener("click", () => { const drawing = selectedDrawing(); if (drawing?.tool === "text") openTextEditor(drawing.anchors[0], drawing); });
  textInput?.addEventListener("input", () => { textInput.setCustomValidity(""); textCount.textContent = String(textInput.value.length); });
  textInput?.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { event.preventDefault(); closeTextEditor(); return; }
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); persistTextEditor(); }
  });
  textSaveButton?.addEventListener("click", persistTextEditor);
  textCancelButton?.addEventListener("click", closeTextEditor);
  volumeInput.addEventListener("change", () => {
    storeLayerSettings();
    syncVolumePane();
    if (payload) drawPayload(payload);
    else resize();
  });
  keltnerInput.addEventListener("change", () => { storeLayerSettings(); if (payload) drawPayload(payload); });
  root.querySelectorAll("[data-framework-layer]").forEach(input => input.addEventListener("change", () => {
    storeLayerSettings();
    if (payload) drawPayload(payload);
  }));
  retryButton?.addEventListener("click", request);
  chart.subscribeClick((param) => {
    if (!payload || !param.time) return;
    const methodEvent = methodEventsById.get(param.hoveredObjectId || "");
    if (methodEvent) { renderMethodEventDetail(methodEvent); return; }
    const selectedDate = chartDate(param.time);
    const matches = (payload.trade_overlay?.executions || []).filter(item => item.traded_on === selectedDate);
    if (matches.length) window.location.assign(`/a/stocks/${encodeURIComponent(symbol)}?edit_trade=${encodeURIComponent(matches[0].execution_id)}#trade-journal`);
  });
  chart.subscribeCrosshairMove((param) => {
    if (!payload?.bars?.length) return;
    const selectedDate = param.time ? chartDate(param.time) : "";
    updateChangeReadout(payload.bars.find(row => row.trade_date === selectedDate) || payload.bars[payload.bars.length - 1]);
  });
  deleteSelectedButton.addEventListener("click", async () => { const drawing = selectedDrawing(); if (!drawing) return; try { await removeDrawing(drawing); drawingHint.textContent = "已删除选中画线"; } catch (_) { drawingHint.textContent = "删除失败，请重试"; } });
  root.querySelector("[data-clear-drawings]").addEventListener("click", async () => { if (!payload || !drawings.length || !window.confirm("清空当前周期下显示的全部个人标记？")) return; for (const drawing of [...drawings]) await removeDrawing(drawing); });
  const chartReviewForm = document.querySelector(".chart-review-control");
  document.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key) || event.altKey || event.ctrlKey || event.metaKey || event.isComposing) return;
    const active = document.activeElement;
    if (active?.isContentEditable || ["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(active?.tagName)) return;
    const target = document.querySelector(event.key === "ArrowLeft" ? "[data-chart-previous]" : "[data-chart-next]");
    if (!target) return;
    event.preventDefault();
    window.location.assign(target.href);
  });
  root.querySelectorAll("[data-chart-limit]").forEach(button => button.addEventListener("click", () => { limit = button.dataset.chartLimit === "all" ? null : Number(button.dataset.chartLimit); syncChartViewControls(); storeChartView(); request(); }));
  root.querySelectorAll("[data-chart-interval]").forEach(button => button.addEventListener("click", () => { closeTextEditor(); interval = button.dataset.chartInterval; applyKcPreset(defaultKcPresetForInterval()); syncChartViewControls(); updateKcSummary(); renderMethodEventDetail(null); storeChartView(); request(); }));
  root.querySelectorAll("[data-candle-style]").forEach(button => button.addEventListener("click", () => {
    candleStyle = button.dataset.candleStyle;
    syncAppearanceControls();
    storeAppearance();
    if (payload) drawPayload(payload);
    else candles.applyOptions(candleStyles[candleStyle].candles);
  }));
  kcPresetInput.addEventListener("change", () => { if (kcPresetInput.value !== "custom") applyKcPreset(kcPresetInput.value); updateKcSummary(); request(); });
  root.querySelectorAll("[data-kc-ma], [data-kc-style], [data-kc-length], [data-kc-atr-length], [data-kc-multiplier], [data-kc-source]").forEach(input => input.addEventListener("change", () => { kcPresetInput.value = "custom"; updateKcSummary(); request(); }));
  chart.timeScale().subscribeVisibleTimeRangeChange(renderDrawings); window.addEventListener("resize", resize); setActiveTool("browse"); updateKcSummary(); syncVolumePane(); resize(); void loadAnomalyEvidence(); request();
})();
