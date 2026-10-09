// Board layout: each card is { type, at: [col, row, width, height], ...props }
// on a 12 x 8 grid. Renderers below turn a card definition into DOM.
const CARDS = [
  { type: "steps", at: [1, 1, 2, 2], goal: 10000, refreshMs: 30_000 },
  { type: "btc", at: [3, 1, 3, 2] },
  { type: "coin", at: [6, 1, 1, 1], symbol: "eth", label: "ETH" },
  { type: "coin", at: [6, 2, 1, 1], symbol: "ada", label: "ADA" },
  { type: "weather", at: [7, 1, 1, 1], city: "tehran", label: "Tehran" },
  { type: "weather", at: [8, 1, 1, 1], city: "toronto", label: "Toronto" },
  { type: "weather", at: [9, 1, 1, 1], city: "madrid", label: "Madrid" },
  { type: "tv", at: [7, 2, 1, 1] },
  { type: "preview", at: [1, 3, 6, 4] },
];

// Where the page runs:
//   tv   the Monet TV app's home screen (Chromecast; window.MonetTV bridge, ?tv=1)
//   pi   the Pi's own kiosk (loopback)
//   web  a laptop / phone on the home network
const PLATFORM = window.MonetTV || new URLSearchParams(location.search).has("tv") ? "tv"
  : ["127.0.0.1", "localhost", "::1"].includes(location.hostname) ? "pi" : "web";
document.body.classList.add(`on-${PLATFORM}`);

const $ = (root, sel) => root.querySelector(sel);
const pad = (n) => String(n).padStart(2, "0");
const fmtInt = (n) => Math.round(n).toLocaleString("en-US");
const fmtCad = (n) => "$" + fmtInt(n);
// Whole dollars above $1,000, cents above $1, four decimals below.
const fmtPrice = (n) => "$" + n.toLocaleString("en-US", {
  minimumFractionDigits: n >= 1000 ? 0 : n >= 1 ? 2 : 4,
  maximumFractionDigits: n >= 1000 ? 0 : n >= 1 ? 2 : 4,
});
const fmtK = (n) => (n / 1000).toFixed(1) + "k";
const fmtPct = (p) => (p >= 0 ? "▲ " : "▼ ") + Math.abs(p).toFixed(2) + "%";
const UP = "#2ee6d6", DOWN = "#ff5c93";

// One poll per URL, shared by every card that subscribes to it.
const feeds = {};
function feed(url, ms, cb) {
  if (!feeds[url]) {
    const f = (feeds[url] = { subs: [] });
    const run = async () => {
      try { const d = await (await fetch(url)).json(); f.subs.forEach((s) => s(d)); } catch { /* keep last */ }
    };
    setTimeout(run, 0); setInterval(run, ms);
  }
  feeds[url].subs.push(cb);
}

// Line + area paths for a [[t, v], ...] series in a w x h box.
function sparkPaths(series, w, h, padY = 4) {
  const vs = series.map((p) => p[1]);
  const lo = Math.min(...vs), hi = Math.max(...vs), span = hi - lo || 1;
  const x = (i) => (i / (series.length - 1)) * w;
  const y = (v) => padY + (1 - (v - lo) / span) * (h - 2 * padY);
  const line = vs.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  return { line, area: `${line}L${w},${h}L0,${h}Z`, y, lastY: y(vs.at(-1)) };
}

const COIN_ICONS = {
  eth: `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="12" fill="#627eea"/>
    <path d="M12 3.5 6.8 12.2 12 15.3l5.2-3.1z" fill="#fff" opacity=".95"/>
    <path d="M12 16.3 6.8 13.2 12 20.5l5.2-7.3z" fill="#fff" opacity=".7"/></svg>`,
  ada: `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="12" fill="#0033ad"/>
    ${[0, 60, 120, 180, 240, 300].map((a) => `<circle cx="${12 + 6.2 * Math.cos((a * Math.PI) / 180)}" cy="${12 + 6.2 * Math.sin((a * Math.PI) / 180)}" r="1.5" fill="#fff"/>`).join("")}
    ${[30, 90, 150, 210, 270, 330].map((a) => `<circle cx="${12 + 3.4 * Math.cos((a * Math.PI) / 180)}" cy="${12 + 3.4 * Math.sin((a * Math.PI) / 180)}" r="1" fill="#fff" opacity=".85"/>`).join("")}
    <circle cx="12" cy="12" r="1.9" fill="none" stroke="#fff" stroke-width="1"/></svg>`,
};

