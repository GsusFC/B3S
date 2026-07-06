(() => {
  const stage = document.querySelector("[data-moodboard-stage]");
  const dataNode = document.querySelector("[data-moodboard-items]");
  if (!stage || !dataNode || !window.Brand3Cloud) return;

  let items = [];
  try {
    items = JSON.parse(dataNode.textContent || "[]");
  } catch {
    return;
  }
  if (!Array.isArray(items) || items.length === 0) return;

  const cloud = window.Brand3Cloud.mount(stage, {
    autoRotate: true,
    interactive: true,
    allowZoom: true,
  });
  cloud.setImages(items);

  const shuffle = document.querySelector("[data-moodboard-shuffle]");
  if (shuffle) shuffle.addEventListener("click", () => cloud.reshuffle());

  const grain = stage.querySelector("[data-moodboard-grain]");
  if (!(grain instanceof HTMLCanvasElement)) return;
  const ctx = grain.getContext("2d");
  if (!ctx) return;

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const tile = document.createElement("canvas");
  const tileCtx = tile.getContext("2d");
  if (!tileCtx) return;
  let noise = null;
  let raf = 0;
  let last = 0;

  const size = () => {
    const rect = stage.getBoundingClientRect();
    grain.width = Math.max(180, Math.round(rect.width / 2));
    grain.height = Math.max(140, Math.round(rect.height / 2));
    noise = ctx.createImageData(96, 72);
    tile.width = noise.width;
    tile.height = noise.height;
  };

  const draw = () => {
    if (!noise) return;
    const data = noise.data;
    for (let i = 0; i < data.length; i += 4) {
      const value = (Math.random() * 255) | 0;
      data[i] = value;
      data[i + 1] = value;
      data[i + 2] = value;
      data[i + 3] = (Math.random() * 34) | 0;
    }
    tileCtx.putImageData(noise, 0, 0);
    ctx.clearRect(0, 0, grain.width, grain.height);
    ctx.fillStyle = ctx.createPattern(tile, "repeat");
    ctx.fillRect(0, 0, grain.width, grain.height);
  };

  const loop = (time) => {
    if (document.hidden) {
      raf = 0;
      return;
    }
    if (time - last > 90) {
      draw();
      last = time;
    }
    raf = requestAnimationFrame(loop);
  };

  const start = () => {
    if (reducedMotion.matches) {
      draw();
      return;
    }
    if (!raf) raf = requestAnimationFrame(loop);
  };

  size();
  start();
  window.addEventListener("resize", () => {
    window.setTimeout(() => {
      size();
      cloud.relayout();
    }, 120);
  });
  document.addEventListener("visibilitychange", () => {
    cloud.setVisible(!document.hidden);
    if (!document.hidden) start();
  });
})();
