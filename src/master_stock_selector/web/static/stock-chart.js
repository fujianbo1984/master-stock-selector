(() => {
  const root = document.querySelector("[data-stock-chart]");
  if (!root || !window.LightweightCharts) return;
  const chartNode = root.querySelector("[data-chart-canvas]");
  const overlay = root.querySelector("[data-drawing-overlay]");
  const context = overlay.getContext("2d");
  const status = root.querySelector("[data-chart-status]");
  const kcSummary = root.querySelector("[data-kc-summary]");
  const drawingActions = root.querySelector("[data-drawing-actions]");
  const drawingHint = root.querySelector("[data-drawing-hint]");
  const deleteSelectedButton = root.querySelector("[data-delete-selected]");
  const symbol = root.dataset.symbol;
  const date = root.dataset.date;
  let limit = 260;
  let payload = null;
  let drawings = [];
  let activeTool = "browse";
  let pendingAnchor = null;
  let previewAnchor = null;
  let selectedDrawingId = null;
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

  const settings = () => ({
    length: root.querySelector("[data-kc-length]").value,
    multiplier: root.querySelector("[data-kc-multiplier]").value,
    source: root.querySelector("[data-kc-source]").value,
    use_ema: root.querySelector("[data-kc-ma]").value === "ema",
    band_style: root.querySelector("[data-kc-style]").value,
    atr_length: root.querySelector("[data-kc-atr-length]").value,
    limit,
  });
  const updateKcSummary = () => {
    const source = root.querySelector("[data-kc-source]").selectedOptions[0].text;
    const basis = root.querySelector("[data-kc-ma]").value.toUpperCase();
    const style = root.querySelector("[data-kc-style]").value.toUpperCase();
    kcSummary.textContent = `KC：${basis}${root.querySelector("[data-kc-length]").value} · ${style}${root.querySelector("[data-kc-atr-length]").value} · ×${root.querySelector("[data-kc-multiplier]").value} · ${source}`;
  };
  const selectedDrawing = () => drawings.find(drawing => drawing.drawing_id === selectedDrawingId) || null;
  const updateDrawingActions = () => {
    drawingActions.hidden = drawings.length === 0;
    deleteSelectedButton.disabled = !selectedDrawing();
  };
  const setActiveTool = (tool) => {
    activeTool = tool;
    pendingAnchor = null;
    previewAnchor = null;
    overlay.style.pointerEvents = activeTool === "browse" ? "none" : "auto";
    overlay.style.cursor = activeTool === "browse" ? "default" : "crosshair";
    root.querySelectorAll("[data-draw-tool]").forEach(item => item.setAttribute("aria-pressed", String(item.dataset.drawTool === activeTool)));
    drawingHint.textContent = ({ browse: "拖拽缩放或查看价格", select: "点击画线选中；Delete / 退格或右键删除", trendline: "点击起点，再点击终点", horizontal: "点击图表设置水平线" })[activeTool] || "选择工具后在图上点击";
    renderDrawings();
  };
  const resize = () => {
    const rect = chartNode.getBoundingClientRect();
    chart.applyOptions({ width: Math.max(320, Math.floor(rect.width)), height: 520 });
    const ratio = window.devicePixelRatio || 1;
    overlay.width = Math.floor(rect.width * ratio); overlay.height = Math.floor(520 * ratio);
    overlay.style.width = `${rect.width}px`; overlay.style.height = "520px";
    context.setTransform(ratio, 0, 0, ratio, 0, 0); renderDrawings();
  };
  const coord = (anchor) => ({
    x: typeof anchor.logical === "number"
      ? chart.timeScale().logicalToCoordinate(anchor.logical)
      : chart.timeScale().timeToCoordinate(anchor.date),
    y: candles.priceToCoordinate(anchor.price),
  });
  const drawOne = (drawing, highlight = false) => {
    const anchors = drawing.anchors.map(coord);
    if (anchors.some(point => point.x === null || point.y === null)) return;
    const stroke = () => {
      context.beginPath();
      if (drawing.tool === "horizontal") { context.moveTo(0, anchors[0].y); context.lineTo(overlay.clientWidth, anchors[0].y); }
      else { context.moveTo(anchors[0].x, anchors[0].y); context.lineTo(anchors[1].x, anchors[1].y); }
      context.stroke();
    };
    context.save();
    if (highlight) {
      context.strokeStyle = "#fffdfa"; context.lineWidth = 7; context.setLineDash([]); stroke();
      context.strokeStyle = "#c83b2b"; context.lineWidth = 3; context.setLineDash(drawing.tool === "horizontal" ? [7, 4] : []); stroke();
      anchors.forEach(point => { context.beginPath(); context.fillStyle = "#fffdfa"; context.strokeStyle = "#c83b2b"; context.lineWidth = 2; context.arc(point.x, point.y, 5, 0, Math.PI * 2); context.fill(); context.stroke(); });
    } else {
      context.strokeStyle = "#8d6c31"; context.lineWidth = 1.5; context.setLineDash(drawing.tool === "horizontal" ? [5, 4] : []); stroke();
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
  const drawPayload = (nextPayload) => {
    payload = nextPayload;
    candles.setData(payload.bars.map(row => ({ time: row.trade_date, open: row.open, high: row.high, low: row.low, close: row.close })));
    const values = (key) => payload.keltner.filter(row => row[key] !== null).map(row => ({ time: row.date, value: row[key] }));
    upper.setData(values("upper")); basis.setData(values("basis")); lower.setData(values("lower"));
    drawings = payload.drawings;
    if (!selectedDrawing()) selectedDrawingId = null;
    updateDrawingActions(); chart.timeScale().fitContent();
    const visibleRange = chart.timeScale().getVisibleLogicalRange();
    if (visibleRange) {
      const rightPadding = Math.max(8, Math.min(20, Math.ceil(payload.bars.length * 0.08)));
      chart.timeScale().setVisibleLogicalRange({ from: visibleRange.from, to: visibleRange.to + rightPadding });
    }
    resize();
    status.textContent = `前复权 · ${payload.bars[0].trade_date} 至 ${payload.bars.at(-1).trade_date} · ${payload.bars.length} 日`;
  };
  const request = async () => {
    const url = chartRequest(symbol, date, settings());
    const cached = cachedPayload(url);
    if (cached?.status === "OK") drawPayload(cached);
    else status.textContent = "正在加载本地日线…";
    try {
      const response = await fetch(url);
      const nextPayload = await response.json();
      if (!response.ok || nextPayload.status !== "OK") { status.textContent = `无法绘图：${nextPayload.reason || "数据不足"}`; return; }
      storePayload(url, nextPayload);
      drawPayload(nextPayload);
    } catch (_) {
      if (!cached?.status) status.textContent = "本地日线加载失败，请重试";
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
    return typeof logical === "number" ? { logical: Number(logical.toFixed(4)), price: Number(price.toFixed(6)) } : null;
  };
  const saveDrawing = async (drawing) => {
    const response = await fetch(`/api/a/stocks/${encodeURIComponent(symbol)}/chart/drawings`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...drawing, price_scale_id: payload.price_scale_id }) });
    if (!response.ok) throw new Error("保存画线失败");
    const saved = await response.json();
    drawings.push(saved);
    selectedDrawingId = saved.drawing_id;
    updateDrawingActions(); renderDrawings();
  };
  const removeDrawing = async (drawing) => {
    const response = await fetch(`/api/a/stocks/${encodeURIComponent(symbol)}/chart/drawings/${encodeURIComponent(drawing.drawing_id)}?price_scale_id=${encodeURIComponent(payload.price_scale_id)}`, { method: "DELETE" });
    if (!response.ok) throw new Error("删除画线失败");
    drawings = drawings.filter(item => item.drawing_id !== drawing.drawing_id);
    if (selectedDrawingId === drawing.drawing_id) selectedDrawingId = null;
    updateDrawingActions(); renderDrawings();
  };
  const nearestDrawing = (event) => {
    const rect = overlay.getBoundingClientRect(); const x = event.clientX - rect.left; const y = event.clientY - rect.top;
    return drawings.find((drawing) => { const points = drawing.anchors.map(coord); if (points.some(point => point.x === null || point.y === null)) return false; if (drawing.tool === "horizontal") return Math.abs(y - points[0].y) < 8; const [a, b] = points; const distance = Math.abs((b.y - a.y) * x - (b.x - a.x) * y + b.x * a.y - b.y * a.x) / Math.hypot(b.y - a.y, b.x - a.x); return distance < 8; });
  };
  const selectDrawingAt = (event) => {
    const drawing = nearestDrawing(event);
    selectedDrawingId = drawing ? drawing.drawing_id : null;
    updateDrawingActions(); renderDrawings();
    drawingHint.textContent = drawing ? "已选中；按 Delete / 退格或右键删除" : "未选中画线";
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
      if (activeTool === "horizontal") { await saveDrawing({ tool: "horizontal", anchors: [point] }); setActiveTool("select"); drawingHint.textContent = "水平线已保存，已切换至选择"; return; }
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
  deleteSelectedButton.addEventListener("click", async () => { const drawing = selectedDrawing(); if (!drawing) return; try { await removeDrawing(drawing); drawingHint.textContent = "已删除选中画线"; } catch (_) { drawingHint.textContent = "删除失败，请重试"; } });
  root.querySelector("[data-clear-drawings]").addEventListener("click", async () => { if (!payload || !drawings.length || !window.confirm("清空当前复权尺度下的全部个人画线？")) return; for (const drawing of [...drawings]) await removeDrawing(drawing); });
  const chartReviewForm = document.querySelector(".chart-review-control");
  chartReviewForm?.querySelector("select[name='manual_state']")?.addEventListener("change", () => chartReviewForm.requestSubmit());
  document.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key) || event.altKey || event.ctrlKey || event.metaKey || event.isComposing) return;
    const active = document.activeElement;
    if (active?.isContentEditable || ["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(active?.tagName)) return;
    const target = document.querySelector(event.key === "ArrowLeft" ? "[data-chart-previous]" : "[data-chart-next]");
    if (!target) return;
    event.preventDefault();
    window.location.assign(target.href);
  });
  root.querySelectorAll("[data-chart-limit]").forEach(button => button.addEventListener("click", () => { limit = Number(button.dataset.chartLimit); root.querySelectorAll("[data-chart-limit]").forEach(item => item.setAttribute("aria-pressed", String(item === button))); request(); }));
  root.querySelectorAll("[data-kc-ma], [data-kc-style], [data-kc-length], [data-kc-atr-length], [data-kc-multiplier], [data-kc-source]").forEach(input => input.addEventListener("change", () => { updateKcSummary(); request(); }));
  chart.timeScale().subscribeVisibleTimeRangeChange(renderDrawings); window.addEventListener("resize", resize); setActiveTool("browse"); resize(); request();
  if ("requestIdleCallback" in window) window.requestIdleCallback(prefetchAdjacentCharts, { timeout: 1200 });
  else window.setTimeout(prefetchAdjacentCharts, 250);
})();