const BTC_ICON = `<span class="btc-icon"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="12"/>
  <path d="M9.5 6.5v11M11.5 5.5v2M11.5 16.5v2M9 7h4.2a2.3 2.3 0 0 1 0 4.6H9m0 0h4.8a2.45 2.45 0 0 1 0 4.9H9"/></svg></span>`;

function shell(card, i, { label, accent }) {
  label = card.label || label;
  const el = document.createElement("section");
  const [c, r, w, h] = card.at;
  el.className = `card card--${card.type}`;
  el.style.gridArea = `${r} / ${c} / span ${h} / span ${w}`;
  el.style.animationDelay = `${i * 70}ms`;
  if (accent) el.style.setProperty("--accent", accent);
  el.innerHTML = `<div class="label"><span class="label__dot"></span><span>${pad(i + 1)} — ${label}</span><span class="label__right" data-status></span></div>`;
  return el;
}

const FOOTPRINTS = `
  <g class="ring__icon" transform="translate(80 33) scale(1.7)">
    <ellipse cx="7.2" cy="7.6" rx="3.1" ry="4.7" transform="rotate(-8 7.2 7.6)"/>
    <ellipse cx="7.9" cy="15.4" rx="2.3" ry="1.9"/>
    <ellipse cx="16.8" cy="11.1" rx="3.1" ry="4.7" transform="rotate(8 16.8 11.1)"/>
    <ellipse cx="16.1" cy="18.9" rx="2.3" ry="1.9"/>
  </g>`;

const RENDER = {
  steps(card, el) {
    const R = 86, C = 2 * Math.PI * R;
    el.insertAdjacentHTML("beforeend", `
      <div class="fill steps">
        <svg class="ring" viewBox="0 0 200 200">
          <defs><linearGradient id="ring-g" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stop-color="#7c5cff"/><stop offset=".55" stop-color="#ff5c93"/><stop offset="1" stop-color="#2ee6d6"/>
          </linearGradient></defs>
          <circle cx="100" cy="100" r="${R}" class="ring__track"/>
          <circle cx="100" cy="100" r="${R}" class="ring__arc" data-arc
            stroke-dasharray="${C}" stroke-dashoffset="${C}" transform="rotate(-90 100 100)"/>
          ${FOOTPRINTS}
          <text x="100" y="118" class="ring__num" data-today>—</text>
          <text x="100" y="146" class="ring__goal">/ ${fmtInt(card.goal)}</text>
        </svg>
        <div class="steps__empty" data-empty hidden></div>
      </div>`);

    const draw = (d) => {
      const status = $(el, "[data-status]");
      const empty = $(el, "[data-empty]");
      if (d.state === "disconnected" || (d.state === "error" && d.today == null)) {
        el.classList.add("is-empty");
        empty.hidden = false;
        empty.innerHTML = d.state === "disconnected"
          ? `<b>Not connected</b><small>Sign in at /auth — see README</small>`
          : `<b>Can’t reach Google Health</b><small>${d.reason || ""}</small>`;
        status.textContent = "";
        return;
      }
      el.classList.remove("is-empty");
      empty.hidden = true;
      status.textContent = d.data_through
        ? "AS OF " + new Date(d.data_through * 1000).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })
        : "";
      $(el, "[data-today]").textContent = fmtInt(d.today);
      $(el, "[data-arc]").style.strokeDashoffset = C * (1 - Math.min(1, d.today / card.goal));
      el.classList.toggle("is-done", d.today >= card.goal);
    };

    const poll = async () => {
      // ?demo renders sample data, for layout work without a Google sign-in.
      if (location.search.includes("demo")) {
        return draw({ state: "ok", today: 6482, data_through: Date.now() / 1000 - 180 });
      }
      try { draw(await (await fetch("/api/steps")).json()); }
      catch (e) { $(el, "[data-status]").textContent = "OFFLINE"; }
    };
    poll(); setInterval(poll, card.refreshMs);
  },

  // BTC/CAD: price, change pill and an area sparkline over the last month.
  btc(card, el) {
    el.insertAdjacentHTML("beforeend", `
      <div class="fill btc">
        <div class="btc__head">${BTC_ICON}<div class="btc__price" data-price>—</div><span class="pill" data-chg></span></div>
        <svg class="btc__chart" viewBox="0 0 300 100" preserveAspectRatio="none">
          <defs><linearGradient id="spark-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="currentColor" stop-opacity=".35"/><stop offset="1" stop-color="currentColor" stop-opacity="0"/>
          </linearGradient></defs>
          <line data-ref x1="0" x2="300" class="btc__ref"/>
          <path data-area fill="url(#spark-fill)"/><path data-line class="btc__line"/>
        </svg>
        <div class="btc__foot"><span data-period></span><span data-range></span></div>
      </div>`);
    feed("/api/btc", 60_000, (d) => {
      if (d.state === "error") return;
      el.style.setProperty("--dir", d.change_pct >= 0 ? UP : DOWN);
      $(el, "[data-price]").textContent = fmtCad(d.price);
      $(el, "[data-chg]").textContent = fmtPct(d.change_pct);
      const p = sparkPaths(d.series, 300, 100, 6);
      $(el, "[data-line]").setAttribute("d", p.line);
      $(el, "[data-area]").setAttribute("d", p.area);
      const ry = Math.max(0, Math.min(100, p.y(d.ref)));
      $(el, "[data-ref]").setAttribute("y1", ry); $(el, "[data-ref]").setAttribute("y2", ry);
      $(el, "[data-period]").textContent = `${d.days}D`;
      $(el, "[data-range]").textContent = `L ${fmtK(d.low)} · H ${fmtK(d.high)}`;
    });
  },

};

