/* Metrics tab: usage compared by profile, model, source, project or origin, over the sessions
   the dashboard is watching. Uses the helpers of app.js (tokens, usd, dur, pct, esc). */
"use strict";

const MX_GROUPS = {
  profile: "Profile",
  model: "Model",
  source: "Claude vs Antigravity",
  project: "Project",
  origin: "Origin",
};
const MX_ORIGIN = { profile: "SpecOps profile", team: "SpecOps team", main: "main session", "built-in": "built-in" };
const MX_COLS = [
  ["key", "", (g) => g.key],
  ["agents", "Agents", (g) => g.agents],
  ["cost", "API cost", (g) => g.cost ?? -1],
  ["cost_per_agent", "$ / agent", (g) => g.cost_per_agent ?? -1],
  ["tokens_out", "Tokens out", (g) => g.tokens_out],
  ["cache_hit", "Cache hit", (g) => g.cache_hit ?? -1],
  ["calls_per_agent", "Calls / agent", (g) => g.calls_per_agent],
  ["error_rate", "Failed calls", (g) => g.error_rate],
  ["retries", "Retries", (g) => g.retries],
  ["avg_duration", "Avg time", (g) => g.avg_duration],
];

const mx = { by: "profile", data: null, sort: "cost", desc: true, error: "" };
try { mx.by = localStorage.getItem("specops.metrics.by") || mx.by; } catch {}
if (!MX_GROUPS[mx.by]) mx.by = "profile";

async function loadMetrics() {
  try {
    mx.data = await api(`/api/metrics?by=${encodeURIComponent(mx.by)}`);
    mx.error = "";
  } catch (err) {
    mx.error = err.message;
  }
  renderMetrics();
}
VIEWS.metrics = loadMetrics;

function window_(sec) {
  if (!sec) return "all the sessions found";
  return "the last " + dur(sec).replace(/ 0+[ms]$/, "");
}

function renderMetrics() {
  const main = $("#mx-main");
  const d = mx.data;
  if (!d) {
    setHTML(main, mx.error ? `<div class="notice error">${esc(mx.error)}</div>` : "");
    return;
  }
  const t = d.totals;
  const tiles = [
    ["Agents", String(t.agents), `${t.sessions} session${t.sessions === 1 ? "" : "s"}`],
    ["API cost", t.cost == null ? "—" : `${t.partial ? "≥" : "≈"} ${usd(t.cost)}`, t.cost_per_agent == null ? "" : `${usd(t.cost_per_agent)} per agent`],
    ["Tokens out", tokens(t.tokens_out), `${tokens(t.tokens_in)} in`],
    ["Cache hit", t.cache_hit == null ? "—" : pct(t.cache_hit), "of prompt tokens"],
    ["Failed calls", pct(t.error_rate), `of ${t.tool_calls.toLocaleString()} tool calls`],
    ["Retries", String(t.retries), "repeated calls"],
  ];
  const col = MX_COLS.find((c) => c[0] === mx.sort) || MX_COLS[2];
  const groups = [...d.groups].sort((a, b) => {
    const x = col[2](a), y = col[2](b);
    const cmp = typeof x === "string" ? x.localeCompare(y) : x - y;
    return mx.desc ? -cmp : cmp;
  });
  const maxCost = Math.max(0, ...d.groups.map((g) => g.cost || 0));
  const head = MX_COLS.map(([k, label]) => {
    const on = mx.sort === k;
    const name = k === "key" ? MX_GROUPS[mx.by] : label;
    return `<th class="${k === "key" ? "" : "num"}" aria-sort="${on ? (mx.desc ? "descending" : "ascending") : "none"}">
      <button type="button" data-mx-sort="${k}">${esc(name)}${on ? (mx.desc ? " ↓" : " ↑") : ""}</button></th>`;
  }).join("");
  const rows = groups.map((g) => {
    const share = maxCost && g.cost ? g.cost / maxCost : 0;
    const label = mx.by === "origin" ? MX_ORIGIN[g.key] || g.key : mx.by === "model" ? model(g.key) || g.key : g.key;
    return `<tr>
      <th scope="row" title="${esc(g.key)}">${esc(label)}</th>
      <td class="num">${g.agents}</td>
      <td class="num"><span class="cost-cell"><span class="bar" title="${g.cost == null ? "no price known for this model" : `${pct(share)} of the most expensive group`}"><i style="width:${(share * 100).toFixed(1)}%"></i></span>${g.cost == null ? "—" : usd(g.cost)}</span></td>
      <td class="num">${g.cost_per_agent == null ? "—" : usd(g.cost_per_agent)}</td>
      <td class="num">${tokens(g.tokens_out)}</td>
      <td class="num">${g.cache_hit == null ? "—" : pct(g.cache_hit)}</td>
      <td class="num">${g.calls_per_agent.toFixed(1)}</td>
      <td class="num${g.error_rate >= 0.1 ? " warn" : ""}">${pct(g.error_rate)}</td>
      <td class="num${g.retries >= 5 ? " warn" : ""}">${g.retries}</td>
      <td class="num">${dur(g.avg_duration)}</td></tr>`;
  }).join("");
  setHTML(main, `<div class="mx">
    <h1 class="sk-title">Metrics</h1>
    <div class="sub">Every agent in ${esc(window_(d.since))} that the dashboard is watching. Costs are
      estimated at API list prices${t.partial ? "; long transcripts were partly read, so totals are lower bounds" : ""}.</div>
    ${mx.error ? `<div class="notice error">${esc(mx.error)}</div>` : ""}
    <div class="tabs mx-by" role="tablist">${Object.entries(MX_GROUPS).map(([k, label]) =>
      `<button class="tab ${mx.by === k ? "on" : ""}" role="tab" aria-selected="${mx.by === k}" data-mx-by="${k}">${esc(label)}</button>`).join("")}</div>
    <div class="mx-tiles">${tiles.map(([label, value, note]) =>
      `<div class="mx-tile"><span class="lbl">${label}</span><b>${value}</b><span class="note">${esc(note)}</span></div>`).join("")}</div>
    ${groups.length ? `<div class="mx-table-wrap"><table class="mx-table"><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></div>`
      : `<p class="hint">No agents in this window yet.</p>`}
    <div class="mx-ask">${icon("agent")}<div>For a written analysis, ask the <b>usage-analyst</b> agent; for
      what to change, <b>usage-optimizer</b> (add them under <a href="#profiles">Profiles</a>). Both read
      <code>specops metrics --json</code>, which takes the same groupings and a <code>--since</code> window.</div></div>
  </div>`);
}

$("#metrics-view").addEventListener("click", (e) => {
  const by = e.target.closest("[data-mx-by]");
  if (by) {
    mx.by = by.dataset.mxBy;
    try { localStorage.setItem("specops.metrics.by", mx.by); } catch {}
    return loadMetrics();
  }
  const sort = e.target.closest("[data-mx-sort]");
  if (sort) {
    const k = sort.dataset.mxSort;
    mx.desc = mx.sort === k ? !mx.desc : k !== "key";
    mx.sort = k;
    renderMetrics();
  }
});
setInterval(() => { if (document.body.dataset.view === "metrics") loadMetrics(); }, 15000);

setView(viewFromHash());
