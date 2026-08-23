(() => {
  const root = document.querySelector("[data-breadth-lab]");
  if (!root || !window.LightweightCharts) return;

  const weinsteinHistory = JSON.parse(document.getElementById("weinstein-breadth-data")?.textContent || "[]");
  const minerviniHistory = JSON.parse(document.getElementById("minervini-breadth-data")?.textContent || "[]");
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
    pointMarkersVisible: true,
    pointMarkersRadius: 3,
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

  weinsteinSeries.setData(weinsteinHistory
    .filter(item => item.pass_rate !== null)
    .map(item => ({ time: item.as_of_date, value: item.pass_rate })));
  minerviniSeries.setData(minerviniHistory
    .filter(item => item.pass_rate !== null)
    .map(item => ({ time: item.as_of_date, value: item.pass_rate })));
  proxySeries.setData(proxy.map(item => ({ time: item.trade_date, value: item.close })));

  const readout = root.querySelector("[data-breadth-readout]");
  const benchmarkName = root.dataset.benchmarkName || "指数";
  const valueAt = (param, series) => {
    const item = param.seriesData.get(series);
    return item && typeof item.value === "number" ? item.value : null;
  };
  const dateKey = (value) => {
    if (typeof value === "string") return value;
    if (value && typeof value.year === "number") {
      return `${value.year}-${String(value.month).padStart(2, "0")}-${String(value.day).padStart(2, "0")}`;
    }
    return "";
  };
  const latestWeinsteinAt = (tradeDate) => {
    let latest = null;
    for (const item of weinsteinHistory) {
      if (item.pass_rate !== null && item.as_of_date <= tradeDate) latest = item;
      if (item.as_of_date > tradeDate) break;
    }
    return latest;
  };
  chart.subscribeCrosshairMove((param) => {
    const tradeDate = dateKey(param.time);
    const weinstein = latestWeinsteinAt(tradeDate);
    const minervini = valueAt(param, minerviniSeries);
    const indexValue = valueAt(param, proxySeries);
    readout.textContent = param.time
      ? `${tradeDate} · W ${weinstein === null ? "—" : `${weinstein.pass_rate.toFixed(2)}% (完整周截至${weinstein.as_of_date})`} · M ${minervini === null ? "—" : `${minervini.toFixed(2)}%`} · ${benchmarkName} ${indexValue === null ? "—" : indexValue.toFixed(1)}`
      : `左轴：W完整周 / M日线通过率 · 右轴：${proxy.length ? "窗口起点1000" : "指数数据不足"}`;
  });

  chart.timeScale().fitContent();
  const resize = () => chart.applyOptions({
    width: Math.max(320, Math.floor(chartNode.getBoundingClientRect().width)),
  });
  new ResizeObserver(resize).observe(root);
})();
