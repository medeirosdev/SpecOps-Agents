/* specops web UI: vanilla JS, no build step. Receives snapshots over SSE and patches the DOM. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);

const state = {
  snap: null,
  selected: null,
  follow: true,
  agent: null, // agent id shown in the drawer
  tab: "timeline",
  filter: "",
  showAllFinished: false,
  stream: null,
  streamFor: undefined,
};

try {
  const f = localStorage.getItem("specops.follow");
  if (f !== null) state.follow = f === "1";
} catch {}

/* ----------------------------------------------------------------- helpers */
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
const md = (s) =>
  esc(s)
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");

const ICONS = {
  read: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  shell: '<path d="m4 17 6-5-6-5"/><path d="M12 19h8"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  agent: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7"/><path d="M18 14a6 6 0 0 1 3.5 6"/>',
  web: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  plan: '<path d="m4 6 2 2 3-3M4 13l2 2 3-3M13 7h7M13 14h7M4 20h16"/>',
  ask: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.7.3-1 .9-1 1.7M12 17h.01"/>',
  mcp: '<path d="M9 2v6M15 2v6M6 8h12v4a6 6 0 0 1-12 0zM12 18v4"/>',
  other: '<circle cx="5" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="19" cy="12" r="1.5"/>',
  thinking: '<path d="M7 18a4 4 0 0 1-.9-7.9A5.5 5.5 0 0 1 17 8.5a4.5 4.5 0 0 1 .5 9z"/><circle cx="5" cy="21.5" r=".6"/>',
  text: '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1.1-4.6A8 8 0 1 1 21 12z"/>',
  prompt: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  error: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01"/>',
  interrupt: '<rect x="5" y="5" width="14" height="14" rx="2"/>',
  command: '<path d="M16 3 8 21"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  branch: '<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="7" r="2"/><path d="M6 7v10M18 9a8 8 0 0 1-8 8H8"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M12 3a9 9 0 1 0 9 9 7 7 0 0 1-9-9z"/>',
};
const icon = (name) => `<svg viewBox="0 0 24 24">${ICONS[name] || ICONS.other}</svg>`;
const mark = () => '<svg viewBox="0 0 32 32"><use href="#mark"/></svg>';

const CAT_VAR = (cat) => `var(--c-${cat || "other"})`;
const KIND_CAT = { thinking: "search", text: "read", prompt: "edit", error: "other", interrupt: "other", command: "plan" };
const catOf = (a) => (a.kind === "tool" ? a.category || "other" : KIND_CAT[a.kind] || "other");

const STATUS = {
  thinking: "Thinking",
  tool: "Working",
  writing: "Writing",
  waiting: "Waiting for you",
  done: "Done",
  idle: "Idle",
  error: "Error",
  interrupted: "Interrupted",
  working: "Working",
};
const WORKING = new Set(["thinking", "tool", "writing"]);

const HUES = ["var(--c-search)", "var(--c-agent)", "var(--c-web)", "var(--c-plan)", "var(--c-read)", "var(--c-mcp)", "var(--c-ask)"];
const SOURCES = { antigravity: "Antigravity IDE", "antigravity-cli": "Antigravity CLI" };
const SOURCE_TAGS = { antigravity: "Antigravity", "antigravity-cli": "Antigravity CLI" };
const srcTag = (s) => (SOURCE_TAGS[s.source] ? `<span class="src">${SOURCE_TAGS[s.source]}</span>` : "");

function hueFor(agent) {
  if (agent.kind === "main") return SOURCES[agent.source] ? "var(--c-web)" : "var(--accent)";
  const t = agent.type || agent.name || "";
  if (t === "Explore") return "var(--c-search)";
  if (t === "general-purpose") return "var(--c-agent)";
  if (t === "Plan") return "var(--c-plan)";
  let h = 0;
  for (const ch of t) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return HUES[h % HUES.length];
}