RENDER.coin = (card, el) => {
  el.insertAdjacentHTML("beforeend", `
    <div class="fill coin">
      <div class="coin__price" data-price>—</div>
      <div class="coin__chg" data-chg></div>
    </div>
    <span class="coin__icon">${COIN_ICONS[card.symbol]}</span>`);
  feed("/api/coins", 60_000, (d) => {
    const c = d.coins && d.coins[card.symbol];
    if (!c) return;
    $(el, "[data-price]").textContent = fmtPrice(c.price);
    const chg = $(el, "[data-chg]");
    chg.textContent = `${fmtPct(c.change_pct)} · ${c.days}D`;
    chg.style.color = c.change_pct >= 0 ? UP : DOWN;
  });
};

// TV switches (capture/tvctl.py): same state as the phone page on the key relay. The Pi
// preview switch lives only on the phone page (debugging); the preview card's OK opens it on the Pi.
RENDER.tv = (card, el) => {
  const SW = [
    ["subtitles", "Subtitles", (s) => (s.subtitles ? (s.captions ? "Whisper running" : "starting…") : "off")],
  ];
  el.insertAdjacentHTML("beforeend", `<div class="fill tv">${SW.map(([k, name]) => `
    <button class="tv__row" data-sw="${k}" data-nav role="switch" aria-checked="false" tabindex="-1">
      <span class="tv__txt"><b>${name}</b><small data-sub></small></span><span class="tv__sw"></span>
    </button>`).join("")}</div>`);
  let busy = false;
  const draw = (s) => {
    if (s.error) return;
    for (const [k, , sub] of SW) {
      const row = $(el, `[data-sw="${k}"]`);
      row.setAttribute("aria-checked", String(!!s[k]));
      $(row, "[data-sub]").textContent = sub(s);
    }
    $(el, "[data-status]").textContent = s.hub ? "" : "HUB DOWN";
  };
  feed("/api/tv", 5_000, (s) => { if (!busy) draw(s); });
  el.querySelectorAll("[data-sw]").forEach((row) => row.addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    const on = row.getAttribute("aria-checked") !== "true";
    row.setAttribute("aria-checked", String(on));
    row.classList.add("is-busy");
    try { draw(await (await fetch(`/api/tv/${row.dataset.sw}/${on ? "on" : "off"}`, { method: "POST" })).json()); }
    catch { $(el, "[data-status]").textContent = "OFFLINE"; }
    finally { busy = false; row.classList.remove("is-busy"); }
  }));
};

