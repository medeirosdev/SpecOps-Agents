/* Skills tab: one skill library, published to Claude Code and Antigravity, globally or per project.
   Reads are open; writes need the unlock code from the terminal (see web/auth.py). Uses the helpers
   of app.js ($, esc, icon, tilde, clock). */
"use strict";

const SK_TOKEN_KEY = "specops.skills.token";
const AGENTS = { claude: "Claude Code", antigravity: "Antigravity" };
const STATUS_TEXT = {
  on: "Published",
  stale: "Out of date",
  modified: "Edited outside SpecOps",
  conflict: "Name taken by another skill",
  off: "Not published",
};
const LOCK_SVG = {
  locked: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
  open: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.5-2"/>',
};
const lockIcon = (k) => `<svg viewBox="0 0 24 24">${LOCK_SVG[k]}</svg>`;

const sk = {
  data: null,
  selected: null, // skill name, "" for a new one, null for none
  token: null,
  project: "",
  notice: null, // {kind: "ok" | "error", text}
  expiresAt: 0,
  loading: false,
};
try { sk.token = sessionStorage.getItem(SK_TOKEN_KEY); } catch {}

function setToken(token, expiresIn) {
  sk.token = token;
  sk.expiresAt = token ? Date.now() + (expiresIn || 0) * 1000 : 0;
  try {
    if (token) sessionStorage.setItem(SK_TOKEN_KEY, token);
    else sessionStorage.removeItem(SK_TOKEN_KEY);
  } catch {}
}

