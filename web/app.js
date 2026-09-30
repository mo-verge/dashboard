// Board layout: each card is { type, at: [col, row, width, height], ...props }
// on a 12 x 8 grid. Renderers below turn a card definition into DOM.
const CARDS = [
  { type: "steps", at: [1, 1, 6, 5], device: "Charge 6", goal: 10000, refreshMs: 30_000 },
];

const $ = (root, sel) => root.querySelector(sel);
const pad = (n) => String(n).padStart(2, "0");
const fmtInt = (n) => Math.round(n).toLocaleString("en-US");

function shell(card, i, { label, accent }) {
  const el = document.createElement("section");
  const [c, r, w, h] = card.at;
  el.className = `card card--${card.type}`;
  el.style.gridArea = `${r} / ${c} / span ${h} / span ${w}`;
  el.style.animationDelay = `${i * 70}ms`;
  if (accent) el.style.setProperty("--accent", accent);
  el.innerHTML = `<div class="label"><span class="label__dot"></span><span>${pad(i + 1)} — ${label}</span><span class="label__right" data-status></span></div>`;
  return el;
}

const RENDER = {
  steps(card, el) {
    const R = 88, C = 2 * Math.PI * R;
    el.insertAdjacentHTML("beforeend", `
      <div class="fill steps">
        <div class="steps__top">
          <div>
            <div class="steps__big" data-today>—</div>
            <div class="steps__goal"><span data-pct>—</span> of ${fmtInt(card.goal)} goal</div>
          </div>
          <svg class="ring" viewBox="0 0 200 200">
            <defs><linearGradient id="ring-g" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0" stop-color="#7c5cff"/><stop offset=".55" stop-color="#ff5c93"/><stop offset="1" stop-color="#2ee6d6"/>
            </linearGradient></defs>
            <circle cx="100" cy="100" r="${R}" class="ring__track"/>
            <circle cx="100" cy="100" r="${R}" class="ring__arc" data-arc
              stroke-dasharray="${C}" stroke-dashoffset="${C}" transform="rotate(-90 100 100)"/>
            <text x="100" y="96" class="ring__num" data-left>—</text>
            <text x="100" y="124" class="ring__sub">TO GO</text>
          </svg>
        </div>
        <div class="hours" data-hours></div>
        <div class="hours__axis"><span>00</span><span>06</span><span>12</span><span>18</span><span>24</span></div>
        <div class="week" data-week></div>
        <div class="steps__empty" data-empty hidden></div>
      </div>`);

    const draw = (d) => {
      const status = $(el, "[data-status]");
      const empty = $(el, "[data-empty]");
      if (d.state === "disconnected" || (d.state === "error" && d.today == null)) {
        el.classList.add("is-empty");
        empty.hidden = false;
        empty.innerHTML = d.state === "disconnected"
          ? `<b>Connect Google Health</b>
             <p>From your Mac run <code>ssh -L 8080:127.0.0.1:8080 monet-wifi-2</code>, then open <code>http://127.0.0.1:8080/auth</code>.</p>
             <small>${d.reason || ""}</small>`
          : `<b>Couldn’t reach Google Health</b><small>${d.reason || ""}</small>`;
        status.textContent = d.state === "disconnected" ? "NOT CONNECTED" : "ERROR";
        return;
      }
      el.classList.remove("is-empty");
      empty.hidden = true;
      const through = d.data_through
        ? new Date(d.data_through * 1000).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })
        : "—";
      status.textContent = `${card.device.toUpperCase()} · AS OF ${through}${d.state === "stale" ? " · RETRYING" : ""}`;

      const pct = Math.min(1, d.today / card.goal);
      $(el, "[data-today]").textContent = fmtInt(d.today);
      $(el, "[data-pct]").textContent = Math.round((d.today / card.goal) * 100) + "%";
      $(el, "[data-arc]").style.strokeDashoffset = C * (1 - pct);
      $(el, "[data-left]").textContent = d.today >= card.goal ? "✓" : fmtInt(card.goal - d.today);

      const nowH = new Date().getHours();
      const peak = Math.max(500, ...d.hours);
      $(el, "[data-hours]").innerHTML = d.hours.map((v, h) =>
        `<i class="${h > nowH ? "is-future" : h === nowH ? "is-now" : ""}" style="--h:${Math.max(0.02, v / peak)}"></i>`).join("");

      const top = Math.max(card.goal, ...d.week.map((w) => w.steps));
      $(el, "[data-week]").innerHTML = d.week.map((w, k) => {
        const day = new Date(w.date + "T12:00");
        const isToday = k === d.week.length - 1;
        return `<div class="${isToday ? "is-today" : ""}${w.steps >= card.goal ? " is-hit" : ""}">
          <span class="week__bar"><i style="--h:${Math.max(0.03, w.steps / top)}"></i><em style="--g:${card.goal / top}"></em></span>
          <b>${w.steps >= 1000 ? (w.steps / 1000).toFixed(1) + "k" : w.steps}</b>
          <span>${isToday ? "TODAY" : day.toLocaleDateString("en-US", { weekday: "short" }).toUpperCase()}</span>
        </div>`;
      }).join("");
    };

    // ?demo renders sample data, for layout work without a Google sign-in.
    const demo = () => {
      const nowH = new Date().getHours();
      const hours = Array.from({ length: 24 }, (_, h) =>
        h > nowH || h < 7 ? 0 : Math.round(300 + 900 * Math.abs(Math.sin(h * 1.7))));
      const week = Array.from({ length: 7 }, (_, k) => ({
        date: new Date(Date.now() - (6 - k) * 864e5).toISOString().slice(0, 10),
        steps: k === 6 ? hours.reduce((a, b) => a + b, 0) : [8420, 11230, 6120, 12890, 9540, 10310][k],
      }));
      return { state: "ok", today: week[6].steps, hours, week, data_through: Date.now() / 1000 - 180 };
    };

    const poll = async () => {
      if (location.search.includes("demo")) return draw(demo());
      try { draw(await (await fetch("/api/steps")).json()); }
      catch (e) { $(el, "[data-status]").textContent = "OFFLINE"; }
    };
    poll(); setInterval(poll, card.refreshMs);
  },
};

const LABELS = { steps: ["Steps", "var(--teal)"] };

const board = document.getElementById("board");
CARDS.forEach((card, i) => {
  const [label, accent] = LABELS[card.type];
  const el = shell(card, i, { label, accent });
  board.append(el);
  RENDER[card.type](card, el);
});
