/* Profiles tab: rules, profiles (agents built from a prompt, rules, skills and a model) and teams
   (a lead agent that delegates to profiles), published as agent files for Claude Code and
   Antigravity. Shares the unlock token and helpers of skills.js and app.js. */
"use strict";

const KIND_LABEL = { profile: "Profile", team: "Team", rule: "Rule" };
const MODELS = {
  claude: ["", "haiku", "sonnet", "opus", "inherit"],
  antigravity: ["", "flash", "pro", "inherit"],
};

const pr = {
  data: null,
  sel: null, // {kind, name}; name "" for a new item
  project: "",
  notice: null,
  loading: false,
};

async function loadStudio() {
  if (pr.loading) return;
  pr.loading = true;
  try {
    pr.data = await api("/api/studio");
  } catch (err) {
    pr.notice = { kind: "error", text: err.message };
  } finally {
    pr.loading = false;
  }
  renderStudio();
}
VIEWS.profiles = () => { loadSkills(); loadStudio(); };
SK_AFTER.push(() => { if (document.body.dataset.view === "profiles") renderStudio(); });

const prItems = (kind) => (pr.data ? pr.data[kind + "s"] : []) || [];
const find = (kind, name) => prItems(kind).find((x) => x.name === name);

/* ----------------------------------------------------------------- render */
function renderStudio() {
  if (!pr.data || !sk.data) return;
  renderStudioList();
  renderStudioMain();
}

function renderStudioList() {
  const rows = [];
  for (const kind of ["profile", "team", "rule"]) {
    rows.push(`<div class="group-label">${KIND_LABEL[kind]}s</div>`);
    const list = prItems(kind);
    if (!list.length) rows.push(`<p class="sk-empty-list">No ${kind}s yet.</p>`);
    for (const x of list) {
      const on = (x.targets || []).some((t) => t.status === "on");
      const meta = kind === "team" ? `${x.members.length} member${x.members.length === 1 ? "" : "s"}`
        : kind === "rule" ? (x.used_by.length ? `in ${x.used_by.length}` : "unused")
        : dots(x);
      const selected = pr.sel && pr.sel.kind === kind && pr.sel.name === x.name;
      rows.push(`<button type="button" class="session sk-item${selected ? " selected" : ""}" data-pick="${kind}" data-name="${esc(x.name)}">
        <span class="dot ${kind === "rule" ? (x.used_by.length ? "done" : "idle") : on ? "done" : "idle"}"></span>
        <span class="proj">${esc(x.name)}</span><span class="when">${meta}</span>
        <span class="title">${esc(x.description)}</span></button>`);
    }
  }
  setHTML($("#pr-list"), rows.join(""));
}

function prNotice() {
  return pr.notice ? `<div class="notice ${pr.notice.kind}" role="status">${esc(pr.notice.text)}</div>` : "";
}

function renderStudioMain() {
  const main = $("#pr-main");
  const head = `<div class="sk-head"><div class="sk-lock">${lockChip()}</div></div>${prNotice()}`;
  if (!pr.sel) {
    main.dataset.view = "";
    setHTML(main, head + introHTML());
    return;
  }
  const { kind, name } = pr.sel;
  const x = name ? find(kind, name) : null;
  if (name && !x) { pr.sel = null; return renderStudioMain(); }
  const ro = !unlocked();
  const key = `${kind}:${x ? x.name + ":" + x.updated : "new"}`;
  if (main.dataset.view !== key || main.dataset.ro !== String(ro)) {
    // Rebuild the form only when the item (or its saved version) or the lock changes.
    const keep = main.dataset.view === key ? readItem() : null;
    main.dataset.view = key;
    main.dataset.ro = String(ro);
    main._html = null;
    main.innerHTML = `<div id="pr-top"></div><section class="sk-editor" id="pr-form">${formHTML(kind, !!x)}</section>
      <div id="pr-publish"></div><div id="pr-audit"></div>`;
    fillForm(keep || x || blank(kind));
    for (const el of main.querySelectorAll("#pr-form input, #pr-form textarea, #pr-form select, #pr-save, #pr-delete")) {
      if (ro && !(el.id === "pr-name" && x)) el.disabled = true;
    }
  }
  setHTML($("#pr-top"), `${head}<div class="pr-kind">${KIND_LABEL[kind]}</div>
    <h1 class="sk-title">${x ? esc(x.name) : `New ${kind}`}</h1>
    ${x ? `<div class="sub">Last saved ${clock(x.updated)}${usedBy(kind, x)}</div>` : ""}`);
  setHTML($("#pr-publish"), x && kind !== "rule" ? studioPublishHTML(kind, x, ro) : "");
  setHTML($("#pr-audit"), studioAuditHTML(kind, x && x.name));
}

