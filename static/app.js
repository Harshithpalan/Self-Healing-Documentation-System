"use strict";
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ticks = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>");

const state = {
  name: "", files: {}, report: null, selected: new Set(), timeline: [], config: null, activeFile: null, aiFixes: {},
  llm: JSON.parse(localStorage.getItem("shd.llm") || '{"provider":"offline"}'),
};

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.add("hidden"), err ? 6000 : 3000);
}

async function api(path, body, opts = {}) {
  const init = body instanceof FormData ? { method: "POST", body }
    : body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
  const res = await fetch(path, init);
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
    throw new Error(msg);
  }
  return opts.raw ? res : res.json();
}

async function busy(btn, fn) {
  btn.disabled = true; btn.classList.add("loading");
  try { return await fn(); } catch (e) { toast(e.message, true); } finally { btn.classList.remove("loading"); btn.disabled = false; refreshButtons(); }
}

// ---------------------------------------------------------------- project loading
function setProject(name, files, note = "") {
  state.name = name; state.files = files; state.report = null; state.selected.clear(); state.timeline = []; state.aiFixes = {};
  const paths = Object.keys(files);
  const docs = paths.filter(isDoc).length;
  $("#projectInfo").classList.remove("hidden");
  $("#projectInfo").innerHTML = `<b>${esc(name)}</b>${paths.length} files · ${docs} docs · ${paths.length - docs} code/config${note ? " · " + esc(note) : ""}`;
  $("#healthCard").classList.add("hidden"); $("#timelineCard").classList.add("hidden");
  $("#issuesUi").classList.add("hidden"); $("#empty").classList.remove("hidden");
  $("#diff").innerHTML = ""; $("#issueCount").textContent = "0";
  state.activeFile = null; renderFiles(); refreshButtons();
  return scan();
}
const isDoc = (p) => /\.(md|markdown|mdx)$/i.test(p);

$("#sampleBtn").onclick = (e) => busy(e.currentTarget, async () => {
  const r = await api("/api/sample");
  await setProject(r.name, r.files);
});

async function upload(fileList) {
  const fd = new FormData();
  for (const f of fileList) fd.append("files", f, f.webkitRelativePath || f.name);
  const r = await api("/api/ingest", fd);
  await setProject(r.name, r.files, r.skipped ? `${r.skipped} skipped` : "");
}
$("#fileInput").onchange = (e) => e.target.files.length && busy($("#scanBtn"), () => upload(e.target.files));
const drop = $("#drop");
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => e.dataTransfer.files.length && busy($("#scanBtn"), () => upload(e.dataTransfer.files)));

$("#ghBtn").onclick = (e) => {
  const url = $("#ghUrl").value.trim();
  if (!url) return toast("Enter a GitHub repository URL", true);
  busy(e.currentTarget, async () => {
    const fd = new FormData(); fd.append("github_url", url);
    const r = await api("/api/ingest", fd);
    await setProject(r.name, r.files, r.skipped ? `${r.skipped} skipped` : "");
  });
};

// ---------------------------------------------------------------- scanning
async function scan(label) {
  const r = await api("/api/scan", { files: state.files });
  const prev = state.report?.score;
  state.report = r; state.aiFixes = {};
  state.selected = new Set(r.issues.filter((i) => i.fix && i.confidence === "high").map((i) => i.id));
  state.timeline.push({ label: label || (state.timeline.length ? "Rescan" : "Initial scan"), score: r.score, issues: r.issues.length, delta: prev == null ? null : r.score - prev });
  renderReport();
}
$("#scanBtn").onclick = (e) => busy(e.currentTarget, () => scan());

