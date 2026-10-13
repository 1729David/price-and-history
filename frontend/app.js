// Price & History frontend. No frameworks, no build step.

const $ = (id) => document.getElementById(id);

const state = {
  indicators: [],   // [{key, name, source, symbol, unit}]
  rows: [],         // rows for the series currently shown
  symbol: null,
  freshDates: new Map(), // symbol -> Set of dates fetched fresh in the last fetch of that symbol
};

// ---------- API helper ----------

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function setStatus(message, kind = "") {
  const el = $("status");
  el.textContent = message;
  el.className = `status ${kind}`;
}

async function withBusy(button, task) {
  button.disabled = true;
  try { await task(); } finally { button.disabled = false; }
}

// ---------- Formatting ----------

const fmtPrice = (v) => v == null ? "–" : v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtVolume = (v) => v == null ? "–" : Math.round(v).toLocaleString();
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const isoDate = (d) => d.toISOString().slice(0, 10);

function describeResults(results) {
  for (const r of results) {
    if (r.ok) state.freshDates.set(r.symbol, new Set(r.fresh_dates));
  }
  const lines = results.map((r) => r.ok
    ? `${r.symbol}: ${r.fetched} fetched, ${r.cached} from cache`
    : `${r.symbol}: ${r.error}`);
  const failed = results.some((r) => !r.ok);
  setStatus(lines.join("\n"), failed ? "error" : "ok");
}

// ---------- Indicators ----------

async function loadIndicators() {
  state.indicators = await api("/api/indicators");
  $("indicator").innerHTML = state.indicators
    .map((i) => `<option value="${i.key}">${i.name}</option>`)
    .join("");
  showIndicatorMeta();
}

function showIndicatorMeta() {
  const ind = state.indicators.find((i) => i.key === $("indicator").value);
  if (!ind) return;
  const source = ind.source === "fred" ? "FRED" : "Yahoo Finance";
  $("indicator-meta").textContent = `${ind.symbol} from ${source}, in ${ind.unit}`;
}

async function fetchIndicator() {
  const key = $("indicator").value;
  const ind = state.indicators.find((i) => i.key === key);
  setStatus(`Fetching ${ind.name}…`);
  const { results } = await api("/api/indicators/fetch", {
    method: "POST",
    body: { keys: [key], start: $("start").value, end: $("end").value },
  });
  describeResults(results);
  await refreshStored(results.find((r) => r.ok)?.symbol);
}

// ---------- Watchlist ----------

function renderWatchlist(symbols) {
  const list = $("watchlist");
  if (!symbols.length) {
    list.innerHTML = `<li class="empty">No stocks yet. Add a ticker above.</li>`;
    return;
  }
  list.innerHTML = symbols
    .map((s) => `<li>${s}<button type="button" data-remove="${s}" aria-label="Remove ${s}">×</button></li>`)
    .join("");
}

async function loadWatchlist() {
  renderWatchlist(await api("/api/watchlist"));
}

async function addTickers(event) {
  event.preventDefault();
  const input = $("ticker-input");
  if (!input.value.trim()) return;
  try {
    renderWatchlist(await api("/api/watchlist", { method: "POST", body: { symbols: input.value } }));
    input.value = "";
    setStatus("");
  } catch (err) {
    setStatus(err.message, "error");
  }
}

async function removeTicker(event) {
  const symbol = event.target.dataset.remove;
  if (!symbol) return;
  renderWatchlist(await api(`/api/watchlist/${encodeURIComponent(symbol)}`, { method: "DELETE" }));
}

async function fetchStocks() {
  setStatus("Fetching watchlist prices…");
  const { results } = await api("/api/stocks/fetch", {
    method: "POST",
    body: { start: $("start").value, end: $("end").value },
  });
  describeResults(results);
  await refreshStored(results.find((r) => r.ok)?.symbol);
}

// ---------- Stored data ----------

function labelFor(symbol) {
  const ind = state.indicators.find((i) => i.symbol === symbol);
  return ind ? `${ind.name} (${symbol})` : symbol;
}

