const SVG = "http://www.w3.org/2000/svg";
const SPOKES = 13;
const RINGS = [0, 0.09, 0.26, 0.55];
const GRAVITY = 900;
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");

const random = (min, max) => min + Math.random() * (max - min);

function arm(enclosure) {
  if (enclosure.dataset.glass) return;
  const form = enclosure.querySelector("form");
  const glass = document.createElement("button");
  glass.type = "button";
  glass.className = "glass";
  glass.title = "Break glass";
  glass.setAttribute("aria-label", "Break the glass to reach the kill switch");
  glass.addEventListener("click", (event) => shatter(enclosure, form, glass, event), { once: true });
  form.inert = true;
  enclosure.append(glass);
  enclosure.dataset.glass = "armed";
}

function shatter(enclosure, form, glass, event) {
  const box = glass.getBoundingClientRect();
  const keyboard = event.detail === 0;
  const impact = keyboard ? [box.width / 2, box.height / 2] : [event.clientX - box.left, event.clientY - box.top];
  glass.remove();
  form.inert = false;
  enclosure.dataset.glass = "broken";
  form.querySelector("button").focus({ preventScroll: true });
  if (!reducedMotion.matches) enclosure.append(animateShards(box.width, box.height, impact));
}

function crackPattern(width, height, [cx, cy]) {
  const reach = Math.hypot(Math.max(cx, width - cx), Math.max(cy, height - cy));
  const radii = [...RINGS.map((ring) => ring * reach * 2), reach * 1.6];
  const offset = random(0, Math.PI * 2);
  const step = (Math.PI * 2) / SPOKES;
  const angles = Array.from({ length: SPOKES }, (_, i) => offset + i * step + random(-0.35, 0.35) * step);
  const points = radii.map((radius, ring) =>
    angles.map((angle) => {
      const r = ring === 0 || ring === radii.length - 1 ? radius : radius * random(0.75, 1.25);
      return [cx + Math.cos(angle) * r, cy + Math.sin(angle) * r];
    }),
  );
  const cells = [];
  for (let ring = 0; ring < radii.length - 1; ring++) {
    for (let spoke = 0; spoke < SPOKES; spoke++) {
      const next = (spoke + 1) % SPOKES;
      const polygon = clipToRect(
        [points[ring][spoke], points[ring][next], points[ring + 1][next], points[ring + 1][spoke]],
        width,
        height,
      );
      if (polygon.length >= 3) cells.push({ ring, polygon });
    }
  }
  return cells;
}

function clipToRect(polygon, width, height) {
  const planes = [[0, 0, 1], [0, width, -1], [1, 0, 1], [1, height, -1]];
  return planes.reduce((points, [axis, bound, sign]) => {
    const inside = (point) => sign * (point[axis] - bound) >= 0;
    return points.flatMap((current, i) => {
      const previous = points.at(i - 1);
      const out = [];
      if (inside(current) !== inside(previous)) {
        const t = (bound - previous[axis]) / (current[axis] - previous[axis]);
        out.push([previous[0] + t * (current[0] - previous[0]), previous[1] + t * (current[1] - previous[1])]);
      }
      if (inside(current)) out.push(current);
      return out;
    });
  }, polygon);
}

function area(polygon) {
  return Math.abs(polygon.reduce((sum, [x, y], i) => {
    const [nx, ny] = polygon[(i + 1) % polygon.length];
    return sum + x * ny - nx * y;
  }, 0)) / 2;
}

function centroid(polygon) {
  const [sx, sy] = polygon.reduce(([ax, ay], [x, y]) => [ax + x, ay + y], [0, 0]);
  return [sx / polygon.length, sy / polygon.length];
}

function svgElement(name, attributes) {
  const element = document.createElementNS(SVG, name);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
  return element;
}

function shard(polygon) {
  return svgElement("polygon", {
    points: polygon.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join(" "),
    "fill-opacity": random(0.06, 0.24).toFixed(2),
  });
}

function fall(element, { drift, lift, spin, delay, duration }) {
  const steps = 10;
  const frames = Array.from({ length: steps + 1 }, (_, i) => {
    const t = (i / steps) * (duration / 1000);
    const x = drift[0] + drift[2] * t;
    const y = drift[1] + lift * t + 0.5 * GRAVITY * t * t;
    return {
      transform: `translate(${x}px, ${y}px) rotate(${spin * (i / steps)}deg) scale(${1 + 0.12 * (i / steps)})`,
      opacity: i / steps < 0.55 ? 1 : 1 - (i / steps - 0.55) / 0.45,
    };
  });
  return element.animate(frames, { duration, delay, fill: "both" }).finished;
}

function animateShards(width, height, impact) {
  const svg = svgElement("svg", { class: "shards", width, height, viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" });
  const falls = [];
  for (const { ring, polygon } of crackPattern(width, height, impact)) {
    const piece = shard(polygon);
    svg.append(piece);
    const [x, y] = centroid(polygon);
    const distance = Math.hypot(x - impact[0], y - impact[1]) || 1;
    const [ux, uy] = [(x - impact[0]) / distance, (y - impact[1]) / distance];
    const push = (RINGS.length - ring) * random(14, 30);
    falls.push(fall(piece, {
      drift: [ux * 1.5, uy * 1.5, ux * push],
      lift: uy * push - random(10, 60),
      spin: random(-260, 260) / Math.sqrt(1 + area(polygon) / 60),
      delay: 110 + ring * 40 + random(0, 70),
      duration: random(650, 950),
    }));
  }
  for (let i = 0; i < 16; i++) {
    const size = random(1, 2.6);
    const [x, y] = impact;
    const dust = svgElement("polygon", {
      class: "dust",
      points: `${x},${y - size} ${x + size},${y + size} ${x - size},${y + size * 0.6}`,
    });
    svg.append(dust);
    const angle = random(0, Math.PI * 2);
    const speed = random(60, 220);
    falls.push(fall(dust, {
      drift: [0, 0, Math.cos(angle) * speed],
      lift: Math.sin(angle) * speed - 80,
      spin: random(-720, 720),
      delay: random(0, 40),
      duration: random(380, 620),
    }));
  }
  const flash = svgElement("circle", { class: "impact", cx: impact[0], cy: impact[1], r: 9 });
  svg.append(flash);
  falls.push(flash.animate(
    [{ opacity: 0.9, transform: "scale(0.4)" }, { opacity: 0, transform: "scale(2.6)" }],
    { duration: 220, easing: "ease-out", fill: "both" },
  ).finished);
  Promise.all(falls).then(() => svg.remove());
  return svg;
}

htmx.onLoad((root) => {
  for (const enclosure of root.querySelectorAll("[data-break-glass]")) arm(enclosure);
});