function renderReport() {
  const r = state.report;
  $("#healthCard").classList.remove("hidden");
  $("#scoreVal").textContent = r.score;
  const fg = $("#ringFg");
  fg.style.strokeDashoffset = 326.7 * (1 - r.score / 100);
  fg.style.stroke = r.score >= 85 ? "var(--ok)" : r.score >= 60 ? "var(--warning)" : "var(--error)";
  const s = r.stats;
  const cov = (a, b) => (b ? Math.round((100 * a) / b) : 100);
  $("#stats").innerHTML = `
    <div><span>Errors</span><b class="${s.errors ? "error" : "ok"}">${s.errors}</b></div>
    <div><span>Warnings</span><b class="${s.warnings ? "warning" : "ok"}">${s.warnings}</b></div>
    <div><span>Auto-fixable</span><b>${s.auto_fixable}</b></div>
    <div><span>References checked</span><b>${s.checks}</b></div>
    ${[["Symbols documented", s.documented_symbols, s.public_symbols], ["Routes documented", s.documented_routes, s.routes], ["Env vars documented", s.documented_env_vars, s.env_vars]]
      .filter(([, , b]) => b).map(([l, a, b]) => `<div title="${a}/${b}"><span>${l}</span><b>${cov(a, b)}%</b></div><div class="bar"><i style="width:${cov(a, b)}%"></i></div>`).join("")}`;
  if (!s.doc_files) toast("No Markdown documentation found in this project", true);

  $("#timelineCard").classList.remove("hidden");
  $("#timeline").innerHTML = state.timeline.map((t) => `<li><span>${esc(t.label)} · ${t.issues} issues</span><span>${t.score}${t.delta ? ` <span class="delta">${t.delta > 0 ? "+" : ""}${t.delta}</span>` : ""}</span></li>`).join("");

  const rules = [...new Set(r.issues.map((i) => i.rule))].sort();
  const cur = $("#ruleFilter").value;
  $("#ruleFilter").innerHTML = `<option value="">All rules</option>` + rules.map((x) => `<option ${x === cur ? "selected" : ""}>${x}</option>`).join("");
  $("#empty").classList.toggle("hidden", true);
  $("#issuesUi").classList.remove("hidden");
  $("#issueCount").textContent = r.issues.length;
  renderIssues(); renderFiles(); refreshButtons();
}

function fixView(fix) {
  if (!fix) return `<div class="fix muted">No automatic fix. ${state.llm.provider !== "offline" ? "Try AI fix." : "Fix manually, or enable an AI provider."}</div>`;
  if (fix.kind === "replace") return `<div class="fix"><span class="muted">Fix:</span><code class="old">${esc(fix.old)}</code>→<code class="new">${esc(fix.new)}</code></div>`;
  const verb = fix.kind === "insert" ? "Insert" : "Rewrite";
  return `<div class="fix"><span class="muted">Fix: ${verb} ${fix.new.split("\n").length} line(s) in ${esc(fix.file)}</span><pre class="evidence">${esc(fix.new)}</pre></div>`;
}

function renderIssues() {
  const sev = $("#sevFilter").value, rule = $("#ruleFilter").value;
  const list = state.report.issues.filter((i) => (!sev || i.severity === sev) && (!rule || i.rule === rule));
  if (!state.report.issues.length) {
    $("#issues").innerHTML = `<div class="empty"><h2>Docs are in sync</h2><p>No drift detected between the documentation and the code.</p></div>`;
    return;
  }
  $("#issues").innerHTML = list.map((i) => {
    const fix = state.aiFixes[i.id] || i.fix;
    return `<article class="issue ${i.severity}" data-id="${i.id}">
      <input type="checkbox" class="sel" ${fix ? "" : "disabled"} ${state.selected.has(i.id) ? "checked" : ""}>
      <div>
        <div class="issue-head"><span class="pill ${i.severity}">${i.severity}</span><span class="rule">${i.rule}</span>
          <span class="loc" data-file="${esc(i.file)}">${esc(i.file)}:${i.line}</span>
          ${fix ? `<span class="pill conf">${state.aiFixes[i.id] ? "AI" : i.confidence} confidence</span>` : ""}</div>
        <div class="issue-msg">${ticks(i.message)}</div>
        ${i.evidence ? `<pre class="evidence">${esc(i.evidence)}</pre>` : ""}
        ${fixView(fix)}
        ${state.llm.provider !== "offline" ? `<div class="issue-actions"><button class="btn small ai-btn">AI fix</button></div>` : ""}
      </div></article>`;
  }).join("") || `<p class="hint pad">No issues match the filters.</p>`;
  $$("#issues .sel").forEach((cb) => cb.onchange = () => {
    const id = cb.closest(".issue").dataset.id;
    cb.checked ? state.selected.add(id) : state.selected.delete(id); refreshButtons();
  });
  $$("#issues .loc").forEach((el) => el.onclick = () => openFile(el.dataset.file, +el.textContent.split(":").pop()));
  $$("#issues .ai-btn").forEach((b) => b.onclick = () => aiFix(b, b.closest(".issue").dataset.id));
}
$("#sevFilter").onchange = renderIssues;
$("#ruleFilter").onchange = renderIssues;
$("#selectAll").onchange = (e) => {
  const ids = state.report.issues.filter((i) => i.fix || state.aiFixes[i.id]).map((i) => i.id);
  state.selected = new Set(e.target.checked ? ids : []); renderIssues(); refreshButtons();
};