async function refreshStored(selectSymbol) {
  const summary = await api("/api/stored");

  $("series").innerHTML = summary.length
    ? summary.map((s) => `<option value="${s.symbol}">${labelFor(s.symbol)}</option>`).join("")
    : `<option value="">Nothing stored yet</option>`;

  $("stored-body").innerHTML = summary.length
    ? summary.map((s) => `
        <tr data-symbol="${s.symbol}">
          <td>${s.symbol}</td><td>${s.source}</td><td class="num">${s.rows.toLocaleString()}</td>
          <td>${s.first_date}</td><td>${s.last_date}</td>
        </tr>`).join("")
    : `<tr><td colspan="5" class="empty">Fetch an indicator or stock to start building history.</td></tr>`;

  const target = selectSymbol || state.symbol || summary[0]?.symbol;
  if (target) {
    $("series").value = target;
    await showSeries(target);
  } else {
    state.rows = [];
    render();
  }
}

async function showSeries(symbol) {
  if (!symbol) return;
  const { rows } = await api(`/api/prices?symbol=${encodeURIComponent(symbol)}`);
  state.symbol = symbol;
  state.rows = rows;
  render();
}

// ---------- Rendering ----------

function render() {
  renderLatest();
  renderTable();
  drawChart();
  loadNews(state.symbol).catch((err) => { $("news-status").textContent = err.message; });
}

function renderLatest() {
  const rows = state.rows;
  if (!rows.length) { $("latest").innerHTML = ""; return; }
  const last = rows[rows.length - 1];
  const first = rows[0];
  const change = first.close ? ((last.close - first.close) / first.close) * 100 : 0;
  const dir = change >= 0 ? "up" : "down";
  $("latest").innerHTML = `
    <div class="value">${fmtPrice(last.close)}</div>
    <div class="meta">Close on ${last.date}.
      <span class="${dir}">${change >= 0 ? "+" : ""}${change.toFixed(1)}%</span> since ${first.date}</div>`;
}

function renderTable() {
  const recent = state.rows.slice(-30).reverse();
  const fresh = state.freshDates.get(state.symbol);
  $("rows-body").innerHTML = recent.length
    ? recent.map((r) => {
        const badge = !fresh ? "" : fresh.has(r.date)
          ? `<span class="badge fresh">Fetched</span>`
          : `<span class="badge cached">Database</span>`;
        return `
        <tr>
          <td>${badge}</td>
          <td>${r.date}</td><td class="num">${fmtPrice(r.open)}</td><td class="num">${fmtPrice(r.high)}</td>
          <td class="num">${fmtPrice(r.low)}</td><td class="num">${fmtPrice(r.close)}</td>
          <td class="num">${fmtVolume(r.volume)}</td>
        </tr>`;
      }).join("")
    : `<tr><td colspan="7" class="empty">No rows to show.</td></tr>`;
}

function drawChart() {
  const canvas = $("chart");
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = 360;
  canvas.width = width * dpr;
  canvas.height = height * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const css = getComputedStyle(document.documentElement);
  const color = (name) => css.getPropertyValue(name).trim();
  ctx.font = `12px ${color("--font")}`;

  const points = state.rows.filter((r) => r.close != null);
  if (points.length < 2) {
    ctx.fillStyle = color("--ink-soft");
    ctx.fillText("Fetch some data, then pick a series to chart it.", 20, height / 2);
    return;
  }

  const pad = { top: 20, right: 16, bottom: 28, left: 64 };
  const w = width - pad.left - pad.right;
  const h = height - pad.top - pad.bottom;
  const closes = points.map((p) => p.close);
  let min = Math.min(...closes), max = Math.max(...closes);
  const margin = (max - min) * 0.05 || 1;
  min -= margin; max += margin;

  const x = (i) => pad.left + (i / (points.length - 1)) * w;
  const y = (v) => pad.top + (1 - (v - min) / (max - min)) * h;

  // Horizontal grid with price labels
  ctx.strokeStyle = color("--rule");
  ctx.fillStyle = color("--ink-soft");
  ctx.lineWidth = 1;
  ctx.textAlign = "right";
  for (let i = 0; i <= 4; i++) {
    const v = min + ((max - min) * i) / 4;
    const yy = Math.round(y(v)) + 0.5;
    ctx.beginPath(); ctx.moveTo(pad.left, yy); ctx.lineTo(width - pad.right, yy); ctx.stroke();
    ctx.fillText(fmtPrice(v), pad.left - 8, yy + 4);
  }

  // Date labels: first, middle, last
  ctx.textAlign = "center";
  [0, Math.floor((points.length - 1) / 2), points.length - 1].forEach((i, n) => {
    ctx.textAlign = n === 0 ? "left" : n === 2 ? "right" : "center";
    ctx.fillText(points[i].date, x(i), height - 8);
  });

  // Indicators draw in brass, stocks in steel blue
  const isIndicator = state.indicators.some((i) => i.symbol === state.symbol);
  ctx.strokeStyle = color(isIndicator ? "--brass" : "--steel");
  ctx.lineWidth = 1.75;
  ctx.lineJoin = "round";
  ctx.beginPath();
  points.forEach((p, i) => (i ? ctx.lineTo(x(i), y(p.close)) : ctx.moveTo(x(i), y(p.close))));
  ctx.stroke();
}