// Live preview: motion JPEG from the capture hub's frames (/api/frame.mjpg, 480 px at 12 fps:
// ~2 Mb/s, ~0.2 s behind the box, no audio). <img> plays it natively, also in the TV app's WebView.
// OK opens the real thing for this platform.
RENDER.preview = (card, el) => {
  const hint = { tv: "OK  to watch", pi: "OK  for full screen", web: "Open the stream ↗" }[PLATFORM];
  el.setAttribute("data-nav", ""); el.tabIndex = -1;
  el.insertAdjacentHTML("afterbegin", `
    <img class="preview__img" alt="" data-img hidden>
    <div class="preview__empty" data-empty>NO SIGNAL</div>
    <div class="preview__shade"></div>`);
  el.insertAdjacentHTML("beforeend", `
    <div class="preview__foot"><span class="live-pill">LIVE</span><span class="preview__hint">${hint}</span></div>`);
  const img = $(el, "[data-img]"), empty = $(el, "[data-empty]");
  let playing = false, paused = false;
  const start = () => {
    img.src = `/api/frame.mjpg?w=480&fps=12&n=${Date.now()}`;   // small card: low bandwidth
    playing = true;
  };
  const stop = () => { img.removeAttribute("src"); playing = false; };
  img.addEventListener("load", () => { empty.hidden = true; img.hidden = false; });
  img.addEventListener("error", () => { playing = false; empty.hidden = false; img.hidden = true; });
  // The stream ends when the hub stops; check every few seconds and restart it.
  const watch = async () => {
    if (paused || document.hidden) { if (playing) stop(); return; }
    const ok = await fetch("/api/frame.jpg?w=160", { cache: "no-store" }).then((r) => r.ok).catch(() => false);
    if (!ok) { stop(); empty.hidden = false; img.hidden = true; }
    else if (!playing) start();
  };
  watch(); setInterval(watch, 4000);
  document.addEventListener("visibilitychange", watch);
  // The TV app pauses the preview while its full-screen player runs (no double download).
  window.monetPreview = (on) => { paused = !on; on ? watch() : stop(); };
  const clock = () => ($(el, "[data-status]").textContent =
    new Date().toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }));
  clock(); setInterval(clock, 15_000);

  el.addEventListener("click", async () => {
    if (PLATFORM === "pi") return fetch("/api/tv/preview/on", { method: "POST" });
    // the TV app and browsers watch the stream: make sure the hub is sending it
    const s = await fetch("/api/tv").then((r) => r.json()).catch(() => ({}));
    if (!s.cast) await fetch("/api/tv/cast/on", { method: "POST" }).catch(() => {});
    if (PLATFORM === "tv") window.MonetTV?.openTv();
    else window.open(`http://${location.hostname}:8888/tv/`, "_blank");
  });
};

// Weather (Open-Meteo via /api/weather): condition, temperature, feels-like, humidity,
// and the city's local time.
const WX_ICONS = {
  clear: `<circle cx="12" cy="12" r="4.5"/><path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M5.3 18.7l1.6-1.6M17.1 6.9l1.6-1.6"/>`,
  night: `<path d="M19 14.5A7.5 7.5 0 0 1 9.5 5a7.5 7.5 0 1 0 9.5 9.5z"/>`,
  partly: `<circle cx="8.5" cy="8.5" r="3.2"/><path d="M8.5 2.5v1.4M2.5 8.5h1.4M4.3 4.3l1 1M12.7 4.3l-1 1"/><path d="M8 19.5h9.5a3.5 3.5 0 0 0 0-7 5 5 0 0 0-9.4 1.3A2.9 2.9 0 0 0 8 19.5z"/>`,
  cloud: `<path d="M7 18.5h10.5a4 4 0 0 0 0-8 6 6 0 0 0-11.3 1.6A3.3 3.3 0 0 0 7 18.5z"/>`,
  rain: `<path d="M7 14.5h10.5a4 4 0 0 0 0-8 6 6 0 0 0-11.3 1.6A3.3 3.3 0 0 0 7 14.5z"/><path d="M8.5 17.5l-1 2.5M12.5 17.5l-1 2.5M16.5 17.5l-1 2.5"/>`,
  snow: `<path d="M7 14.5h10.5a4 4 0 0 0 0-8 6 6 0 0 0-11.3 1.6A3.3 3.3 0 0 0 7 14.5z"/><path d="M8 18.5h.01M12 20h.01M16 18.5h.01M10 21.5h.01M14 21.5h.01"/>`,
  storm: `<path d="M7 14.5h10.5a4 4 0 0 0 0-8 6 6 0 0 0-11.3 1.6A3.3 3.3 0 0 0 7 14.5z"/><path d="M12.5 15.5l-2 3.5h3l-2 3.5"/>`,
  fog: `<path d="M4 9h16M6 13h12M4 17h16"/>`,
};

