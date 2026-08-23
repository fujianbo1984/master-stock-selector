(() => {
  const root = document.querySelector("[data-style-relative-lab]");
  const chartNode = root?.querySelector("[data-style-relative-chart]");
  if (!root || !chartNode || !window.LightweightCharts) return;

  const groups = JSON.parse(
    document.getElementById("style-relative-data")?.textContent || "[]",
  ).filter(item => Array.isArray(item.series) && item.series.length);
  if (!groups.length) return;

  const chart = LightweightCharts.createChart(chartNode, {
    width: Math.max(320, Math.floor(chartNode.getBoundingClientRect().width)),
    height: Math.max(260, Math.floor(chartNode.getBoundingClientRect().height)),
    layout: { background: { color: "#fffdfa" }, textColor: "#526273" },
    grid: { vertLines: { color: "#eee8de" }, horzLines: { color: "#eee8de" } },
    rightPriceScale: { borderColor: "#d8d0c3", scaleMargins: { top: 0.1, bottom: 0.1 } },
    timeScale: { borderColor: "#d8d0c3", timeVisible: true, rightOffset: 3 },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    localization: { locale: "zh-CN" },
  });

  const plotted = groups.map((item) => {
    const series = chart.addSeries(LightweightCharts.LineSeries, {
      color: item.color,
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: true,
      priceFormat: { type: "price", precision: 1, minMove: 0.1 },
    });
    series.setData(item.series.map(point => ({
      time: point.trade_date,
      value: point.value,
    })));
    return { item, series };
  });

  plotted[0].series.createPriceLine({
    price: 1000,
    color: "#aaa397",
    lineWidth: 1,
    lineStyle: LightweightCharts.LineStyle.Dashed,
    axisLabelVisible: true,
    title: "起点",
  });

  const readout = root.querySelector("[data-style-relative-readout]");
  chart.subscribeCrosshairMove((param) => {
    if (!readout) return;
    if (!param.time) {
      readout.textContent = "窗口起点1000；曲线上升表示分子指数相对占优";
      return;
    }
    const values = plotted.map(({ item, series }) => {
      const point = param.seriesData.get(series);
      const value = point && typeof point.value === "number"
        ? point.value.toFixed(1)
        : "—";
      return `${item.left_name}/${item.right_name} ${value}`;
    });
    readout.textContent = `${param.time} · ${values.join(" · ")}`;
  });

  chart.timeScale().fitContent();
  new ResizeObserver(() => chart.applyOptions({
    width: Math.max(320, Math.floor(chartNode.getBoundingClientRect().width)),
  })).observe(chartNode);
})();