function chosenIssues() {
  return state.report.issues.filter((i) => state.selected.has(i.id)).map((i) => ({ ...i, fix: state.aiFixes[i.id] || i.fix })).filter((i) => i.fix);
}

function refreshButtons() {
  const has = Object.keys(state.files).length > 0;
  $("#scanBtn").disabled = !has; $("#exportBtn").disabled = !has;
  const n = state.report ? chosenIssues().length : 0;
  $("#previewBtn").disabled = !n; $("#healBtn").disabled = !n;
  $("#healBtn").textContent = n ? `Heal selected (${n})` : "Heal selected";
  $("#autoBtn").disabled = !state.report || !state.report.issues.some((i) => i.fix && i.confidence === "high");
}

// ---------------------------------------------------------------- healing
function renderDiff(diffs, summary) {
  $("#diffSummary").innerHTML = summary;
  $("#diff").innerHTML = diffs.map((d) => `<div class="diff-file"><h4>${esc(d.path)}<span><span class="plus">+${d.added}</span> <span class="minus">−${d.removed}</span></span></h4><pre>${
    d.diff.split("\n").filter((l) => !l.startsWith("---") && !l.startsWith("+++")).map((l) => `<span class="ln ${l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : l.startsWith("@@") ? "hunk" : ""}">${esc(l) || " "}</span>`).join("")
  }</pre></div>`).join("");
}

$("#previewBtn").onclick = (e) => busy(e.currentTarget, async () => {
  const r = await api("/api/preview", { files: state.files, issues: chosenIssues() });
  renderDiff(r.diff, `${r.applied.length} fix(es) ready across ${r.diff.length} file(s)${r.skipped.length ? `, ${r.skipped.length} could not be applied` : ""}. <button class="btn small primary" id="applyFromDiff">Apply these fixes</button>`);
  $("#applyFromDiff").onclick = () => $("#healBtn").click();
  showTab("diff");
});

async function heal(issues, label) {
  const before = state.files;
  const r = await api("/api/heal", { files: state.files, issues });
  state.files = r.files;
  const prev = state.report.score;
  state.report = r.report; state.aiFixes = {};
  state.selected = new Set(r.report.issues.filter((i) => i.fix && i.confidence === "high").map((i) => i.id));
  state.timeline.push({ label: `${label} (${r.applied.length} fixes)`, score: r.report.score, issues: r.report.issues.length, delta: r.report.score - prev });
  renderDiff(r.diff, `Applied ${r.applied.length} fix(es) to ${r.diff.length} file(s). Health ${prev} → ${r.report.score}.`);
  renderReport();
  toast(`Healed ${r.applied.length} issue(s). Health ${prev} → ${r.report.score}`);
  void before;
}
$("#healBtn").onclick = (e) => busy(e.currentTarget, () => heal(chosenIssues(), "Healed selection"));
$("#autoBtn").onclick = (e) => busy(e.currentTarget, async () => {
  for (let pass = 0; pass < 3; pass++) {
    const issues = state.report.issues.filter((i) => i.fix && i.confidence === "high");
    if (!issues.length) break;
    await heal(issues, pass ? "Auto-heal pass " + (pass + 1) : "Auto-heal");
  }
});

