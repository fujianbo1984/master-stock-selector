(() => {
  const root = document.querySelector("[data-breadth-lab]");
  if (!root || !window.LightweightCharts) return;

  const history = JSON.parse(document.getElementById("breadth-history-data")?.textContent || "[]");
  const proxy = JSON.parse(document.getElementById("breadth-proxy-data")?.textContent || "[]");
  const chartNode = root.querySelector("[data-overlay-chart]");
  if (!chartNode) return;

  const chart = LightweightCharts.createChart(chartNode, {
    width: Math.max(320, Math.floor(chartNode.getBoundingClientRect().width)),
    height: Math.max(260, Math.floor(chartNode.getBoundingClientRect().height)),
    layout: { background: { color: "#fffdfa" }, textColor: "#526273" },
    grid: { vertLines: { color: "#eee8de" }, horzLines: { color: "#eee8de" } },
    leftPriceScale: { visible: true, borderColor: "#d8d0c3", scaleMargins: { top: 0.1, bottom: 0.1 } },
    rightPriceScale: { visible: true, borderColor: "#d8d0c3", scaleMargins: { top: 0.1, bottom: 0.1 } },
    timeScale: { borderColor: "#d8d0c3", timeVisible: true, rightOffset: 3 },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    localization: { locale: "zh-CN" },
  });

  const rateScale = (original) => {
    const current = original();
    return {
      priceRange: { minValue: 0, maxValue: 100 },
      margins: current?.margins,
    };
  };
  const percentFormat = {
    type: "custom",
    formatter: value => `${value.toFixed(1)}%`,
    minMove: 0.01,
  };
  const weinsteinSeries = chart.addSeries(LightweightCharts.LineSeries, {
    priceScaleId: "left",
    color: "#2457a7",
    lineWidth: 2,
    lineStyle: LightweightCharts.LineStyle.Dotted,
    priceLineVisible: false,
    lastValueVisible: true,
    priceFormat: percentFormat,
    autoscaleInfoProvider: rateScale,
  });
  const minerviniSeries = chart.addSeries(LightweightCharts.LineSeries, {
    priceScaleId: "left",
    color: "#d28b18",
    lineWidth: 2,
    lineStyle: LightweightCharts.LineStyle.Dotted,
    priceLineVisible: false,
    lastValueVisible: true,
    priceFormat: percentFormat,
    autoscaleInfoProvider: rateScale,
  });
  const proxySeries = chart.addSeries(LightweightCharts.LineSeries, {
    priceScaleId: "right",
    color: "#526273",
    lineWidth: 2,
    lineStyle: LightweightCharts.LineStyle.Solid,
    priceLineVisible: false,
    lastValueVisible: true,
    priceFormat: { type: "price", precision: 1, minMove: 0.1 },
  });

  weinsteinSeries.setData(history
    .filter(item => item.weinstein_pass_rate !== null)
    .map(item => ({ time: item.as_of_date, value: item.weinstein_pass_rate })));
  minerviniSeries.setData(history
    .filter(item => item.minervini_pass_rate !== null)
    .map(item => ({ time: item.as_of_date, value: item.minervini_pass_rate })));
  proxySeries.setData(proxy.map(item => ({ time: item.trade_date, value: item.close })));

  const readout = root.querySelector("[data-breadth-readout]");
  const benchmarkName = root.dataset.benchmarkName || "指数";
  const valueAt = (param, series) => {
    const item = param.seriesData.get(series);
    return item && typeof item.value === "number" ? item.value : null;
  };
  chart.subscribeCrosshairMove((param) => {
    const weinstein = valueAt(param, weinsteinSeries);
    const minervini = valueAt(param, minerviniSeries);
    const indexValue = valueAt(param, proxySeries);
    readout.textContent = param.time
      ? `${param.time} · W ${weinstein === null ? "—" : `${weinstein.toFixed(2)}%`} · M ${minervini === null ? "—" : `${minervini.toFixed(2)}%`} · ${benchmarkName} ${indexValue === null ? "—" : indexValue.toFixed(1)}`
      : `左轴：通过率 · 右轴：${proxy.length ? "窗口起点1000" : "指数数据不足"}`;
  });

  chart.timeScale().fitContent();
  const resize = () => chart.applyOptions({
    width: Math.max(320, Math.floor(chartNode.getBoundingClientRect().width)),
  });
  new ResizeObserver(resize).observe(root);
})();
