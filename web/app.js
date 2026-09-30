// Board layout: each card is { type, at: [col, row, width, height], ...props }
// on a 12 x 8 grid. Renderers below turn a card definition into DOM.
const CARDS = [
  { type: "steps", at: [1, 1, 2, 2], goal: 10000, refreshMs: 30_000 },
  { type: "btc", at: [3, 1, 3, 2] },
  { type: "coin", at: [6, 1, 1, 1], symbol: "eth", label: "ETH" },
  { type: "coin", at: [6, 2, 1, 1], symbol: "ada", label: "ADA" },
];

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

const LABELS = {
  steps: ["Steps", "var(--teal)"],
  btc: ["BTC / CAD", "var(--amber)"],
  coin: ["Coin", "var(--violet)"],
};

const board = document.getElementById("board");
CARDS.forEach((card, i) => {
  const [label, accent] = LABELS[card.type];
  const el = shell(card, i, { label, accent });
  board.append(el);
  RENDER[card.type](card, el);
});
