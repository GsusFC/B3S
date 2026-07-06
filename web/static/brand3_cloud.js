(() => {
  const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
  const aspects = [4 / 3, 3 / 4, 1, 16 / 10, 3 / 2, 2 / 3];

  const seeded = (seed) => {
    let state = seed >>> 0;
    return () => {
      state = (state + 0x6d2b79f5) | 0;
      let value = Math.imul(state ^ (state >>> 15), 1 | state);
      value = (value + Math.imul(value ^ (value >>> 7), 61 | value)) ^ value;
      return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
    };
  };

  function mount(stage, options = {}) {
    const layer = stage.querySelector("[data-cloud-layer]") || stage;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const interactive = options.interactive !== false;
    const autoRotate = options.autoRotate !== false;
    const focal = 2.6;

    let cards = [];
    let rotationY = 0.4;
    let rotationX = -0.12;
    let velocityY = 0;
    let zoom = options.zoom || 1;
    let dragging = false;
    let lastX = 0;
    let lastY = 0;
    let raf = 0;
    let visible = true;

    const unit = () => Math.min(stage.clientWidth, stage.clientHeight) || 360;

    const sizeCard = (card) => {
      const width = unit() * 0.26 * card.baseScale * Math.sqrt(card.aspect);
      card.width = width;
      card.height = width / card.aspect;
      card.el.style.width = `${width.toFixed(1)}px`;
      card.el.style.height = `${card.height.toFixed(1)}px`;
    };

    const makeCard = (random) => {
      const figure = document.createElement("figure");
      figure.className = "cloud-card";
      const image = document.createElement("span");
      image.className = "cloud-card-img";
      figure.appendChild(image);
      layer.appendChild(figure);
      const card = {
        el: figure,
        image,
        aspect: aspects[(random() * aspects.length) | 0],
        baseScale: 0.78 + random() * 0.55,
        alive: false,
        px: 0,
        py: 0,
        pz: 0,
        tx: 0,
        ty: 0,
        tz: 0,
        width: 0,
        height: 0,
      };
      if (interactive) {
        figure.addEventListener("pointerenter", () => {
          card.alive = true;
        });
        figure.addEventListener("pointerleave", () => {
          card.alive = false;
        });
      }
      return card;
    };

    const ensureCards = (count, random) => {
      while (cards.length < count) cards.push(makeCard(random));
      while (cards.length > count) {
        const card = cards.pop();
        if (card) card.el.remove();
      }
    };

    const layout = (seed, flyIn = false) => {
      const random = seeded(seed || 1);
      const golden = Math.PI * (1 + Math.sqrt(5));
      const total = Math.max(1, cards.length);
      cards.forEach((card, index) => {
        const t = (index + 0.5) / total;
        const phi = Math.acos(1 - 2 * t);
        const theta = golden * index;
        let x = Math.sin(phi) * Math.cos(theta);
        let y = Math.sin(phi) * Math.sin(theta);
        let z = Math.cos(phi);
        x += (random() - 0.5) * 0.4;
        y += (random() - 0.5) * 0.4;
        z += (random() - 0.5) * 0.4;
        card.tx = x * 1.18;
        card.ty = y * 0.82;
        card.tz = z * 0.92;
        if (flyIn && !reduced) {
          card.px = card.tx * 2.2;
          card.py = card.ty * 2.2;
          card.pz = card.tz - 1.7;
        } else {
          card.px = card.tx;
          card.py = card.ty;
          card.pz = card.tz;
        }
        sizeCard(card);
      });
    };

    const assignImage = (card, item) => {
      const url = typeof item === "string" ? item : item.url;
      if (!url) return;
      card.el.dataset.role = item.role || "content";
      card.el.title = item.alt || url;
      const probe = new Image();
      probe.referrerPolicy = "no-referrer";
      probe.onload = () => {
        if (probe.naturalWidth && probe.naturalHeight) {
          card.aspect = probe.naturalWidth / probe.naturalHeight;
          sizeCard(card);
        }
        card.image.style.setProperty("--cell-img", `url("${url}")`);
        card.el.classList.add("is-filled");
      };
      probe.src = url;
    };

    const render = () => {
      const cx = stage.clientWidth / 2;
      const cy = stage.clientHeight / 2;
      const spreadX = stage.clientWidth * 0.4;
      const spreadY = stage.clientHeight * 0.4;
      if (autoRotate && !reduced && !dragging) rotationY += 0.0017;
      rotationY += velocityY;
      if (!dragging) velocityY *= 0.93;
      if (Math.abs(velocityY) < 0.0002) velocityY = 0;
      rotationX += (-0.12 - rotationX) * 0.04;

      const cyaw = Math.cos(rotationY);
      const syaw = Math.sin(rotationY);
      const cpitch = Math.cos(rotationX);
      const spitch = Math.sin(rotationX);

      cards.forEach((card) => {
        card.px += (card.tx - card.px) * 0.07;
        card.py += (card.ty - card.py) * 0.07;
        card.pz += (card.tz - card.pz) * 0.07;

        const x1 = card.px * cyaw + card.pz * syaw;
        const z1 = -card.px * syaw + card.pz * cyaw;
        const y1 = card.py * cpitch - z1 * spitch;
        const z2 = card.py * spitch + z1 * cpitch;
        const perspective = focal / (focal - z2);
        const depth = clamp((z2 + 1.2) / 2.4, 0, 1);
        let scale = perspective * zoom * (0.55 + depth * 0.5);
        if (card.alive) scale *= 1.18;

        const x = cx + x1 * spreadX * perspective * zoom - card.width / 2;
        const y = cy + y1 * spreadY * perspective * zoom - card.height / 2;
        card.el.style.transform = `translate3d(${x.toFixed(1)}px, ${y.toFixed(1)}px, 0) scale(${scale.toFixed(3)})`;
        card.el.style.opacity = (0.45 + depth * 0.55).toFixed(2);
        card.el.style.zIndex = card.alive ? "9999" : String(Math.round(depth * 1000));
        card.el.style.filter = depth < 0.45 ? `blur(${((0.45 - depth) * 2.4).toFixed(1)}px)` : "none";
      });
      raf = visible ? requestAnimationFrame(render) : 0;
    };

    const start = () => {
      if (!raf && visible) raf = requestAnimationFrame(render);
    };
    const stop = () => {
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
    };

    if (interactive) {
      stage.addEventListener("pointerdown", (event) => {
        dragging = true;
        velocityY = 0;
        lastX = event.clientX;
        lastY = event.clientY;
        try {
          stage.setPointerCapture(event.pointerId);
        } catch {}
      });
      stage.addEventListener("pointermove", (event) => {
        if (!dragging) return;
        const dx = event.clientX - lastX;
        const dy = event.clientY - lastY;
        rotationY += dx * 0.006;
        rotationX = clamp(rotationX + dy * 0.004, -0.7, 0.7);
        velocityY = dx * 0.006;
        lastX = event.clientX;
        lastY = event.clientY;
      });
      const endDrag = () => {
        dragging = false;
      };
      stage.addEventListener("pointerup", endDrag);
      stage.addEventListener("pointercancel", endDrag);
      if (options.allowZoom !== false) {
        stage.addEventListener(
          "wheel",
          (event) => {
            event.preventDefault();
            zoom = clamp(zoom * (1 - event.deltaY * 0.0012), 0.55, 2.6);
          },
          { passive: false }
        );
      }
    }

    return {
      setImages(items) {
        const list = Array.isArray(items) ? items : [];
        const random = seeded(list.length || 7);
        ensureCards(list.length, random);
        layout((list.length || 1) * 13 + 1, true);
        list.forEach((item, index) => assignImage(cards[index], item));
        start();
      },
      reshuffle() {
        layout(((Math.random() * 1e9) | 0) + 1, false);
      },
      setVisible(next) {
        visible = Boolean(next);
        if (visible) start();
        else stop();
      },
      relayout() {
        cards.forEach(sizeCard);
      },
    };
  }

  window.Brand3Cloud = { mount };
})();
