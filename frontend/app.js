// Price & History frontend. No frameworks, no build step.

const $ = (id) => document.getElementById(id);

const state = {
  indicators: [],   // [{key, name, source, symbol, unit}]
  rows: [],         // rows for the series currently shown
  symbol: null,
  selected: null,   // date of the day pinned on the chart
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

// Just enough Markdown for the agent's overviews: headings, bold/italic/code, nested lists, paragraphs.
// Each line is escaped before any tags are added, so the only HTML in the output is ours.
function markdownHtml(text) {
  const inline = (s) => esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*\w])\*(?!\s)([^*]+?)\*(?![*\w])/g, "$1<em>$2</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[(\d{4}-\d{2}-\d{2})\]/g, `<span class="cite">[$1]</span>`); // keep dates from breaking at hyphens

  const out = [];
  const lists = []; // open lists, innermost last: {indent, tag}; each has an open <li>
  let para = [];

  const closePara = () => { if (para.length) out.push(`<p>${para.join(" ")}</p>`); para = []; };
  const closeList = () => out.push(`</li></${lists.pop().tag}>`);
  const closeAll = () => { closePara(); while (lists.length) closeList(); };

  for (const line of String(text ?? "").split("\n")) {
    const item = line.match(/^(\s*)([*+-]|\d+[.)])\s+(.*)$/);
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    if (item) {
      closePara();
      const indent = item[1].replace(/\t/g, "    ").length;
      const tag = /\d/.test(item[2]) ? "ol" : "ul";
      while (lists.length && indent < lists.at(-1).indent) closeList();
      const top = lists.at(-1);
      if (top && indent === top.indent && tag === top.tag) {
        out.push("</li>");
      } else {
        if (top && indent === top.indent) closeList();
        out.push(`<${tag}>`);
        lists.push({ indent, tag });
      }
      out.push(`<li>${inline(item[3])}`);
    } else if (heading) {
      closeAll();
      out.push(`<h4>${inline(heading[1])}</h4>`);
    } else if (!line.trim()) {
      closePara();
    } else if (lists.length && /^\s/.test(line)) {
      out.push(` ${inline(line.trim())}`); // continuation of the current list item
    } else {
      if (lists.length) closeAll();
      para.push(inline(line.trim()));
    }
  }
  closeAll();
  return out.join("");
}

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
  state.selected = null;
  render();
}

// ---------- Rendering ----------

function render() {
  renderLatest();
  renderTable();
  renderPointDetail();
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
  // The Source column only means something right after a fetch of this symbol.
  $("rows-table").classList.toggle("no-source", !fresh);
  $("rows-body").innerHTML = recent.length
    ? recent.map((r) => {
        const badge = !fresh ? "" : fresh.has(r.date)
          ? `<span class="badge fresh">Fetched</span>`
          : `<span class="badge cached">Database</span>`;
        return `
        <tr data-date="${r.date}"${r.date === state.selected ? ` class="selected"` : ""}>
          <td>${badge}</td>
          <td>${r.date}</td><td class="num">${fmtPrice(r.open)}</td><td class="num">${fmtPrice(r.high)}</td>
          <td class="num">${fmtPrice(r.low)}</td><td class="num">${fmtPrice(r.close)}</td>
          <td class="num">${fmtVolume(r.volume)}</td>
        </tr>`;
      }).join("")
    : `<tr><td colspan="7" class="empty">No rows to show.</td></tr>`;
}

// Geometry of the last chart drawn, so mouse positions can be mapped back to days.
let chart = null;
let hoverIndex = null;

function drawChart() {
  chart = null;
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
  const lineColor = color(isIndicator ? "--brass" : "--steel");
  ctx.strokeStyle = lineColor;
  ctx.lineWidth = 1.75;
  ctx.lineJoin = "round";
  ctx.beginPath();
  points.forEach((p, i) => (i ? ctx.lineTo(x(i), y(p.close)) : ctx.moveTo(x(i), y(p.close))));
  ctx.stroke();

  chart = { points, pad, w, width, height, x, y };

  const marker = (i, ink, dashed) => {
    const xx = Math.round(x(i)) + 0.5;
    ctx.save();
    ctx.strokeStyle = ink;
    ctx.lineWidth = 1;
    ctx.setLineDash(dashed ? [3, 3] : []);
    ctx.beginPath(); ctx.moveTo(xx, pad.top); ctx.lineTo(xx, height - pad.bottom); ctx.stroke();
    ctx.setLineDash([]);
    ctx.beginPath(); ctx.arc(x(i), y(points[i].close), 4, 0, Math.PI * 2);
    ctx.fillStyle = lineColor; ctx.fill();
    ctx.strokeStyle = color("--surface"); ctx.lineWidth = 2; ctx.stroke();
    ctx.restore();
  };

  const selectedIndex = points.findIndex((p) => p.date === state.selected);
  if (selectedIndex >= 0) marker(selectedIndex, color("--ink"), false);

  if (hoverIndex != null && hoverIndex < points.length && hoverIndex !== selectedIndex) {
    marker(hoverIndex, color("--ink-soft"), true);
    // Small date / close label at the top, kept inside the plot area.
    const p = points[hoverIndex];
    const text = `${p.date}  ${fmtPrice(p.close)}`;
    const tw = ctx.measureText(text).width + 12;
    const lx = Math.min(Math.max(x(hoverIndex) - tw / 2, pad.left), width - pad.right - tw);
    ctx.fillStyle = color("--ink");
    ctx.fillRect(lx, pad.top - 14, tw, 20);
    ctx.fillStyle = color("--surface");
    ctx.textAlign = "left";
    ctx.fillText(text, lx + 6, pad.top);
  }
}