function usedBy(kind, x) {
  if (!x.used_by || !x.used_by.length) return kind === "rule" ? " · not used by any profile" : "";
  const what = kind === "rule" ? "profile" : "team";
  return ` · used by ${what}${x.used_by.length === 1 ? "" : "s"} ${x.used_by.map((n) => `<span class="link" data-pick="${what}" data-name="${esc(n)}">${esc(n)}</span>`).join(", ")}`;
}

function introHTML() {
  const have = new Set([...prItems("profile"), ...prItems("team")].map((x) => x.name));
  const missing = (pr.data.starters || []).filter((n) => !have.has(n));
  return `<div class="sk-intro">
      <h1>Profiles &amp; teams</h1>
      <p>A <b>rule</b> is a standing instruction. A <b>profile</b> is an agent: a prompt, the rules
      it follows, the skills it uses and the model it runs on. A <b>team</b> is a lead agent that
      hands work to member profiles.</p>
      <p>Publishing writes an agent definition that both CLIs read, beside the skills folder:</p>
      <table class="sk-where">
        <tr><th></th><th>Everywhere</th><th>One project</th></tr>
        <tr><td>Claude Code</td><td><code>~/.claude/agents/</code></td><td><code>&lt;project&gt;/.claude/agents/</code></td></tr>
        <tr><td>Antigravity</td><td><code>~/.gemini/config/agents/</code></td><td><code>&lt;project&gt;/.agents/agents/</code></td></tr>
      </table>
      <p>Rules are written into each profile's prompt; a profile's skills and a team's members are
      published with it. Changing a rule updates every published profile that follows it. Agents
      started from a profile show up under its name in <a href="#metrics">Metrics</a>.</p>
      ${missing.length ? `<div class="starter">
        <div><b>Starter teams</b>
        <p><b>research</b> (web and code researchers, a report writer and a fact-checker),
        <b>dev-team</b> (architect, implementer, test engineer, code reviewer, docs writer) and
        <b>usage-review</b> (light agents that read <code>specops metrics</code> and propose cheaper,
        leaner profiles). ${missing.length} of them ${missing.length === 1 ? "is" : "are"} missing from your library.</p></div>
        <button class="btn primary" type="button" data-starters ${unlocked() ? "" : "disabled"}>Add ${missing.length === 1 ? esc(missing[0]) : "the missing ones"}</button>
      </div>` : ""}
    </div>${studioAuditHTML(null, null)}`;
}

function blank(kind) {
  return { name: "", description: "", body: "", prompt: "", model: { claude: "", antigravity: "" }, rules: [], skills: [], tools: [], members: [] };
}

function modelField(cli) {
  const label = cli === "claude" ? "Claude Code model" : "Antigravity model";
  return `<label>${label} <span class="hint">empty: the session's model</span>
    <input id="pr-model-${cli}" list="pr-models-${cli}" maxlength="80" spellcheck="false" autocomplete="off" placeholder="inherit">
    <datalist id="pr-models-${cli}">${MODELS[cli].filter(Boolean).map((m) => `<option value="${m}">`).join("")}</datalist></label>`;
}

function pickList(id, label, hint, options, empty) {
  const boxes = options.map((o) => `<label class="pick-item"><input type="checkbox" value="${esc(o.name)}">
    <span><b>${esc(o.name)}</b><span class="hint">${esc(o.description)}</span></span></label>`).join("");
  return `<fieldset class="pick" id="${id}"><legend>${label} <span class="hint">${hint}</span></legend>
    ${boxes || `<p class="hint">${empty}</p>`}</fieldset>`;
}