RENDER.weather = (card, el) => {
  el.insertAdjacentHTML("beforeend", `
    <div class="fill wx">
      <div class="wx__main"><div class="wx__temp" data-temp>—</div><svg class="wx__icon" viewBox="0 0 24 24" data-icon></svg></div>
      <div class="wx__line"><span data-desc></span><span data-time></span></div>
      <div class="wx__meta"><span>Feels <b data-feels>—</b></span><span>Hum <b data-hum>—</b></span></div>
    </div>`);
  let offset = null;
  const clock = () => {
    if (offset == null) return;
    const t = new Date(Date.now() + offset * 1000);
    $(el, "[data-time]").textContent = t.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: "UTC" });
  };
  setInterval(clock, 15_000);
  feed("/api/weather", 300_000, (d) => {
    const c = d.cities && d.cities[card.city];
    if (!c) return;
    offset = c.utc_offset_s; clock();
    const deg = (v) => `${Math.round(v)}°`;
    $(el, "[data-temp]").textContent = deg(c.temp);
    $(el, "[data-desc]").textContent = c.desc;
    $(el, "[data-feels]").textContent = deg(c.feels);
    $(el, "[data-hum]").textContent = `${Math.round(c.humidity)}%`;
    const icon = c.icon === "clear" && !c.day ? "night" : c.icon;
    $(el, "[data-icon]").innerHTML = WX_ICONS[icon] || WX_ICONS.cloud;
    el.dataset.icon = icon;
  });
};

// ---------- remote / keyboard navigation ----------
// Arrow keys move between [data-nav] elements by position (nearest in that direction);
// Enter (the remote's OK) clicks. Same on the TV remote, a keyboard on the Pi, a laptop.
function nav() {
  let cur = null;
  const items = () => [...document.querySelectorAll("[data-nav]")];
  const focus = (el) => {
    if (!el) return;
    cur?.classList.remove("is-focus");
    cur = el; cur.classList.add("is-focus"); cur.focus({ preventScroll: true });
  };
  const DIRS = { ArrowUp: [0, -1], ArrowDown: [0, 1], ArrowLeft: [-1, 0], ArrowRight: [1, 0] };
  addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      if (cur) { e.preventDefault(); cur.click(); }
      return;
    }
    const d = DIRS[e.key];
    if (!d) return;
    e.preventDefault();
    if (!cur) return focus(items()[0]);
    const a = cur.getBoundingClientRect(), ax = a.x + a.width / 2, ay = a.y + a.height / 2;
    let best = null, bestScore = Infinity;
    for (const el of items()) {
      if (el === cur) continue;
      const b = el.getBoundingClientRect(), bx = b.x + b.width / 2, by = b.y + b.height / 2;
      const along = (bx - ax) * d[0] + (by - ay) * d[1];        // distance in the key's direction
      if (along <= 1) continue;
      const across = Math.abs((bx - ax) * d[1] + (by - ay) * d[0]);
      const score = along + 2 * across;
      if (score < bestScore) { best = el; bestScore = score; }
    }
    focus(best);
  });
  // Remote-driven screens start on the preview (not the first [data-nav] in page order,
  // which is the Subtitles switch); browsers wait for the first key.
  if (PLATFORM !== "web") setTimeout(() => focus($(document, ".card--preview") || items()[0]), 0);
}

const LABELS = {
  steps: ["Steps", "var(--teal)"],
  btc: ["BTC / CAD", "var(--amber)"],
  coin: ["Coin", "var(--violet)"],
  tv: ["TV", "var(--pink)"],
  preview: ["Live TV", "var(--pink)"],
  weather: ["Weather", "var(--teal)"],
};

const board = document.getElementById("board");
// Reload when the dashboard's files change on the Pi: the TV app and the kiosk keep the
// page open for days, and reopening the TV app resumes it rather than reloading.
(() => {
  let seen = null;
  const check = async () => {
    try {
      const v = (await (await fetch("/api/version", { cache: "no-store" })).json()).version;
      if (seen && v !== seen) location.reload();
      seen = v;
    } catch { /* Pi restarting: try again later */ }
  };
  check(); setInterval(check, 30_000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) check(); });
})();

CARDS.forEach((card, i) => {
  const [label, accent] = LABELS[card.type];
  const el = shell(card, i, { label, accent });
  board.append(el);
  RENDER[card.type](card, el);
});
nav();