function indexAt(event) {
  if (!chart) return null;
  const offsetX = event.clientX - event.currentTarget.getBoundingClientRect().left;
  const n = chart.points.length;
  const i = Math.round(((offsetX - chart.pad.left) / chart.w) * (n - 1));
  return Math.min(Math.max(i, 0), n - 1);
}

function selectDate(date) {
  state.selected = date;
  renderPointDetail();
  renderTable();
  drawChart();
}

function renderPointDetail() {
  const box = $("point-detail");
  const rows = state.rows;
  const i = rows.findIndex((r) => r.date === state.selected);
  if (i < 0) {
    box.innerHTML = rows.length ? `<p class="hint">Click the chart to see a day's prices.</p>` : "";
    return;
  }
  const r = rows[i];
  const prev = rows.slice(0, i).reverse().find((p) => p.close != null);
  const change = prev && r.close != null ? ((r.close - prev.close) / prev.close) * 100 : null;
  const changeHtml = change == null ? "–"
    : `<span class="${change >= 0 ? "up" : "down"}">${change >= 0 ? "+" : ""}${change.toFixed(2)}%</span>`;
  const stat = (label, value) => `<div><dt>${label}</dt><dd>${value}</dd></div>`;
  box.innerHTML = `
    <div class="pd-head">
      <strong>${r.date}</strong>
      <button type="button" class="link" data-clear>Clear</button>
    </div>
    <dl>
      ${stat("Open", fmtPrice(r.open))}
      ${stat("High", fmtPrice(r.high))}
      ${stat("Low", fmtPrice(r.low))}
      ${stat("Close", fmtPrice(r.close))}
      ${stat("Day change", changeHtml)}
      ${stat("Volume", fmtVolume(r.volume))}
    </dl>`;
}

function wireChart() {
  const canvas = $("chart");
  canvas.addEventListener("mousemove", (e) => {
    const i = indexAt(e);
    if (i !== hoverIndex) { hoverIndex = i; drawChart(); }
  });
  canvas.addEventListener("mouseleave", () => { hoverIndex = null; drawChart(); });
  canvas.addEventListener("click", (e) => {
    const i = indexAt(e);
    if (i != null) selectDate(chart.points[i].date);
  });
  canvas.addEventListener("keydown", (e) => {
    if (!chart) return;
    const points = chart.points;
    const current = points.findIndex((p) => p.date === state.selected);
    let next = null;
    if (e.key === "ArrowLeft") next = current < 0 ? points.length - 1 : Math.max(current - 1, 0);
    else if (e.key === "ArrowRight") next = current < 0 ? points.length - 1 : Math.min(current + 1, points.length - 1);
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = points.length - 1;
    else if (e.key === "Escape") { selectDate(null); return; }
    else return;
    e.preventDefault();
    selectDate(points[next].date);
  });
  $("point-detail").addEventListener("click", (e) => {
    if (e.target.closest("[data-clear]")) selectDate(null);
  });
  $("rows-body").addEventListener("click", (e) => {
    const date = e.target.closest("tr[data-date]")?.dataset.date;
    if (date) selectDate(date);
  });
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
    ? `<p class="meta">Overview generated ${esc(news.overview.generated_at)}</p><div class="news-overview">${markdownHtml(news.overview.overview)}</div>`
    : "";
  $("news-body").innerHTML = overview + news.articles.map(articleHtml).join("");
}