function formHTML(kind, existing) {
  const name = `<label>Name<input id="pr-name" maxlength="64" spellcheck="false" autocomplete="off"
    placeholder="e.g. ${kind === "rule" ? "answer-in-portuguese" : kind === "team" ? "release-crew" : "code-reviewer"}" ${existing ? "disabled" : ""}></label>`;
  const desc = `<label>Description <span class="hint">${kind === "rule" ? "what it's about" : "when should it be used? Agents pick by this"}</span>
    <textarea id="pr-desc" rows="2" maxlength="1024"></textarea></label>`;
  const actions = `<div class="sk-actions">
    <button class="btn primary" type="button" id="pr-save">${existing ? "Save" : `Create ${kind}`}</button>
    ${existing ? `<button class="btn danger" type="button" id="pr-delete">Delete</button>` : ""}
    <button class="btn" type="button" id="pr-cancel">${existing ? "Revert" : "Cancel"}</button></div>`;
  if (kind === "rule") {
    return `${name}${desc}<label>Rule <span class="hint">markdown, written into every profile that follows it</span>
      <textarea id="pr-body" rows="10" spellcheck="false"></textarea></label>${actions}`;
  }
  const prompt = `<label>${kind === "team" ? "Lead instructions" : "Prompt"} <span class="hint">${kind === "team" ? "how the lead runs the team; the member list is added for you" : "markdown: who this agent is and how it works"}</span>
    <textarea id="pr-prompt" rows="${kind === "team" ? 6 : 12}" spellcheck="false"></textarea></label>`;
  const models = `<div class="pr-row">${modelField("claude")}${modelField("antigravity")}</div>`;
  if (kind === "team") {
    return `${name}${desc}${prompt}${models}
      ${pickList("pr-members", "Members", "profiles the lead can hand work to", prItems("profile"), "Create a profile first.")}${actions}`;
  }
  return `${name}${desc}${prompt}${models}
    <label>Tools <span class="hint">Claude Code only, comma-separated, e.g. Read, Grep, Bash(git log:*). Empty: all tools</span>
      <input id="pr-tools" spellcheck="false" autocomplete="off"></label>
    ${pickList("pr-rules", "Rules", "written into the prompt", prItems("rule"), "No rules yet: create one with New rule.")}
    ${pickList("pr-skills", "Skills", "published with the profile", sk.data.library, "No skills in the library yet.")}${actions}`;
}

function checked(id) {
  return [...document.querySelectorAll(`#${id} input:checked`)].map((i) => i.value);
}
function setChecked(id, values) {
  for (const i of document.querySelectorAll(`#${id} input`)) i.checked = values.includes(i.value);
}

function fillForm(x) {
  const set = (id, v) => { const el = $("#" + id); if (el) el.value = v || ""; };
  set("pr-name", x.name);
  set("pr-desc", x.description);
  set("pr-body", x.body);
  set("pr-prompt", x.prompt);
  set("pr-model-claude", x.model && x.model.claude);
  set("pr-model-antigravity", x.model && x.model.antigravity);
  set("pr-tools", (x.tools || []).join(", "));
  setChecked("pr-rules", x.rules || []);
  setChecked("pr-skills", x.skills || []);
  setChecked("pr-members", x.members || []);
}

function readItem() {
  const val = (id) => ($("#" + id) || {}).value || "";
  const kind = pr.sel && pr.sel.kind;
  const item = { name: pr.sel && pr.sel.name ? pr.sel.name : val("pr-name").trim(), description: val("pr-desc") };
  if (kind === "rule") return { ...item, body: val("pr-body") };
  item.prompt = val("pr-prompt");
  item.model = { claude: val("pr-model-claude").trim(), antigravity: val("pr-model-antigravity").trim() };
  if (kind === "team") return { ...item, members: checked("pr-members") };
  // Split on commas outside parentheses: Bash(git log:*) keeps its own.
  const tools = val("pr-tools").split(/,(?![^(]*\))/).map((t) => t.trim()).filter(Boolean);
  return { ...item, tools, rules: checked("pr-rules"), skills: checked("pr-skills") };
}

/** An item reduced to what the form edits, in a stable order, to compare saved and typed. */
function normalize(kind, x) {
  const base = { name: x.name || "", description: x.description || "" };
  if (kind === "rule") return { ...base, body: x.body || "" };
  const m = x.model || {};
  const out = { ...base, prompt: x.prompt || "", model: { claude: m.claude || "", antigravity: m.antigravity || "" } };
  if (kind === "team") return { ...out, members: [...(x.members || [])].sort() };
  return { ...out, tools: x.tools || [], rules: [...(x.rules || [])].sort(), skills: [...(x.skills || [])].sort() };
}