// ---------- News (fetched by custom-agent, stored here) ----------

async function loadNews(symbol) {
  if (!symbol) { $("news-body").innerHTML = ""; return; }
  const news = await api(`/api/news/${encodeURIComponent(symbol)}`);
  if (symbol !== state.symbol) return; // user switched series while this was loading
  if (!news.articles.length) {
    $("news-body").innerHTML = `<p class="hint">No news stored for ${esc(symbol)} yet.</p>`;
    return;
  }
  const overview = news.overview
    ? `<p class="meta">Overview generated ${esc(news.overview.generated_at)}</p><p class="news-overview">${esc(news.overview.overview)}</p>`
    : "";
  $("news-body").innerHTML = overview + news.articles.map((a) => `
    <article class="news-article">
      <h3><a href="${esc(a.url)}" target="_blank" rel="noopener noreferrer">${esc(a.title || a.url)}</a></h3>
      <p class="meta">${esc(a.source || "Unknown source")} · ${esc((a.published_at || a.retrieved_at || "").slice(0, 10))}</p>
      <p>${esc(a.summary)}</p>
    </article>`).join("");
}

function describeProgress(job) {
  const counts = job.progress?.counts || {};
  const done = counts.done || 0;
  const target = job.progress?.target;
  return `${job.status}${job.progress ? ` (${job.progress.stage}, ${done}${target ? ` of ${target}` : ""} articles done)` : ""}`;
}

async function fetchNews() {
  const symbol = state.symbol;
  if (!symbol) throw new Error("Pick a stored series first.");
  $("news-status").textContent = `Queuing news for ${symbol}…`;
  const { job_id } = await api(`/api/news/${encodeURIComponent(symbol)}`, { method: "POST", body: {} });
  // Jobs take minutes. The server stores the result on the first poll that sees it finished.
  for (;;) {
    const job = await api(`/api/news/jobs/${job_id}`);
    if (job.status === "done") break;
    if (job.status === "failed") throw new Error(job.error || "News job failed.");
    if (job.status === "paused") {
      throw new Error(`News job paused: ${job.error || "custom-agent stopped it"}. Resume it on custom-agent, then fetch again.`);
    }
    $("news-status").textContent = `${symbol}: ${describeProgress(job)}`;
    await new Promise((resolve) => setTimeout(resolve, 3000));
  }
  $("news-status").textContent = "";
  await loadNews(symbol);
}

// ---------- Wiring ----------

function initDates() {
  const end = new Date();
  const start = new Date();
  start.setFullYear(end.getFullYear() - 1);
  $("start").value = isoDate(start);
  $("end").value = isoDate(end);
}

function guard(fn) {
  return (event) => fn(event).catch((err) => setStatus(err.message, "error"));
}

document.addEventListener("DOMContentLoaded", async () => {
  initDates();

  $("indicator").addEventListener("change", showIndicatorMeta);
  $("fetch-indicator").addEventListener("click", (e) => withBusy(e.currentTarget, guard(fetchIndicator)));
  $("watchlist-form").addEventListener("submit", addTickers);
  $("watchlist").addEventListener("click", guard(removeTicker));
  $("fetch-stocks").addEventListener("click", (e) => withBusy(e.currentTarget, guard(fetchStocks)));
  $("fetch-news").addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
    try { await fetchNews(); } catch (err) { $("news-status").textContent = err.message; }
  }));
  $("series").addEventListener("change", guard((e) => showSeries(e.target.value)));
  $("stored-body").addEventListener("click", guard(async (e) => {
    const symbol = e.target.closest("tr")?.dataset.symbol;
    if (symbol) { $("series").value = symbol; await showSeries(symbol); }
  }));

  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(drawChart, 100);
  });

  try {
    await loadIndicators();
    await loadWatchlist();
    await refreshStored();
  } catch (err) {
    setStatus(`Can't reach the backend. Is python backend/app.py running?\n${err.message}`, "error");
  }
});