function articleHtml(a) {
  return `
    <article class="news-article">
      <h3><a href="${esc(a.url)}" target="_blank" rel="noopener noreferrer">${esc(a.title || a.url)}</a></h3>
      <p class="meta">${esc(a.source || "Unknown source")} · ${esc((a.published_at || a.retrieved_at || "").slice(0, 10))}</p>
      <p>${esc(a.summary)}</p>
    </article>`;
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

// ---------- News jobs: queue several tickers, watch them, open a finished job ----------

const ACTIVE = new Set(["queued", "running"]);
let jobsTimer = null;
let openJobId = null;

async function loadJobs() {
  const jobs = await api("/api/news/jobs");
  $("jobs-body").innerHTML = jobs.length
    ? jobs.map((j) => {
        const detail = j.status === "done"
          ? `<button class="link" data-job="${esc(j.job_id)}">View data</button>`
          : "";
        const counts = j.progress?.counts?.done;
        const status = ACTIVE.has(j.status) && j.progress
          ? `${j.status} (${esc(j.progress.stage)}${counts != null ? `, ${counts} done` : ""})`
          : esc(j.status) + (j.error ? ` – ${esc(j.error)}` : "");
        return `<tr>
          <td>${esc(j.symbol)}</td><td>${esc(j.requested_at)}</td>
          <td class="status-${esc(j.status)}">${status}</td>
          <td class="num">${j.article_count ?? "–"}</td><td>${detail}</td></tr>`;
      }).join("")
    : `<tr><td colspan="5" class="empty">No jobs yet. Queue some tickers to the left.</td></tr>`;

  // Keep polling only while something is running; stop once everything has settled.
  const anyActive = jobs.some((j) => ACTIVE.has(j.status));
  if (anyActive && !jobsTimer) {
    jobsTimer = setInterval(() => loadJobs().catch(() => {}), 3000);
  } else if (!anyActive && jobsTimer) {
    clearInterval(jobsTimer);
    jobsTimer = null;
    // A job just finished: refresh the per-ticker news view too.
    loadNews(state.symbol).catch(() => {});
  }
}

async function queueJobs(event) {
  event.preventDefault();
  const out = $("jobs-status");
  out.textContent = "Queuing…";
  try {
    const { results } = await api("/api/news", {
      method: "POST",
      body: {
        symbols: $("jobs-tickers").value,
        days: Number($("jobs-days").value),
        max_articles: Number($("jobs-max").value),
        refresh: $("jobs-refresh").checked,
      },
    });
    const ok = results.filter((r) => r.ok).map((r) => r.symbol);
    const failed = results.filter((r) => !r.ok);
    out.textContent = [
      ok.length ? `Queued: ${ok.join(", ")}` : "",
      ...failed.map((r) => `${r.symbol}: ${r.error}`),
    ].filter(Boolean).join("\n");
    if (ok.length) $("jobs-tickers").value = "";
    await loadJobs();
  } catch (err) {
    out.textContent = err.message;
  }
}

async function showJob(jobId) {
  const box = $("job-detail");
  box.textContent = "Loading…";
  const r = await api(`/api/news/jobs/${encodeURIComponent(jobId)}/result`);
  openJobId = jobId;
  const skipped = (r.skipped || []).length
    ? `<ul class="skipped">${r.skipped.map((x) => `<li>${esc(x.title || x.url)} – ${esc(x.reason)}</li>`).join("")}</ul>`
    : "";
  box.innerHTML = `
    <h3>${esc(r.symbol)}: ${r.articles.length} article${r.articles.length === 1 ? "" : "s"}</h3>
    <p class="meta">Generated ${esc(r.generated_at)}</p>
    <div class="news-overview">${markdownHtml(r.overview)}</div>
    ${r.articles.map(articleHtml).join("")}
    ${skipped ? `<h4>Skipped</h4>${skipped}` : ""}`;
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
  wireChart();

  $("indicator").addEventListener("change", showIndicatorMeta);
  $("fetch-indicator").addEventListener("click", (e) => withBusy(e.currentTarget, guard(fetchIndicator)));
  $("watchlist-form").addEventListener("submit", addTickers);
  $("watchlist").addEventListener("click", guard(removeTicker));
  $("fetch-stocks").addEventListener("click", (e) => withBusy(e.currentTarget, guard(fetchStocks)));
  $("fetch-news").addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
    try { await fetchNews(); } catch (err) { $("news-status").textContent = err.message; }
  }));
  $("jobs-form").addEventListener("submit", queueJobs);
  $("jobs-body").addEventListener("click", guard(async (e) => {
    const id = e.target.closest("button[data-job]")?.dataset.job;
    if (id) await showJob(id);
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
    await loadJobs();
  } catch (err) {
    setStatus(`Can't reach the backend. Is python backend/app.py running?\n${err.message}`, "error");
  }
});