function prDirty() {
  if (!pr.sel || !$("#pr-form")) return false;
  const typed = readItem();
  if (!pr.sel.name) return !!(typed.name || typed.description);
  const saved = find(pr.sel.kind, pr.sel.name);
  return !!saved && JSON.stringify(normalize(pr.sel.kind, typed)) !== JSON.stringify(normalize(pr.sel.kind, saved));
}

function studioRow(kind, x, t, ro) {
  const on = ["on", "stale", "modified"].includes(t.status);
  const act = t.status === "conflict" ? "" :
    `<button type="button" class="btn small ${on ? "" : "primary"}" ${ro ? "disabled" : ""}
      data-pr-publish="${esc(t.key)}" data-on="${on ? "0" : "1"}" data-status="${t.status}">${on ? "Remove" : "Publish"}</button>`;
  return `<div class="pub-row">
    <span class="pub-agent">${esc(AGENTS[t.agent])}</span>
    <span class="pub-where mono">${esc(tilde(t.root))}/${esc(x.name)}.md</span>
    <span class="pub-status ${t.status}">${esc(t.status === "conflict" ? "Name taken by another agent" : STATUS_TEXT[t.status])}</span>
    ${act}</div>`;
}

function studioPublishHTML(kind, x, ro) {
  const projects = new Set(x.targets.filter((t) => t.scope === "project").map((t) => t.project));
  if (pr.project && sk.data.projects.includes(pr.project)) projects.add(pr.project);
  const projectRows = [...projects].sort().map((p) => {
    const rows = ["claude", "antigravity"].map((agent) => {
      const key = `project:${agent}:${p}`;
      const t = x.targets.find((y) => y.key === key) || {
        key, agent, scope: "project", project: p, status: "off",
        root: agent === "claude" ? p + "/.claude/agents" : p + "/.agents/agents",
      };
      return studioRow(kind, x, t, ro);
    });
    return `<div class="pub-project"><div class="pub-project-name">${icon("folder")}<span class="mono">${esc(tilde(p))}</span></div>${rows.join("")}</div>`;
  });
  const options = sk.data.projects.filter((p) => !projects.has(p))
    .map((p) => `<option value="${esc(p)}">${esc(tilde(p))}</option>`).join("");
  const extra = kind === "team" ? "Its member profiles are published with it." : x.skills.length ? "Its skills are published with it." : "";
  return `<section class="sk-section">
      <h2>Everywhere</h2>
      <p class="hint">Every session of that agent, in any folder. ${extra}</p>
      ${x.targets.filter((t) => t.scope === "global").map((t) => studioRow(kind, x, t, ro)).join("")}
    </section>
    <section class="sk-section">
      <h2>Per project</h2>
      <p class="hint">Only sessions working in that folder.</p>
      ${projectRows.join("") || `<p class="hint">Not published to any project.</p>`}
      ${options ? `<div class="pub-add"><select id="pr-project" ${ro ? "disabled" : ""}>
        <option value="">Choose a project…</option>${options}</select></div>` : ""}
    </section>
    <section class="sk-section"><h2>Run it</h2>
      ${x.targets.some((t) => t.status !== "off" && t.status !== "conflict")
        ? `<p class="hint">In a folder where it is published, ${kind === "team" ? "start the lead and give it the job" : "ask for it by name, or start a session as it"}:</p>
          <pre class="run-hint">claude --agent ${esc(x.name)}\n# or, in a session: "use the ${esc(x.name)} agent to …"</pre>`
        : `<div class="notice error">Not published yet: Claude Code and Antigravity only find agents in their
          agents folders. Publish it above (everywhere, or to a project), then run
          <code>claude --agent ${esc(x.name)}</code> there.</div>`}
    </section>`;
}

function studioAuditHTML(kind, name) {
  const rows = (sk.data.audit || []).filter((a) => a.kind && (!kind || a.kind === kind) && (!name || a.skill === name)).slice(0, 12);
  if (!rows.length) return "";
  return `<section class="sk-section sk-log"><h2>Activity</h2>${rows.map((a) => `
    <div class="log-row"><span class="mono">${clock(a.ts)}</span>
      <span>${esc(a.kind)} <b>${esc(a.skill)}</b> ${esc(ACTIONS[a.action] || a.action)}${a.target ? ` → ${esc(targetLabel(a.target))}` : ""}${a.synced && a.synced.length ? ` · ${a.synced.length} cop${a.synced.length === 1 ? "y" : "ies"} synced` : ""}</span>
      <span class="muted">${esc(a.actor || "")}</span></div>`).join("")}</section>`;
}

/* ----------------------------------------------------------------- actions */
async function studioRun(label, fn) {
  try {
    const out = await fn();
    const notes = out && out.notes && out.notes.length ? " " + out.notes.join("; ") : "";
    pr.notice = label ? { kind: notes ? "error" : "ok", text: label + notes } : null;
    return out;
  } catch (err) {
    pr.notice = { kind: "error", text: err.status === 401 ? "Editing locked itself. Unlock again to continue." : err.message };
    return null;
  } finally {
    await Promise.all([loadSkills(), loadStudio()]);
  }
}

function pick(kind, name) {
  if (prDirty() && !confirm("Discard your unsaved changes?")) return;
  pr.sel = kind ? { kind, name } : null;
  pr.notice = null;
  pr.project = "";
  $("#pr-main").dataset.view = "";
  renderStudio();
}

async function studioSave() {
  const { kind, name } = pr.sel;
  const creating = !name;
  const item = readItem();
  const out = await studioRun(creating ? `${KIND_LABEL[kind]} created.` : "Saved. Published copies were updated.", () =>
    api(`/api/studio/${creating ? "create" : "update"}`, { kind, item }));
  if (out && creating) { pr.sel = { kind, name: out.name }; renderStudio(); }
}

async function studioDelete() {
  const { kind, name } = pr.sel;
  const x = find(kind, name);
  const copies = (x.targets || []).filter((t) => t.status !== "off" && t.status !== "conflict").length;
  if (!confirm(`Delete ${kind} "${name}"${copies ? ` and its ${copies} published cop${copies === 1 ? "y" : "ies"}` : ""}? This can't be undone.`)) return;
  const edited = (x.targets || []).some((t) => t.status === "modified");
  if (edited && !confirm("A published copy was edited outside SpecOps. Delete it anyway?")) return;
  const out = await studioRun(`${KIND_LABEL[kind]} deleted.`, () => api("/api/studio/delete", { kind, name, force: edited }));
  if (out) { pr.sel = null; renderStudio(); }
}

