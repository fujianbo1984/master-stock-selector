(() => {
  "use strict";

  const root = document.querySelector("[data-industry-chart]");
  const canvas = root?.querySelector("[data-industry-chart-canvas]");
  const payload = document.getElementById("industry-chart-data");
  const charts = window.LightweightCharts;
  if (!root || !canvas || !payload || !charts) return;

  let bars;
  try {
    bars = JSON.parse(payload.textContent || "[]");
  } catch (_error) {
    return;
  }
  if (!Array.isArray(bars) || bars.length === 0) return;

  const chart = charts.createChart(canvas, {
    width: canvas.clientWidth,
    height: canvas.clientHeight,
    layout: { background: { type: "solid", color: "#fffdfa" }, textColor: "#6f685f" },
    grid: { vertLines: { color: "#eee8de" }, horzLines: { color: "#e8e1d6" } },
    rightPriceScale: { borderColor: "#d8d0c4", scaleMargins: { top: 0.08, bottom: 0.08 } },
    timeScale: { borderColor: "#d8d0c4", timeVisible: false, rightOffset: 2 },
    crosshair: { mode: charts.CrosshairMode.Normal },
    localization: { locale: "zh-CN", priceFormatter: (price) => Number(price).toFixed(2) },
  });

  const candleSeries = chart.addSeries(charts.CandlestickSeries, {
    upColor: "#c94e47",
    downColor: "#258460",
    borderUpColor: "#c94e47",
    borderDownColor: "#258460",
    wickUpColor: "#c94e47",
    wickDownColor: "#258460",
  });
  const normalized = bars.map((bar) => ({
    time: String(bar.trade_date),
    open: Number(bar.open),
    high: Number(bar.high),
    low: Number(bar.low),
    close: Number(bar.close),
  }));
  candleSeries.setData(normalized);

  const movingAverage = (windowSize) => normalized.slice(windowSize - 1).map((bar, offset) => {
    const end = offset + windowSize;
    const values = normalized.slice(offset, end);
    return { time: bar.time, value: values.reduce((sum, item) => sum + item.close, 0) / windowSize };
  });
  const ma20 = chart.addSeries(charts.LineSeries, { color: "#d28b18", lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
  const ma50 = chart.addSeries(charts.LineSeries, { color: "#5b6fc7", lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
  ma20.setData(movingAverage(20));
  ma50.setData(movingAverage(50));
  chart.timeScale().fitContent();

  const barByDate = new Map(bars.map((bar) => [String(bar.trade_date), bar]));
  const readout = root.querySelector("[data-industry-chart-readout]");
  const showBar = (bar) => {
    if (!readout || !bar) return;
    readout.textContent = `${bar.trade_date} · 开 ${bar.open} · 高 ${bar.high} · 低 ${bar.low} · 收 ${bar.close} · 有效成员 ${bar.member_count}`;
  };
  chart.subscribeCrosshairMove((event) => {
    if (!event.time) {
      showBar(bars[bars.length - 1]);
      return;
    }
    showBar(barByDate.get(String(event.time)));
  });

  const resize = () => chart.applyOptions({ width: canvas.clientWidth, height: canvas.clientHeight });
  if (window.ResizeObserver) new ResizeObserver(resize).observe(canvas);
  else window.addEventListener("resize", resize);

  document.addEventListener("keydown", (event) => {
    const target = event.target;
    if (target instanceof HTMLElement && (target.matches("input, select, textarea, button") || target.isContentEditable)) return;
    const link = event.key === "ArrowLeft"
      ? document.querySelector("[data-industry-previous]")
      : event.key === "ArrowRight"
        ? document.querySelector("[data-industry-next]")
        : null;
    if (link instanceof HTMLAnchorElement) {
      event.preventDefault();
      window.location.assign(link.href);
    }
  });
})();