async function api(path, body) {
  const headers = sk.token ? { Authorization: "Bearer " + sk.token } : {};
  const opts = { headers, cache: "no-store", credentials: "same-origin" };
  if (body !== undefined) {
    opts.method = "POST";
    headers["Content-Type"] = "application/json";
    headers["X-SpecOps"] = "1";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch {}
  if (!res.ok) {
    if (res.status === 401 && path !== "/api/auth/pair") setToken(null);
    const err = new Error((data && data.error) || res.statusText || "request failed");
    err.status = res.status;
    throw err;
  }
  return data;
}

const unlocked = () => !!(sk.data && sk.data.writable && sk.data.auth && sk.data.auth.unlocked && sk.token);

async function loadSkills() {
  if (sk.loading) return;
  sk.loading = true;
  try {
    sk.data = await api("/api/skills");
    if (sk.data.auth && sk.data.auth.unlocked) sk.expiresAt = Date.now() + sk.data.auth.expires_in * 1000;
    else if (sk.token) setToken(null);
  } catch (err) {
    sk.notice = { kind: "error", text: err.message };
  } finally {
    sk.loading = false;
  }
  renderSkills();
}

/* ----------------------------------------------------------------- views */
function setView(view) {
  document.body.dataset.view = view;
  $("#agents-view").hidden = view !== "agents";
  $("#skills-view").hidden = view !== "skills";
  $("#stats").hidden = view !== "agents";
  $(".follow").hidden = view !== "agents";
  for (const a of document.querySelectorAll(".views a")) {
    if (a.dataset.view === view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  if (view === "skills") loadSkills();
}
const viewFromHash = () => (location.hash === "#skills" ? "skills" : "agents");
window.addEventListener("hashchange", () => setView(viewFromHash()));

/* ----------------------------------------------------------------- render */
function skill(name) {
  return sk.data && sk.data.library.find((s) => s.name === name);
}

function renderSkills() {
  if (!sk.data) return;
  renderSkillList();
  renderSkillMain();
}

function dots(s) {
  return s.targets
    .filter((t) => t.status !== "off")
    .map((t) => `<i class="pub ${t.status}" title="${esc(AGENTS[t.agent])} · ${esc(t.scope === "global" ? "global" : tilde(t.project))} · ${esc(STATUS_TEXT[t.status])}"></i>`)
    .join("");
}

function renderSkillList() {
  const d = sk.data;
  const rows = [];
  rows.push(`<div class="group-label">Library</div>`);
  if (!d.library.length) rows.push(`<p class="sk-empty-list">No skills yet.</p>`);
  for (const s of d.library) {
    rows.push(`<button type="button" class="session sk-item${s.name === sk.selected ? " selected" : ""}" data-skill="${esc(s.name)}">
      <span class="dot ${s.targets.some((t) => t.status === "on") ? "done" : "idle"}"></span>
      <span class="proj">${esc(s.name)}</span><span class="when">${dots(s)}</span>
      <span class="title">${esc(s.description)}</span></button>`);
  }
  if (d.external.length) {
    rows.push(`<div class="group-label">In agent folders</div>`);
    for (const x of d.external) {
      const can = unlocked() && x.importable && !skill(x.name);
      rows.push(`<div class="session sk-ext">
        <span class="dot idle"></span><span class="proj">${esc(x.name)}</span>
        <span class="when">${esc(AGENTS[x.agent])}</span>
        <span class="title">${esc(x.description)}</span>
        ${can ? `<button type="button" class="btn small" data-import="${esc(x.name)}" data-target="${esc(x.target)}">Import to library</button>` : ""}
      </div>`);
    }
  }
  setHTML($("#sk-list"), rows.join(""));
}

function lockChip() {
  const d = sk.data;
  if (!d.writable) return `<span class="lock ro">${lockIcon("locked")}Read-only${d.reason ? ` · ${esc(d.reason)}` : ""}</span>`;
  if (!unlocked()) return `<button type="button" class="lock locked" data-unlock>${lockIcon("locked")}Locked · unlock to edit</button>`;
  const mins = Math.max(1, Math.round((sk.expiresAt - Date.now()) / 60000));
  return `<span class="lock open">${lockIcon("open")}Unlocked · locks in ${mins} min idle</span>
    <button type="button" class="btn small" data-lock>Lock now</button>`;
}

function notice() {
  if (!sk.notice) return "";
  return `<div class="notice ${sk.notice.kind}" role="status">${esc(sk.notice.text)}</div>`;
}

function renderSkillMain() {
  const main = $("#sk-main");
  const d = sk.data;
  const head = `<div class="sk-head"><div class="sk-lock">${lockChip()}</div></div>${notice()}`;

  if (sk.selected === null) {
    main.dataset.view = "";
    setHTML(main, `${head}
      <div class="sk-intro">
        <h1>Skills</h1>
        <p>A skill is a folder with a <code>SKILL.md</code>: a name, a description that tells agents
        when to use it, and instructions. Claude Code and Antigravity read the same format, so one
        skill written here works for both.</p>
        <table class="sk-where">
          <tr><th></th><th>Everywhere</th><th>One project</th></tr>
          <tr><td>Claude Code</td><td><code>~/.claude/skills/</code></td><td><code>&lt;project&gt;/.claude/skills/</code></td></tr>
          <tr><td>Antigravity</td><td><code>~/.gemini/config/skills/</code></td><td><code>&lt;project&gt;/.agents/skills/</code></td></tr>
        </table>
        <p>Skills are kept in <code>${esc(tilde(d.home))}/skills</code> and copied where you publish
        them. Copies are marked, so SpecOps never touches a skill it didn't publish, and editing a
        skill here updates every copy. Agents load only the description until a skill is needed;
        sessions that are already running may need a restart to see a new one.</p>
      </div>
      ${auditHTML()}`);
    return;
  }

  const s = sk.selected ? skill(sk.selected) : null;
  if (sk.selected && !s) { sk.selected = null; return renderSkillMain(); }
  const key = "skill:" + (s ? s.name + ":" + s.hash : "new");
  const ro = !unlocked();
  if (main.dataset.view !== key || main.dataset.ro !== String(ro)) {
    // Rebuild the form only when the skill (or its saved version) or the lock changes, so
    // typing isn't lost to a refresh.
    const keep = main.dataset.view === key ? readForm() : null;
    main.dataset.view = key;
    main.dataset.ro = String(ro);
    main._html = null;
    main.innerHTML = `<div id="sk-top"></div>
      <section class="sk-editor">
        <label>Name<input id="sk-name" maxlength="64" spellcheck="false" autocomplete="off"
          placeholder="e.g. release-notes" ${s ? "disabled" : ""}></label>
        <label>Description <span class="hint">when should an agent use it?</span>
          <textarea id="sk-desc" rows="3" maxlength="1024"></textarea></label>
        <label>Instructions <span class="hint">markdown</span>
          <textarea id="sk-body" rows="18" spellcheck="false"></textarea></label>
        <div class="sk-actions">
          <button class="btn primary" type="button" id="sk-save">${s ? "Save" : "Create skill"}</button>
          ${s ? `<button class="btn danger" type="button" id="sk-delete">Delete</button>` : ""}
          <button class="btn" type="button" id="sk-cancel">${s ? "Revert" : "Cancel"}</button>
        </div>
      </section>
      <div id="sk-publish"></div>
      <div id="sk-audit"></div>`;
    const src = keep || (s ? { name: s.name, description: s.description, body: s.body } : { name: "", description: "", body: "" });
    $("#sk-name").value = src.name;
    $("#sk-desc").value = src.description;
    $("#sk-body").value = src.body;
    for (const el of main.querySelectorAll(".sk-editor input, .sk-editor textarea, #sk-save, #sk-delete")) {
      if (ro && !(el.id === "sk-name" && s)) el.disabled = true;
    }
  }
  setHTML($("#sk-top"), `${head}<h1 class="sk-title">${s ? esc(s.name) : "New skill"}</h1>
    ${s ? `<div class="sub">Last saved ${clock(s.updated)}</div>` : ""}`);
  setHTML($("#sk-publish"), s ? publishHTML(s, ro) : "");
  setHTML($("#sk-audit"), auditHTML(s && s.name));
}

function targetRow(s, t, ro) {
  const where = t.scope === "global" ? tilde(t.root) : tilde(t.root);
  const on = t.status === "on" || t.status === "stale" || t.status === "modified";
  const act = t.status === "conflict" ? "" :
    `<button type="button" class="btn small ${on ? "" : "primary"}" ${ro ? "disabled" : ""}
      data-publish="${esc(t.key)}" data-on="${on ? "0" : "1"}" data-status="${t.status}">${on ? "Remove" : "Publish"}</button>`;
  return `<div class="pub-row">
    <span class="pub-agent">${esc(AGENTS[t.agent])}</span>
    <span class="pub-where mono">${esc(where)}/${esc(s.name)}</span>
    <span class="pub-status ${t.status}">${esc(STATUS_TEXT[t.status])}</span>
    ${act}</div>`;
}

function publishHTML(s, ro) {
  const d = sk.data;
  const global = s.targets.filter((t) => t.scope === "global");
  const projects = new Set(s.targets.filter((t) => t.scope === "project").map((t) => t.project));
  if (sk.project && d.projects.includes(sk.project)) projects.add(sk.project);
  const projectRows = [...projects].sort().map((p) => {
    const rows = ["claude", "antigravity"].map((agent) => {
      const key = `project:${agent}:${p}`;
      const t = s.targets.find((x) => x.key === key) || {
        key, agent, scope: "project", project: p, status: "off",
        root: agent === "claude" ? p + "/.claude/skills" : p + "/.agents/skills",
      };
      return targetRow(s, t, ro);
    });
    return `<div class="pub-project"><div class="pub-project-name">${icon("folder")}<span class="mono">${esc(tilde(p))}</span></div>${rows.join("")}</div>`;
  });
  const options = d.projects.filter((p) => !projects.has(p))
    .map((p) => `<option value="${esc(p)}">${esc(tilde(p))}</option>`).join("");
  return `<section class="sk-section">
      <h2>Everywhere</h2>
      <p class="hint">Every session of that agent, in any folder.</p>
      ${global.map((t) => targetRow(s, t, ro)).join("")}
    </section>
    <section class="sk-section">
      <h2>Per project</h2>
      <p class="hint">Only sessions working in that folder. Projects are the folders where SpecOps
        has seen an agent session.</p>
      ${projectRows.join("") || `<p class="hint">Not published to any project.</p>`}
      ${options ? `<div class="pub-add"><select id="sk-project" ${ro ? "disabled" : ""}>
        <option value="">Choose a project…</option>${options}</select></div>` : ""}
    </section>`;
}

const ACTIONS = {
  create: "created", update: "updated", delete: "deleted", publish: "published",
  unpublish: "unpublished", import: "imported",
};
function auditHTML(name) {
  const rows = (sk.data.audit || []).filter((a) => !name || a.skill === name).slice(0, 12);
  if (!rows.length) return "";
  return `<section class="sk-section sk-log"><h2>Activity</h2>${rows.map((a) => `
    <div class="log-row"><span class="mono">${clock(a.ts)}</span>
      <span><b>${esc(a.skill)}</b> ${esc(ACTIONS[a.action] || a.action)}${a.target ? ` → ${esc(targetLabel(a.target))}` : ""}${a.synced && a.synced.length ? ` · ${a.synced.length} cop${a.synced.length === 1 ? "y" : "ies"} synced` : ""}</span>
      <span class="muted">${esc(a.actor || "")}</span></div>`).join("")}</section>`;
}
function targetLabel(key) {
  const [scope, agent, ...rest] = key.split(":");
  return `${AGENTS[agent] || agent} (${scope === "global" ? "global" : tilde(rest.join(":"))})`;
}

/* ----------------------------------------------------------------- actions */
function readForm() {
  return {
    name: ($("#sk-name") || {}).value || "",
    description: ($("#sk-desc") || {}).value || "",
    body: ($("#sk-body") || {}).value || "",
  };
}

function dirty() {
  if (sk.selected === null || !$("#sk-body")) return false;
  const f = readForm();
  const s = sk.selected ? skill(sk.selected) : { name: "", description: "", body: "" };
  return !s || f.description !== s.description || f.body !== s.body || (!sk.selected && f.name !== "");
}

function choose(name) {
  if (name !== sk.selected && dirty() && !confirm("Discard your unsaved changes?")) return;
  sk.selected = name;
  sk.notice = null;
  sk.project = "";
  renderSkills();
}

async function run(label, fn) {
  try {
    const out = await fn();
    sk.notice = label ? { kind: "ok", text: label } : null;
    return out;
  } catch (err) {
    sk.notice = { kind: "error", text: err.message };
    if (err.status === 401) sk.notice.text = "Editing locked itself. Unlock again to continue.";
    return null;
  } finally {
    await loadSkills();
  }
}

async function save() {
  const f = readForm();
  const creating = !sk.selected;
  const out = await run(creating ? "Skill created." : "Saved. Published copies were updated.", () =>
    api(creating ? "/api/skills/create" : "/api/skills/update", { ...f, name: creating ? f.name.trim() : sk.selected }));
  if (out && creating) { sk.selected = out.name; renderSkills(); }
}

async function remove() {
  const s = skill(sk.selected);
  const copies = s.targets.filter((t) => t.status !== "off" && t.status !== "conflict").length;
  if (!confirm(`Delete "${s.name}"${copies ? ` and its ${copies} published cop${copies === 1 ? "y" : "ies"}` : ""}? This can't be undone.`)) return;
  const edited = s.targets.some((t) => t.status === "modified");
  if (edited && !confirm("A published copy was edited outside SpecOps. Delete it anyway?")) return;
  const out = await run("Skill deleted.", () => api("/api/skills/delete", { name: s.name, force: edited }));
  if (out) { sk.selected = null; renderSkills(); }
}

async function publish(btn) {
  const on = btn.dataset.on === "1";
  let force = false;
  if (btn.dataset.status === "modified") {
    if (!confirm(`This copy was edited outside SpecOps. ${on ? "Overwrite" : "Delete"} those edits?`)) return;
    force = true;
  }
  await run(on ? "Published." : "Removed.", () =>
    api("/api/skills/publish", { name: sk.selected, target: btn.dataset.publish, on, force }));
}

/* ----------------------------------------------------------------- unlock */
function openUnlock() {
  const modal = $("#sk-unlock");
  modal.hidden = false;
  $("#sk-unlock-error").textContent = "";
  $("#sk-code").value = "";
  $("#sk-code").focus();
}
function closeUnlock() { $("#sk-unlock").hidden = true; }

async function unlock() {
  const code = $("#sk-code").value;
  const btn = $("#sk-unlock-go");
  btn.disabled = true;
  try {
    const out = await api("/api/auth/pair", { code });
    setToken(out.token, out.expires_in);
    closeUnlock();
    sk.notice = { kind: "ok", text: "Unlocked. A new code was printed in the terminal for next time." };
    await loadSkills();
  } catch (err) {
    $("#sk-unlock-error").textContent = err.message;
    $("#sk-code").select();
  } finally {
    btn.disabled = false;
  }
}

async function lockNow() {
  try { await api("/api/auth/lock", {}); } catch {}
  setToken(null);
  sk.notice = { kind: "ok", text: "Locked." };
  await loadSkills();
}

/* ----------------------------------------------------------------- events */
$("#skills-view").addEventListener("click", (e) => {
  const t = e.target;
  const item = t.closest("[data-skill]");
  if (item) return choose(item.dataset.skill);
  if (t.closest("#sk-new")) return choose("");
  if (t.closest("[data-unlock]")) return openUnlock();
  if (t.closest("[data-lock]")) return lockNow();
  if (t.closest("#sk-save")) return save();
  if (t.closest("#sk-delete")) return remove();
  if (t.closest("#sk-cancel")) {
    if (dirty() && !confirm("Discard your unsaved changes?")) return;
    $("#sk-main").dataset.view = "";
    if (!sk.selected) sk.selected = null;
    return renderSkills();
  }
  const pub = t.closest("[data-publish]");
  if (pub) return publish(pub);
  const imp = t.closest("[data-import]");
  if (imp) {
    return run("Imported into the library.", () =>
      api("/api/skills/import", { name: imp.dataset.import, target: imp.dataset.target }));
  }
});
$("#skills-view").addEventListener("change", (e) => {
  if (e.target.id === "sk-project") { sk.project = e.target.value; renderSkillMain(); }
});
$("#sk-unlock").addEventListener("click", (e) => {
  if (e.target.closest("[data-unlock-close]")) closeUnlock();
  if (e.target.closest("#sk-unlock-go")) unlock();
});
$("#sk-code").addEventListener("keydown", (e) => {
  if (e.key === "Enter") unlock();
  if (e.key === "Escape") closeUnlock();
});
window.addEventListener("beforeunload", (e) => {
  if (dirty()) e.preventDefault();
});
window.addEventListener("focus", () => { if (document.body.dataset.view === "skills") loadSkills(); });
setInterval(() => {
  if (document.body.dataset.view !== "skills" || !sk.data) return;
  if (sk.token && Date.now() > sk.expiresAt) { setToken(null); loadSkills(); }
  else if (sk.selected !== null || sk.token) setHTML($(".sk-lock"), lockChip());
}, 30000);

setView(viewFromHash());
