(() => {
  const root = document.querySelector("[data-stock-chart]");
  if (!root || !window.LightweightCharts) return;
  const chartNode = root.querySelector("[data-chart-canvas]");
  const overlay = root.querySelector("[data-drawing-overlay]");
  const context = overlay.getContext("2d");
  const volumeInput = root.querySelector("[data-volume]");
  const kcSummary = root.querySelector("[data-kc-summary]");
  const drawingActions = root.querySelector("[data-drawing-actions]");
  const drawingHint = root.querySelector("[data-drawing-hint]");
  const deleteSelectedButton = root.querySelector("[data-delete-selected]");
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
  let recordMode = false;
  const canonicalDrawingAnchors = new Map();
  const chart = LightweightCharts.createChart(chartNode, {
    layout: { background: { color: "#fffdfa" }, textColor: "#142d4c" },
    grid: { vertLines: { color: "#eee8de" }, horzLines: { color: "#eee8de" } },
    rightPriceScale: { borderColor: "#d8d0c3" }, timeScale: { borderColor: "#d8d0c3", timeVisible: true },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    localization: { locale: "zh-CN" },
  });
  const candles = chart.addSeries(LightweightCharts.CandlestickSeries, {
    upColor: "#c94e47", downColor: "#258460", borderVisible: false, wickUpColor: "#c94e47", wickDownColor: "#258460",
  });
  const upper = chart.addSeries(LightweightCharts.LineSeries, { color: "#a9b0b7", lineWidth: 1, lastValueVisible: false, priceLineVisible: false });
  const basis = chart.addSeries(LightweightCharts.LineSeries, { color: "#b9bec4", lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, lastValueVisible: false, priceLineVisible: false });
  const lower = chart.addSeries(LightweightCharts.LineSeries, { color: "#a9b0b7", lineWidth: 1, lastValueVisible: false, priceLineVisible: false });
  const ma20 = chart.addSeries(LightweightCharts.LineSeries, { color: "#d28b18", lineWidth: 1, lastValueVisible: false, priceLineVisible: false });
  const ma50 = chart.addSeries(LightweightCharts.LineSeries, { color: "#5b6fc7", lineWidth: 1, lastValueVisible: false, priceLineVisible: false });
  const initialVolumePane = chart.addPane();
  initialVolumePane.setHeight(132);
  const volume = chart.addSeries(LightweightCharts.HistogramSeries, { priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false, base: 0 }, initialVolumePane.paneIndex());
  volume.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0 } });
  const markersApi = LightweightCharts.createSeriesMarkers(candles, []);

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
    kcSummary.textContent = `肯特纳通道：${root.querySelector("[data-kc-length]").value}日${basis} · ${root.querySelector("[data-kc-atr-length]").value}日${style} · ${root.querySelector("[data-kc-multiplier]").value}倍 · ${source}`;
  };
  const selectedDrawing = () => drawings.find(drawing => drawing.drawing_id === selectedDrawingId) || null;
  const updateDrawingActions = () => {
    drawingActions.hidden = drawings.length === 0;
    deleteSelectedButton.disabled = !selectedDrawing();
  };
  const setActiveTool = (tool) => {
    recordMode = false;
    activeTool = tool;
    pendingAnchor = null;
    previewAnchor = null;
    overlay.style.pointerEvents = activeTool === "browse" ? "none" : "auto";
    overlay.style.cursor = activeTool === "browse" ? "default" : "crosshair";
    root.querySelectorAll("[data-draw-tool]").forEach(item => item.setAttribute("aria-pressed", String(item.dataset.drawTool === activeTool)));
    drawingHint.textContent = ({ browse: "拖拽缩放或查看价格", select: "点击画线选中；按删除键、退格键或右键删除", trendline: "点击起点，再点击终点", horizontal: "点击图表设置水平线" })[activeTool] || "选择工具后在图上点击";
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
  };
  const chartRequest = (targetSymbol, targetDate, targetSettings) => {
    const query = new URLSearchParams({ date: targetDate, ...targetSettings });
    return `/api/a/stocks/${encodeURIComponent(targetSymbol)}/chart?${query}`;
  };
  const chartCacheKey = (url) => `masterstock-chart:${url}`;
  const cachedPayload = (url) => {
    try { return JSON.parse(window.sessionStorage.getItem(chartCacheKey(url)) || "null"); } catch (_) { return null; }
  };
  const storePayload = (url, value) => {
    try { window.sessionStorage.setItem(chartCacheKey(url), JSON.stringify(value)); } catch (_) { /* Browser storage can be unavailable. */ }
  };
  const withPrivateOverlay = async (publicPayload) => {
    const combined = { ...publicPayload, drawings: [], trade_overlay: { executions: [], open_stops: [] } };
    if (!privateOverlayUrl || publicPayload.status !== "OK") return combined;
    const query = new URLSearchParams({
      date,
      price_scale_id: publicPayload.price_scale_id,
      interval: publicPayload.interval || interval,
    });
    try {
      const response = await fetch(`${privateOverlayUrl}?${query}`, { credentials: "same-origin" });
      if (!response.ok) return combined;
      return { ...combined, ...(await response.json()) };
    } catch (_) {
      return combined;
    }
  };
  const drawPayload = (nextPayload) => {
    payload = nextPayload;
    candles.setData(payload.bars.map(row => ({ time: row.trade_date, open: row.open, high: row.high, low: row.low, close: row.close })));
    const values = (key) => payload.keltner.filter(row => row[key] !== null).map(row => ({ time: row.date, value: row[key] }));
    upper.setData(values("upper")); basis.setData(values("basis")); lower.setData(values("lower"));
    const simpleMa = (length) => payload.bars.map((row, index, rows) => {
      if (index + 1 < length) return null;
      const window = rows.slice(index + 1 - length, index + 1);
      return { time: row.trade_date, value: window.reduce((sum, item) => sum + item.close, 0) / length };
    }).filter(Boolean);
    ma20.setData(root.querySelector('[data-ma="20"]').checked ? simpleMa(20) : []);
    ma50.setData(root.querySelector('[data-ma="50"]').checked ? simpleMa(50) : []);
    volume.setData(payload.bars.map(row => ({ time: row.trade_date, value: row.volume || 0, color: row.close >= row.open ? "rgba(201,78,71,.48)" : "rgba(37,132,96,.48)" })));
    syncVolumePane();
    markersApi.setMarkers((payload.trade_overlay?.executions || []).map(item => ({
      id: item.execution_id, time: item.traded_on,
      position: item.side === "BUY" ? "belowBar" : "aboveBar",
      shape: item.side === "BUY" ? "arrowUp" : "arrowDown",
      color: item.side === "BUY" ? "#c94e47" : "#258460",
      text: `${item.side === "BUY" ? "买" : "卖"} ${item.price} ×${item.quantity}`,
    })));
    drawings = (payload.drawings || []).map(canonicalizeDrawing);
    if (!selectedDrawing()) selectedDrawingId = null;
    updateDrawingActions(); resize(); fitBarsToViewport();
  };
  const request = async () => {
    const url = chartRequest(symbol, date, settings());
    const cached = cachedPayload(url);
    if (cached?.status === "OK") drawPayload(await withPrivateOverlay(cached));
    try {
      const response = await fetch(url);
      const nextPayload = await response.json();
      if (!response.ok || nextPayload.status !== "OK") return;
      storePayload(url, nextPayload);
      drawPayload(await withPrivateOverlay(nextPayload));
    } catch (_) {
      return;
    }
  };
  const prefetchAdjacentCharts = () => {
    root.querySelectorAll("[data-chart-previous], [data-chart-next]").forEach((link) => {
      const match = new URL(link.href, window.location.origin).pathname.match(/\/a\/stocks\/([^/]+)\/chart$/);
      if (!match) return;
      const url = chartRequest(decodeURIComponent(match[1]), date, settings());
      if (cachedPayload(url)?.status === "OK") return;
      fetch(url).then(async (response) => {
        const nextPayload = await response.json();
        if (response.ok && nextPayload.status === "OK") storePayload(url, nextPayload);
      }).catch(() => {});
    });
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
    drawings.push(saved);
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
    return drawings.find((drawing) => { const points = drawing.anchors.map(coord); if (points.some(point => point.x === null || point.y === null)) return false; if (drawing.tool !== "trendline") return Math.abs(y - points[0].y) < 8; const [a, b] = points; const distance = Math.abs((b.y - a.y) * x - (b.x - a.x) * y + b.x * a.y - b.y * a.x) / Math.hypot(b.y - a.y, b.x - a.x); return distance < 8; });
  };
  const selectDrawingAt = (event) => {
    const drawing = nearestDrawing(event);
    selectedDrawingId = drawing ? drawing.drawing_id : null;
    updateDrawingActions(); renderDrawings();
    drawingHint.textContent = drawing ? "已选中；按删除键、退格键或右键删除" : "未选中画线";
    return drawing;
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
  root.querySelectorAll("[data-draw-tool]").forEach(button => button.addEventListener("click", () => setActiveTool(button.dataset.drawTool)));
  const recordButton = root.querySelector("[data-chart-record]");
  recordButton?.addEventListener("click", () => {
    const nextRecordMode = !recordMode; setActiveTool("browse"); recordMode = nextRecordMode;
    recordButton.setAttribute("aria-pressed", String(recordMode));
    drawingHint.textContent = recordMode ? "点击价格图预填成交日与价格；保存前仍可修改" : "拖拽缩放或查看价格";
  });
  root.querySelectorAll("[data-ma]").forEach(input => input.addEventListener("change", () => { if (payload) drawPayload(payload); }));
  volumeInput.addEventListener("change", () => {
    syncVolumePane();
    if (payload) drawPayload(payload);
    else resize();
  });
  chart.subscribeClick((param) => {
    if (!payload || !param.time) return;
    const chartDate = typeof param.time === "string" ? param.time : `${param.time.year}-${String(param.time.month).padStart(2, "0")}-${String(param.time.day).padStart(2, "0")}`;
    const matches = (payload.trade_overlay?.executions || []).filter(item => item.traded_on === chartDate);
    if (matches.length && !recordMode) { window.location.assign(`/a/stocks/${encodeURIComponent(symbol)}?edit_trade=${encodeURIComponent(matches[0].execution_id)}#trade-journal`); return; }
    if (!recordMode || !param.point) return;
    const price = candles.coordinateToPrice(param.point.y);
    if (price) window.location.assign(`/a/stocks/${encodeURIComponent(symbol)}?traded_on=${encodeURIComponent(chartDate)}&trade_price=${encodeURIComponent(price.toFixed(3))}#trade-journal`);
  });
  deleteSelectedButton.addEventListener("click", async () => { const drawing = selectedDrawing(); if (!drawing) return; try { await removeDrawing(drawing); drawingHint.textContent = "已删除选中画线"; } catch (_) { drawingHint.textContent = "删除失败，请重试"; } });
  root.querySelector("[data-clear-drawings]").addEventListener("click", async () => { if (!payload || !drawings.length || !window.confirm("清空当前复权尺度下的全部个人画线？")) return; for (const drawing of [...drawings]) await removeDrawing(drawing); });
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
  root.querySelectorAll("[data-chart-limit]").forEach(button => button.addEventListener("click", () => { limit = button.dataset.chartLimit === "all" ? null : Number(button.dataset.chartLimit); root.querySelectorAll("[data-chart-limit]").forEach(item => item.setAttribute("aria-pressed", String(item === button))); request(); }));
  root.querySelectorAll("[data-chart-interval]").forEach(button => button.addEventListener("click", () => { interval = button.dataset.chartInterval; root.querySelectorAll("[data-chart-interval]").forEach(item => item.setAttribute("aria-pressed", String(item === button))); request(); }));
  root.querySelectorAll("[data-kc-ma], [data-kc-style], [data-kc-length], [data-kc-atr-length], [data-kc-multiplier], [data-kc-source]").forEach(input => input.addEventListener("change", () => { updateKcSummary(); request(); }));
  chart.timeScale().subscribeVisibleTimeRangeChange(renderDrawings); window.addEventListener("resize", resize); setActiveTool("browse"); syncVolumePane(); resize(); request();
  if ("requestIdleCallback" in window) window.requestIdleCallback(prefetchAdjacentCharts, { timeout: 1200 });
  else window.setTimeout(prefetchAdjacentCharts, 250);
})();