async function aiFix(btn, id) {
  const issue = state.report.issues.find((i) => i.id === id);
  await busy(btn, async () => {
    const r = await api("/api/ai-fix", { files: state.files, issue, llm: state.llm });
    state.aiFixes[id] = r.fix; state.selected.add(id);
    renderIssues(); toast("AI fix proposed. Review it, then heal.");
  });
}

$("#exportBtn").onclick = (e) => busy(e.currentTarget, async () => {
  const res = await api("/api/export", { files: state.files, name: state.name.replace(/\s*\(.*\)/, "") }, { raw: true });
  const blob = await res.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = (res.headers.get("Content-Disposition") || "").match(/filename="(.+)"/)?.[1] || "healed-docs.zip";
  a.click(); URL.revokeObjectURL(a.href);
});

// ---------------------------------------------------------------- files
function renderFiles() {
  const counts = {};
  (state.report?.issues || []).forEach((i) => (counts[i.file] = (counts[i.file] || 0) + 1));
  const paths = Object.keys(state.files).sort((a, b) => (isDoc(b) - isDoc(a)) || a.localeCompare(b));
  $("#fileList").innerHTML = paths.map((p) => `<li data-path="${esc(p)}" class="${p === state.activeFile ? "active" : ""}"><span class="${isDoc(p) ? "doc" : ""}">${esc(p)}</span>${counts[p] ? `<span class="n">${counts[p]}</span>` : ""}</li>`).join("");
  $$("#fileList li").forEach((li) => li.onclick = () => openFile(li.dataset.path));
  if (state.activeFile && state.files[state.activeFile] != null && document.activeElement !== $("#editor")) $("#editor").value = state.files[state.activeFile];
}
function openFile(path, line) {
  if (state.files[path] == null) return;
  state.activeFile = path; showTab("files"); renderFiles();
  const ed = $("#editor");
  ed.disabled = false; ed.value = state.files[path]; $("#editorPath").textContent = path;
  if (line) {
    const lines = ed.value.split("\n");
    const start = lines.slice(0, line - 1).join("\n").length + (line > 1 ? 1 : 0);
    ed.focus(); ed.setSelectionRange(start, start + (lines[line - 1] || "").length);
    ed.scrollTop = Math.max(0, (line - 6) * 19.4);
  }
}
$("#editor").oninput = (e) => { if (state.activeFile) state.files[state.activeFile] = e.target.value; };

// ---------------------------------------------------------------- tabs, theme, settings
function showTab(name) {
  $$("#mainTabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".tab-pane").forEach((p) => p.classList.toggle("hidden", p.id !== "tab-" + name));
}
$$("#mainTabs .tab").forEach((t) => t.onclick = () => showTab(t.dataset.tab));

const theme = localStorage.getItem("shd.theme") || "dark";
document.documentElement.dataset.theme = theme;
$("#themeBtn").onclick = () => {
  const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = t; localStorage.setItem("shd.theme", t);
};

function updateBadge() {
  const p = state.config?.providers?.[state.llm.provider];
  const ai = state.llm.provider !== "offline";
  $("#modeBadge").textContent = ai ? `Rules + ${p?.label.split(" /")[0] || state.llm.provider}` : "Rule engine (offline)";
  $("#modeBadge").classList.toggle("ai", ai);
}
$("#settingsBtn").onclick = () => {
  const s = state.llm;
  $("#provider").value = s.provider; $("#model").value = s.model || ""; $("#apiKey").value = s.api_key || ""; $("#baseUrl").value = s.base_url || "";
  $("#settings").showModal();
};
$("#saveSettings").onclick = () => {
  state.llm = { provider: $("#provider").value, model: $("#model").value.trim(), api_key: $("#apiKey").value.trim(), base_url: $("#baseUrl").value.trim() };
  localStorage.setItem("shd.llm", JSON.stringify(state.llm));
  updateBadge(); if (state.report) renderIssues();
};

(async function init() {
  try {
    state.config = await api("/api/config");
    $("#provider").innerHTML = Object.entries(state.config.providers).map(([k, v]) => `<option value="${k}">${esc(v.label)}${v.server_key ? " (server key)" : ""}</option>`).join("");
  } catch (e) { toast(e.message, true); }
  updateBadge(); refreshButtons();
})();