async function studioPublish(btn) {
  const on = btn.dataset.on === "1";
  let force = false;
  if (btn.dataset.status === "modified") {
    if (!confirm(`This file was edited outside SpecOps. ${on ? "Overwrite" : "Delete"} those edits?`)) return;
    force = true;
  }
  const { kind, name } = pr.sel;
  await studioRun(on ? "Published." : "Removed.", () =>
    api("/api/studio/publish", { kind, name, target: btn.dataset.prPublish, on, force }));
}

/* ----------------------------------------------------------------- events */
$("#profiles-view").addEventListener("click", (e) => {
  const t = e.target;
  const item = t.closest("[data-pick]");
  if (item) return pick(item.dataset.pick, item.dataset.name);
  const add = t.closest("[data-new]");
  if (add) return pick(add.dataset.new, "");
  if (t.closest("[data-unlock]")) return openUnlock();
  if (t.closest("[data-lock]")) return lockNow();
  if (t.closest("[data-starters]")) {
    return studioRun("Starter agents added. Publish them where you want them.", () => api("/api/studio/starters", {}));
  }
  if (t.closest("#pr-save")) return studioSave();
  if (t.closest("#pr-delete")) return studioDelete();
  if (t.closest("#pr-cancel")) {
    if (prDirty() && !confirm("Discard your unsaved changes?")) return;
    if (!pr.sel.name) pr.sel = null;
    $("#pr-main").dataset.view = "";
    return renderStudio();
  }
  const pub = t.closest("[data-pr-publish]");
  if (pub) return studioPublish(pub);
});
$("#profiles-view").addEventListener("change", (e) => {
  if (e.target.id === "pr-project") { pr.project = e.target.value; renderStudioMain(); }
});
window.addEventListener("beforeunload", (e) => { if (prDirty()) e.preventDefault(); });
window.addEventListener("focus", () => { if (document.body.dataset.view === "profiles") VIEWS.profiles(); });