function model(m) {
  if (!m) return "";
  const x = /^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-|$)/.exec(m);
  return x ? `${x[1]} ${x[2]}${x[3] ? "." + x[3] : ""}` : m;
}
function tokens(n) {
  if (!n) return "0";
  if (n < 1000) return String(n);
  if (n < 1e6) return (n / 1000).toFixed(n < 1e4 ? 1 : 0) + "k";
  return (n / 1e6).toFixed(1) + "M";
}
function dur(sec) {
  sec = Math.max(0, Math.floor(sec));
  if (sec < 60) return sec + "s";
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m}m ${String(sec % 60).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${String(m % 60).padStart(2, "0")}m`;
  return Math.floor(h / 24) + "d";
}
function ago(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 10) return "just now";
  return dur(s).replace(/ \d+s$/, "") + " ago";
}
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
function tilde(p) {
  const home = state.snap?.home;
  return home && p && p.startsWith(home) ? "~" + p.slice(home.length) : p || "";
}
const live = (ts) => `<span class="tick" data-since="${ts || 0}"></span>`;
const liveAgo = (ts) => `<span class="tick" data-ago="${ts || 0}"></span>`;

/** Keyed list patching: reuses DOM nodes so running animations don't restart. */
function patch(container, items, key, render, cls) {
  const old = new Map();
  for (const el of container.children) if (el.dataset.key) old.set(el.dataset.key, el);
  let prev = null;
  for (const item of items) {
    const k = key(item);
    let el = old.get(k);
    if (el) old.delete(k);
    else {
      el = document.createElement("div");
      el.dataset.key = k;
      el._enter = true;
      el.addEventListener("animationend", (e) => {
        if (e.target === el && el._enter) { el._enter = false; el.classList.remove("enter"); }
      });
    }
    const c = cls(item) + (el._enter ? " enter" : "");
    if (el.className !== c) el.className = c;
    const html = render(item);
    if (el._html !== html) { el.innerHTML = html; el._html = html; }
    const next = prev ? prev.nextSibling : container.firstChild;
    if (el !== next) container.insertBefore(el, next);
    prev = el;
  }
  for (const el of old.values()) el.remove();
}
function setHTML(el, html) {
  if (el && el._html !== html) { el.innerHTML = html; el._html = html; }
}

/* ----------------------------------------------------------------- stream */
function connect(session) {
  if (state.stream && state.streamFor === session) return;
  if (state.stream) state.stream.close();
  state.streamFor = session;
  const es = new EventSource("/api/stream" + (session ? "?session=" + encodeURIComponent(session) : ""));
  state.stream = es;
  es.onopen = () => setLive(true);
  es.onerror = () => setLive(false);
  es.onmessage = (ev) => {
    try { onSnapshot(JSON.parse(ev.data)); } catch (err) { console.error(err); }
  };
}
function setLive(on) {
  const el = $("#live");
  el.className = "live " + (on ? "on" : "off");
  el.querySelector("span").textContent = on ? "live" : "offline";
}

function onSnapshot(snap) {
  state.snap = snap;
  const sessions = snap.sessions;
  const current = sessions.find((s) => s.id === state.selected);
  if (state.follow) {
    const busy = sessions.filter((s) => s.status === "working");
    if (busy.length && (!current || current.status !== "working")) state.selected = busy[0].id;
  }
  if (!sessions.find((s) => s.id === state.selected)) state.selected = sessions[0]?.id ?? null;
  if (state.selected !== state.streamFor) connect(state.selected);
  render();
}

/* ----------------------------------------------------------------- render */
function render() {
  renderStats();
  renderSessions();
  renderMain();
  renderDrawer();
  tick();
}

function renderStats() {
  const ss = state.snap.sessions;
  const working = ss.reduce((n, s) => n + (s.working || 0), 0);
  const active = ss.filter((s) => s.status === "working").length;
  setHTML(
    $("#stats"),
    `<span class="${working ? "hot" : ""}"><b>${working}</b> agent${working === 1 ? "" : "s"} working</span>` +
      `<span><b>${active}</b> active session${active === 1 ? "" : "s"}</span>` +
      `<span><b>${ss.length}</b> total</span>`
  );
  document.title = working ? `(${working}) SpecOps Claude` : "SpecOps Claude";
}

function renderSessions() {
  const q = state.filter.toLowerCase();
  const list = state.snap.sessions.filter(
    (s) => !q || `${s.project} ${s.title} ${s.cwd} ${s.branch}`.toLowerCase().includes(q)
  );
  const active = list.filter((s) => s.status === "working");
  const rest = list.filter((s) => s.status !== "working");
  const items = [];
  if (active.length) items.push({ label: "Active now" }, ...active);
  if (rest.length) items.push({ label: active.length ? "Recent" : "Sessions" }, ...rest);
  if (!list.length) items.push({ label: q ? "No matches" : "No sessions yet" });

  patch(
    $("#sessions"),
    items,
    (s) => (s.label ? "label:" + s.label : s.id),
    (s) => {
      if (s.label) return esc(s.label);
      const ants = s.agents
        .slice(0, 28)
        .map((a) => `<i class="${a.status}${WORKING.has(a.status) ? " working" : ""}" title="${esc(a.name)}"></i>`)
        .join("");
      return `<span class="dot ${s.status}"></span>
        <span class="proj">${esc(s.project)}</span>
        <span class="when">${liveAgo(s.last_ts)}</span>
        <span class="title">${srcTag(s)}${esc(s.title)}</span>
        ${s.agent_count > 1 ? `<span class="ants">${ants}</span>` : ""}`;
    },
    (s) => (s.label ? "group-label" : "session" + (s.id === state.selected ? " selected" : ""))
  );
}

function sessionDetail() {
  return state.snap.sessions.find((s) => s.id === state.selected && s.detail);
}

function renderMain() {
  const main = $("#main");
  const s = state.snap.sessions.find((x) => x.id === state.selected);
  if (!s) {
    main.dataset.view = "empty";
    setHTML(
      main,
      `<div class="empty">${mark()}<h2>All quiet on the field</h2>
      <p>No Claude Code or Antigravity sessions found in the last hours under<br><code>${esc(state.snap.root)}</code></p>
      <p>Start <code>claude</code> or an Antigravity agent in any project and it shows up here live. Or try the demo:</p>
      <pre>specops web --demo</pre></div>`
    );
    return;
  }
  const d = sessionDetail();
  if (!d) return; // detail for the new selection is on its way

  if (main.dataset.view !== "session:" + d.id) {
    main.dataset.view = "session:" + d.id;
    main._html = null;
    main.innerHTML = `<div id="shead"></div>
      <div class="colony queen" id="queen"></div>
      <div id="wtitle"></div><div class="colony" id="workers"></div>
      <div id="ftitle"></div><div class="finished" id="finished"></div><div id="fmore"></div>`;
    main.scrollTop = 0;
  }

  const [queen, ...subs] = d.agents;
  const tok = d.agents.reduce((n, a) => n + (a.tokens?.out || 0), 0);
  const toolsUsed = d.agents.reduce((n, a) => n + (a.tool_count || 0), 0);
  setHTML(
    $("#shead"),
    `<div class="shead">
      <div style="min-width:0">
        <h1>${esc(d.project)}</h1>
        <div class="sub">${esc(d.title)}</div>
        <div class="meta">
          <button class="chip" data-copy="${esc(d.cwd)}" title="Copy path">${icon("folder")}<span class="mono">${esc(tilde(d.cwd))}</span></button>
          ${d.branch ? `<span class="chip">${icon("branch")}${esc(d.branch)}</span>` : ""}
          <span class="chip">${icon("clock")}started ${clock(d.started || d.last_ts)}</span>
          ${SOURCES[d.source] ? `<span class="chip src-chip">${esc(SOURCES[d.source])}</span>` : ""}
        </div>
      </div>
      <div class="totals">
        <div class="total"><b>${d.agents.length}</b><span>agents</span></div>
        <div class="total"><b>${toolsUsed}</b><span>tool calls</span></div>
        ${tok || !SOURCES[d.source] ? `<div class="total"><b>${tokens(tok)}</b><span>tokens out</span></div>` : ""}
      </div>
    </div>`
  );

  patch($("#queen"), [queen], (a) => a.id, (a) => card(a, true), (a) => cardClass(a) + " queen");

  const working = subs.filter((a) => !["done", "idle", "error", "interrupted"].includes(a.status));
  const finished = subs.filter((a) => !working.includes(a)).sort((a, b) => (b.last_ts || 0) - (a.last_ts || 0));

  setHTML($("#wtitle"), working.length ? sectionTitle("Working now", working.length) : "");
  patch($("#workers"), working, (a) => a.id, (a) => card(a, false), cardClass);

  const shown = state.showAllFinished ? finished : finished.slice(0, 8);
  setHTML($("#ftitle"), finished.length ? sectionTitle("Finished", finished.length) : "");
  patch($("#finished"), shown, (a) => a.id, row, (a) => "row " + a.status);
  setHTML(
    $("#fmore"),
    finished.length > shown.length
      ? `<button class="more-btn" data-more>Show ${finished.length - shown.length} more</button>`
      : ""
  );
}

const sectionTitle = (t, n) => `<div class="section-title">${esc(t)} <span class="count">${n}</span></div>`;
const cardClass = (a) => `card ${a.status}${WORKING.has(a.status) ? " working" : ""}`;

function pill(a) {
  return `<span class="pill" style="--tone:var(--s-${a.status})"><span class="dot"></span>${STATUS[a.status] || a.status}</span>`;
}

function nowLine(a) {
  const cur = a.current;
  let cat = "other", ic = "other", verb = "", target = "", since = a.last_ts;
  if (a.status === "tool" && cur && cur.kind === "tool") {
    cat = cur.category; ic = cur.category; verb = cur.verb; target = cur.target; since = cur.ts;
  } else if (a.status === "thinking") {
    cat = "search"; ic = "thinking"; verb = "Thinking"; target = "";
  } else if (a.status === "writing") {
    cat = "read"; ic = "text"; verb = "Writing a reply";
  } else if (a.status === "waiting") {
    cat = "web"; ic = "prompt"; verb = "Waiting for your next message";
  } else if (a.status === "done") {
    cat = "shell"; ic = "plan"; verb = "Finished";
    target = a.started && a.last_ts ? "in " + dur(a.last_ts - a.started) : "";
    since = 0;
  } else if (a.status === "interrupted") {
    cat = "other"; ic = "interrupt"; verb = "Interrupted";
  } else if (a.status === "error") {
    cat = "agent"; ic = "error"; verb = "Hit an error";
  } else {
    ic = "clock"; verb = "Idle";
  }
  const verbHtml = verb === "Thinking" ? `<span class="verb dots">Thinking</span>` : `<span class="verb">${esc(verb)}</span>`;
  return `<div class="now" style="--cat:${CAT_VAR(cat)}">
    <span class="ico">${icon(ic)}</span>${verbHtml}
    <span class="target" title="${esc(target)}">${esc(target)}</span>
    ${since ? `<span class="elapsed">${live(since)}</span>` : ""}
  </div>`;
}

function thought(a) {
  const t = a.thought;
  if (!t || !t.text) return "";
  const said = t.kind === "text";
  return `<div class="thought ${said ? "said" : ""}">
    <div class="lbl"><span>${said ? "Said" : "Thought"}</span><span>${liveAgo(t.ts)}</span></div>
    <div class="txt">${md(t.text.trim())}</div>
  </div>`;
}

function folder(a, limit) {
  const files = (a.files || []).slice(0, limit);
  const more = (a.file_count || 0) - files.length;
  return `<div class="folder">
    <div class="cwd" title="${esc(a.cwd)}">${icon("folder")}<span>&lrm;${esc(tilde(a.cwd))}&lrm;</span></div>
    ${files.length ? `<div class="files">${files
      .map((f) => `<span class="file ${f.writes ? "w" : ""}" title="${esc(f.path)}${f.writes ? " (edited)" : " (read)"}">${esc(f.rel.split(/[\\/]/).slice(-2).join("/"))}</span>`)
      .join("")}${more > 0 ? `<span class="file more">+${more}</span>` : ""}</div>` : ""}
  </div>`;
}

function todos(a) {
  const list = a.todos || [];
  if (!list.length) return "";
  const done = list.filter((t) => t.status === "completed").length;
  return `<div class="todos">
    <div class="todo-bar"><i style="width:${(done / list.length) * 100}%"></i></div>
    ${list
      .slice(0, 7)
      .map((t) => `<div class="todo ${esc(t.status)}"><span class="box"></span><span>${esc(t.status === "in_progress" && t.active ? t.active : t.content)}</span></div>`)
      .join("")}
  </div>`;
}

function trail(a) {
  const acts = (a.activities || []).filter((x) => x.kind === "tool" || x.text).slice(-32);
  return `<span class="trail">${acts
    .map((x) => {
      const h = x.kind === "tool" ? 16 : x.kind === "text" ? 11 : 7;
      return `<i class="${x.status === "error" ? "err" : ""}" style="height:${h}px;--cat:${CAT_VAR(catOf(x))}"></i>`;
    })
    .join("")}</span>`;
}

function avatar(a) {
  return `<span class="avatar" style="--hue:${hueFor(a)}">${mark()}</span>`;
}

function card(a, queen) {
  const desc = a.kind === "main" ? a.title || "Main agent" : a.description || a.task.split("\n")[0];
  const head = `<div class="card-head">${avatar(a)}
      <div class="who"><div class="name">${esc(a.name)}${a.model ? `<span class="model">${esc(model(a.model))}</span>` : ""}</div>
      <div class="desc" title="${esc(desc)}">${esc(desc)}</div></div>
      ${pill(a)}</div>`;
  const foot = `<div class="card-foot"><span>${a.tool_count} tools</span>${SOURCES[a.source] ? "" : `<span>${tokens(a.tokens?.out)} tok</span>`}${trail(a)}</div>`;
  if (queen) {
    return `${head}<div class="qgrid">
      <div>${nowLine(a)}${thought(a)}</div><div>${folder(a, 10)}${todos(a)}</div></div>${foot}`;
  }
  return `${head}${nowLine(a)}${thought(a)}${folder(a, 5)}${todos(a)}${foot}`;
}

function row(a) {
  const took = a.started && a.last_ts ? dur(a.last_ts - a.started) : "";
  return `${avatar(a)}<span class="name">${esc(a.name)}</span>
    <span class="desc" title="${esc(a.description)}">${esc(a.description || a.task.split("\n")[0])}</span>
    <span class="meta">${a.tool_count} tools · ${took}</span>
    <span class="meta">${liveAgo(a.last_ts)}</span>`;
}

/* ----------------------------------------------------------------- drawer */
function openAgent(id) {
  state.agent = id;
  state.tab = "timeline";
  const body = $("#drawer-body");
  body._html = null;
  body.innerHTML = "";
  renderDrawer(true);
}
function closeDrawer() {
  state.agent = null;
  const d = $("#drawer");
  d.classList.remove("open");
  d.setAttribute("aria-hidden", "true");
}

function renderDrawer(fresh = false) {
  if (!state.agent) return;
  const d = sessionDetail();
  const a = d?.agents.find((x) => x.id === state.agent);
  if (!a) { if (d) closeDrawer(); return; }
  const drawer = $("#drawer");
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  const body = $("#drawer-body");
  if (!body.firstChild || fresh || body.dataset.agent !== a.id) {
    body.dataset.agent = a.id;
    body.innerHTML = `<div class="dhead" id="dhead"></div><div class="dscroll" id="dscroll"><div class="timeline" id="tl"></div></div>`;
  }
  const parent = a.parent_id && a.parent_id !== a.session_id ? d.agents.find((x) => x.id === a.parent_id) : null;
  setHTML(
    $("#dhead"),
    `<div class="top">${avatar(a)}
      <div class="who"><div class="name">${esc(a.name)}${a.model ? `<span class="model">${esc(model(a.model))}</span>` : ""} ${pill(a)}</div>
      <div class="desc">${esc(a.kind === "main" ? d.title : a.description)}${parent ? ` · spawned by <span class="link" data-agent="${esc(parent.id)}">${esc(parent.name)}</span>` : ""}</div></div>
      <button class="icon-btn close" data-close title="Close (Esc)">${icon("close")}</button></div>
    <div class="shead" style="margin:12px 0 0"><div class="meta" style="margin:0">
      <button class="chip" data-copy="${esc(a.cwd)}" title="Copy path">${icon("folder")}<span class="mono">${esc(tilde(a.cwd))}</span></button>
      ${a.branch ? `<span class="chip">${icon("branch")}${esc(a.branch)}</span>` : ""}
      <span class="chip">${a.tool_count} tools${SOURCES[a.source] ? "" : ` · ${tokens(a.tokens.out)} out · ${tokens(a.tokens.in + a.tokens.cache)} in`}</span>
    </div></div>
    ${a.kind === "sub" && a.task ? `<div class="task">${esc(a.task)}</div>` : ""}
    <div class="tabs">${["timeline", "thoughts", "tools", "files"]
      .map((t) => `<button class="tab ${state.tab === t ? "on" : ""}" data-tab="${t}">${t[0].toUpperCase() + t.slice(1)}${t === "files" ? ` (${a.file_count})` : ""}</button>`)
      .join("")}</div>`
  );

  const scroller = $("#dscroll");
  const atBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 60;
  const tl = $("#tl");
  if (state.tab === "files") {
    tl._files = true;
    tl.innerHTML = `<div class="files-list">${a.files
      .map((f) => `<div class="f" title="${esc(f.path)}"><span>${esc(f.rel)}</span>${f.reads ? `<span class="badge r">R ${f.reads}</span>` : ""}${f.writes ? `<span class="badge w">W ${f.writes}</span>` : ""}</div>`)
      .join("") || '<p class="redacted">No files touched yet.</p>'}</div>`;
    return;
  }
  if (tl._files) { tl._files = false; tl.innerHTML = ""; }
  const acts = a.activities.filter((x, i, all) => {
    if (state.tab === "thoughts") return (x.kind === "thinking" || x.kind === "text") && x.text;
    if (state.tab === "tools") return x.kind === "tool";
    // hide empty "thinking" markers except a trailing one (the agent is thinking right now)
    return x.kind !== "thinking" || x.text || (i === all.length - 1 && a.status === "thinking");
  });
  patch(
    tl,
    acts,
    (x) => `${x.ts}:${x.kind}:${x.id || ""}:${(x.text || "").length}`,
    (x) => timelineItem(x, a),
    (x) => `tl ${x.kind}${x.status === "error" ? " error" : ""}`
  );
  if (fresh || atBottom) scroller.scrollTop = scroller.scrollHeight;
}

function timelineItem(x, agent) {
  const cat = catOf(x);
  const ic = x.kind === "tool" ? x.category : x.kind;
  let body = "";
  if (x.kind === "tool") {
    const d = x.ended ? dur(x.ended - x.ts) : "";
    const status = x.status === "running" ? live(x.ts) : x.status === "error" ? `<span class="status-error">failed ${d}</span>` : d;
    const spawn = x.agent_ref ? ` <span class="link" data-agent="${esc(x.agent_ref)}">open agent →</span>` : "";
    body = `<div class="line"><span class="verb">${esc(x.verb)}</span><span class="target" title="${esc(x.target)}">${esc(x.target)}</span>${spawn}<span class="dur">${status}</span></div>`;
    if (x.detail && x.detail !== x.target)
      body += `<details><summary>${x.tool === "Bash" ? "command" : x.category === "read" || x.category === "edit" ? "full path" : "input"}</summary><pre>${esc(x.detail)}</pre></details>`;
    if (x.result) body += `<details><summary>output</summary><pre>${esc(x.result)}</pre></details>`;
  } else if (x.kind === "thinking") {
    body = x.text
      ? `<div class="line"><span class="verb">Thought</span></div><div class="prose">${md(x.text)}</div>`
      : `<div class="line"><span class="verb dots">Thinking</span></div>`;
  } else if (x.kind === "text") {
    body = `<div class="line"><span class="verb">Said</span></div><div class="prose">${md(x.text)}</div>`;
  } else if (x.kind === "prompt") {
    body = `<div class="line"><span class="verb">${agent.kind === "main" ? "You" : "Task"}</span></div><div class="prose clamp">${esc(x.text)}</div>`;
  } else if (x.kind === "command") {
    body = `<div class="line"><span class="verb">Command</span><span class="target">${esc(x.text)}</span></div>`;
  } else {
    body = `<div class="line"><span class="verb">${x.kind === "error" ? "Error" : "Interrupted"}</span></div><div class="prose">${esc(x.text)}</div>`;
  }
  return `<span class="t">${clock(x.ts).slice(0, 8)}</span><span class="ic"><span style="--cat:${CAT_VAR(cat)}">${icon(ic)}</span></span><div class="tl-body">${body}</div>`;
}

/* ----------------------------------------------------------------- ticking clocks */
function tick() {
  const now = Date.now() / 1000;
  for (const el of document.querySelectorAll(".tick")) {
    if (el.dataset.since) el.textContent = +el.dataset.since ? dur(now - +el.dataset.since) : "";
    else if (el.dataset.ago) el.textContent = +el.dataset.ago ? ago(+el.dataset.ago) : "";
  }
}
setInterval(tick, 1000);

/* ----------------------------------------------------------------- events */
function select(id) {
  if (!id || id === state.selected) return;
  state.selected = id;
  state.showAllFinished = false;
  setFollow(false);
  closeDrawer();
  connect(id);
  render();
}
function setFollow(on) {
  state.follow = on;
  $("#follow").checked = on;
  try { localStorage.setItem("specops.follow", on ? "1" : "0"); } catch {}
}

document.addEventListener("click", (e) => {
  const t = e.target;
  const copy = t.closest("[data-copy]");
  if (copy) {
    navigator.clipboard?.writeText(copy.dataset.copy);
    copy.animate([{ transform: "scale(.96)" }, { transform: "none" }], 180);
    return;
  }
  const link = t.closest("[data-agent]");
  if (link) return openAgent(link.dataset.agent);
  if (t.closest("[data-close]")) return closeDrawer();
  if (t.closest("[data-more]")) { state.showAllFinished = true; return render(); }
  const tab = t.closest("[data-tab]");
  if (tab) {
    state.tab = tab.dataset.tab;
    $("#tl").innerHTML = "";
    return renderDrawer(true);
  }
  const s = t.closest(".session");
  if (s) return select(s.dataset.key);
  const c = t.closest(".card, .row");
  if (c && !t.closest("a, button, summary")) return openAgent(c.dataset.key);
});

$("#follow").checked = state.follow;
$("#follow").addEventListener("change", (e) => {
  setFollow(e.target.checked);
  if (state.snap) onSnapshot(state.snap);
});
$("#search").addEventListener("input", (e) => {
  state.filter = e.target.value;
  if (state.snap) renderSessions();
});

function effectiveTheme() {
  const t = document.documentElement.dataset.theme;
  if (t) return t;
  return matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}
function paintThemeButton() {
  $("#theme").innerHTML = icon(effectiveTheme() === "dark" ? "sun" : "moon");
}
function toggleTheme() {
  const next = effectiveTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("specops.theme", next); } catch {}
  paintThemeButton();
}
$("#theme").addEventListener("click", toggleTheme);
paintThemeButton();

document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, textarea")) {
    if (e.key === "Escape") e.target.blur();
    return;
  }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "Escape") closeDrawer();
  else if (e.key === "/") { e.preventDefault(); $("#search").focus(); }
  else if (e.key === "t") toggleTheme();
  else if (e.key === "f") { setFollow(!state.follow); if (state.snap) onSnapshot(state.snap); }
  else if ((e.key === "j" || e.key === "k" || e.key === "ArrowDown" || e.key === "ArrowUp") && state.snap) {
    const ids = [...document.querySelectorAll(".session")].map((el) => el.dataset.key);
    const i = ids.indexOf(state.selected);
    const next = ids[Math.min(ids.length - 1, Math.max(0, i + (e.key === "j" || e.key === "ArrowDown" ? 1 : -1)))];
    if (next) { e.preventDefault(); select(next); }
  }
});

connect(null);
