(() => {
  const page = document.querySelector("[data-realtime-page]");
  const frame = document.querySelector("[data-realtime-frame]");
  const loading = document.querySelector("[data-realtime-loading]");
  if (frame && loading) frame.addEventListener("load", () => loading.setAttribute("hidden", ""));

  window.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key) || event.altKey || event.ctrlKey || event.metaKey || event.isComposing) return;
    const active = document.activeElement;
    if (active?.isContentEditable || ["INPUT", "SELECT", "TEXTAREA"].includes(active?.tagName)) return;
    const target = document.querySelector(event.key === "ArrowLeft" ? "[data-realtime-previous]" : "[data-realtime-next]");
    if (!target) return;
    event.preventDefault();
    window.location.assign(target.href);
  });

  page?.focus({ preventScroll: true });
})();
