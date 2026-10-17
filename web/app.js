// ASCEnD DocGen front end: chat on the left, live editable document on the right.
// All document state lives in the browser (localStorage), keyed by template id.
"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const S = {
  tid: null,
  schema: null,
  state: { values: {}, skipped: [], signatories: {}, sig_titles: {}, sig_removed: [] },
  chat: [],          // {role: "user"|"assistant"|"note"|"error", text, images?: [url], files?: [name]}
  pending: [],       // files waiting to be sent: {file, url} (url is null for documents)
  missing: [],
  values: {},
  health: {},
  busy: false,
};

// ---------------------------------------------------------------- storage
const key = () => `ascend-docgen:${S.tid}`;
const emptyState = () => ({ values: {}, skipped: [], signatories: {}, sig_titles: {}, sig_removed: [] });
function save() {
  try { localStorage.setItem(key(), JSON.stringify({ state: S.state, chat: S.chat.slice(-60) })); } catch (_) {}
}
function restore() {
  try {
    const raw = JSON.parse(localStorage.getItem(key()) || "null");
    if (raw) { S.state = Object.assign(emptyState(), raw.state); S.chat = raw.chat || []; return; }
  } catch (_) {}
  S.state = emptyState();
  S.chat = [];
}

// ---------------------------------------------------------------- user's own API key
// Kept only in this browser and sent as X-Gemini-Key; the server uses it for that request.
const KEY_STORE = "ascend-docgen:gemini-key";
function userKey() { try { return localStorage.getItem(KEY_STORE) || ""; } catch (_) { return ""; } }
function setUserKey(k) {
  try { k ? localStorage.setItem(KEY_STORE, k) : localStorage.removeItem(KEY_STORE); } catch (_) { toast("This browser cannot save the key"); }
}

// ---------------------------------------------------------------- api
async function api(path, body) {
  const headers = { "Content-Type": "application/json" };
  if (userKey()) headers["X-Gemini-Key"] = userKey();
  const res = await fetch(path, body === undefined ? {} : { method: "POST", headers, body: JSON.stringify(body) });
  if (!res.ok) {
    let msg = `${res.status}`;
    try { msg = (await res.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res;
}
const getJSON = async (p, b) => (await api(p, b)).json();

function toast(text) {
  const t = $("#toast");
  t.textContent = text; t.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => (t.hidden = true), 3200);
}

// ---------------------------------------------------------------- init
async function init() {
  S.health = await getJSON("/api/health").catch(() => ({}));
  renderBadge();

  const templates = await getJSON("/api/templates");
  const sel = $("#template");
  sel.innerHTML = templates.map((t) => `<option value="${esc(t.id)}">${esc(t.office ? t.office + " · " : "")}${esc(t.name)}</option>`).join("");
  const last = localStorage.getItem("ascend-docgen:last");
  if (last && templates.some((t) => t.id === last)) sel.value = last;
  sel.onchange = () => loadTemplate(sel.value);
  await loadTemplate(sel.value);
}

async function loadTemplate(tid) {
  S.tid = tid;
  try { localStorage.setItem("ascend-docgen:last", tid); } catch (_) {}
  S.schema = await getJSON(`/api/templates/${tid}`);
  $("#doc-title").textContent = S.schema.name;
  restore();
  if (!S.chat.length) greet();
  renderChat();
  await refresh();
}

function renderBadge() {
  const badge = $("#provider");
  const prov = S.health.ai_provider || "unknown";
  const offline = !userKey() && prov === "offline";
  badge.textContent = userKey() ? "AI: gemini (your key)" : offline ? "Offline mode · add API key" : `AI: ${prov}`;
  badge.classList.toggle("offline", offline);
}

const keyDialog = $("#key-dialog");
function keyStatus() {
  const prov = S.health.ai_provider || "unknown";
  $("#key-status").textContent = userKey()
    ? "Using your key."
    : prov === "offline" ? "No key yet: the assistant is in offline mode and can only read labelled notes."
    : "No key of your own: using the server's key.";
}
$("#provider").addEventListener("click", () => {
  $("#key-input").value = userKey();
  $("#key-input").type = "password"; $("#key-show").checked = false;
  $("#key-remove").hidden = !userKey();
  keyStatus();
  keyDialog.showModal();
  $("#key-input").focus();
});
$("#key-show").addEventListener("change", (e) => { $("#key-input").type = e.target.checked ? "text" : "password"; });
$("#key-cancel").addEventListener("click", () => keyDialog.close());
$("#key-remove").addEventListener("click", () => { setUserKey(""); renderBadge(); keyDialog.close(); toast("Key removed from this browser"); });
$("#key-form").addEventListener("submit", (e) => {
  const k = $("#key-input").value.trim();
  if (!k) { e.preventDefault(); $("#key-input").focus(); return; }
  setUserKey(k); renderBadge(); toast("Key saved in this browser");
});

function greet() {
  S.chat.push({
    role: "assistant",
    text: `Hi! Let's fill out the ${S.schema.name}. Paste whatever you have about the event: notes, the program, a list of attendees, photos (event pictures, a signed attendance sheet, the pubmat), or files like a PDF, Word document, slides or spreadsheet. I'll put everything in the right place and ask for anything missing.\n\nYou can also click any part of the document on the right to edit it directly.`,
  });
}

// ---------------------------------------------------------------- preview
let refreshSeq = 0;
async function refresh(changed = []) {
  const seq = ++refreshSeq;
  const data = await getJSON("/api/preview", { template_id: S.tid, state: S.state });
  if (seq !== refreshSeq) return;
  S.missing = data.missing; S.values = data.values;
  // keep focus on the field the user moved to while we re-rendered
  const act = document.activeElement;
  const focusSel = act && $("#pages").contains(act)
    ? ["field", "sig", "sigtitle", "row", "col", "img"].filter((k) => act.dataset[k] !== undefined)
        .map((k) => `[data-${k}="${CSS.escape(act.dataset[k])}"]`).join("")
    : "";
  $("#pages").innerHTML = data.html;
  if (focusSel) {
    const el = $("#pages").querySelector(focusSel);
    if (el) { el.focus(); const r = document.createRange(); r.selectNodeContents(el); r.collapse(false); const s = getSelection(); s.removeAllRanges(); s.addRange(r); }
  }
  layoutTabs();
  fitPages();
  renderNeeds();
  renderMeta();
  renderProgress();
  for (const id of changed) {
    document.querySelectorAll(`#pages [data-field="${id}"]`).forEach((el) => el.classList.add("changed"));
  }
}

// Word tab stops: explicit stops from the template, else every 0.5 inch (48px).
function layoutTabs() {
  document.querySelectorAll("#pages p.dp").forEach((p) => {
    const tabs = p.querySelectorAll(".tab");
    if (!tabs.length) return;
    const stops = (p.dataset.tabs || "").split(",").filter(Boolean).map(Number).sort((a, b) => a - b);
    tabs.forEach((t) => {
      t.style.width = "0px";
      const x = t.offsetLeft;
      let next = stops.find((s) => s > x + 1);
      if (next === undefined) next = (Math.floor(x / 48) + 1) * 48;
      t.style.width = `${next - x}px`;
    });
  });
}

function fitPages() {
  const wrap = $("#pages-wrap"), pages = $("#pages");
  const page = pages.querySelector(".page");
  if (!page) return;
  pages.style.zoom = 1;
  const avail = wrap.clientWidth - (window.innerWidth <= 900 ? 32 : 48);
  pages.style.zoom = Math.min(1, avail / page.offsetWidth).toFixed(3);
}
window.addEventListener("resize", fitPages);

function fieldDef(id) { return S.schema.fields.find((f) => f.id === id); }

function renderProgress() {
  const total = S.schema.fields.length;
  const filled = S.schema.fields.filter((f) => S.values[f.id]).length;
  const skipped = S.schema.fields.filter((f) => !S.values[f.id] && S.state.skipped.includes(f.id)).length;
  $("#bar").style.width = `${Math.round(((filled + skipped) / total) * 100)}%`;
  $("#progress-text").textContent = `${filled} of ${total} filled${skipped ? `, ${skipped} skipped` : ""}`;
}

// fields that feed the document but are not printed on it (e.g. date, venue)
function renderMeta() {
  const hidden = S.schema.fields.filter((f) => f.in_document === false);
  const meta = $("#meta");
  const placeholders = Object.entries(S.schema.config_signatories || {})
    .filter(([role, v]) => /^\[.*\]$/.test(v) && !(S.state.signatories || {})[role] && !(S.state.sig_removed || []).includes(role));
  meta.innerHTML =
    hidden.map((f) => `<label>${esc(f.label)}<input data-meta="${esc(f.id)}" value="${esc(S.values[f.id] || "")}" placeholder="Not printed; used for the summary"></label>`).join("") +
    (placeholders.length ? `<span class="note">Highlighted signatory names are placeholders from config.json. Click one in the document to set it for this report, or update config.json once for every report.</span>` : "");
  meta.querySelectorAll("input[data-meta]").forEach((inp) => {
    inp.addEventListener("change", () => setValue(inp.dataset.meta, inp.value.trim() || null));
  });
}

// ---------------------------------------------------------------- inline editing
function setValue(fid, value) {
  if (value === null || value === "" || (Array.isArray(value) && !value.length)) delete S.state.values[fid];
  else S.state.values[fid] = value;
  S.state.skipped = S.state.skipped.filter((x) => x !== fid);
  save(); refresh();
}

function currentValue(fid) {
  // start from the resolved value (includes defaults) so edits keep them
  return JSON.parse(JSON.stringify(S.state.values[fid] ?? S.values[fid] ?? null));
}

const pages = $("#pages");
pages.addEventListener("keydown", (e) => {
  const el = e.target;
  if (e.key === "Enter" && el.isContentEditable && !el.classList.contains("blk")) { e.preventDefault(); el.blur(); }
});
pages.addEventListener("focusout", (e) => {
  const el = e.target;
  if (!el.isContentEditable) return;
  const text = el.innerText.replace(/ /g, " ").trim();
  if (el.dataset.sig) {
    const role = el.dataset.sig;
    const cfg = (S.schema.config_signatories || {})[role] || "";
    S.state.signatories = S.state.signatories || {};
    if (!text || text === cfg) delete S.state.signatories[role]; else S.state.signatories[role] = text;
    save(); refresh(); return;
  }
  if (el.dataset.sigtitle) {
    const role = el.dataset.sigtitle;
    const def = (S.schema.signatories || {})[role] || "";
    S.state.sig_titles = S.state.sig_titles || {};
    if (!text || text === def) delete S.state.sig_titles[role]; else S.state.sig_titles[role] = text;
    save(); refresh(); return;
  }
  const fid = el.dataset.field;
  if (!fid) return;
  const f = fieldDef(fid);
  if (el.dataset.row !== undefined) {
    const rows = currentValue(fid) || [];
    const r = +el.dataset.row, c = +el.dataset.col;
    while (rows.length <= r) rows.push(new Array(f.columns.length).fill(""));
    if ((rows[r][c] || "") === text) return;
    rows[r][c] = text;
    setValue(fid, rows.filter((row) => row.some(Boolean)));
  } else if (el.dataset.img !== undefined) {
    const imgs = currentValue(fid) || [];
    const i = +el.dataset.img;
    if (!imgs[i] || imgs[i].caption === text) return;
    imgs[i].caption = text;
    setValue(fid, imgs);
  } else {
    const val = f.type === "longtext"
      ? [...el.querySelectorAll("p")].length
        ? el.innerText.split(/\n+/).map((s) => s.trim()).filter(Boolean).join("\n")
        : text
      : text.replace(/\s*\n\s*/g, " ");
    if ((S.values[fid] || "") === val) return;
    setValue(fid, val || null);
  }
});
pages.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.sigRemove || b.dataset.sigAdd) {
    const role = b.dataset.sigRemove || b.dataset.sigAdd;
    const rest = (S.state.sig_removed || []).filter((r) => r !== role);
    S.state.sig_removed = b.dataset.sigRemove ? [...rest, role] : rest;
    save(); refresh(); return;
  }
  const fid = b.dataset.field || b.dataset.addRow;
  if (b.dataset.addRow !== undefined) {
    const f = fieldDef(fid);
    const tbody = pages.querySelector(`table[data-table="${fid}"] tbody`);
    const r = tbody.rows.length;
    const tr = document.createElement("tr");
    tr.innerHTML = (f.numbered ? `<td class="no">${r + 1}</td>` : "") +
      f.columns.map((_, c) => `<td contenteditable="plaintext-only" data-field="${esc(fid)}" data-row="${r}" data-col="${c}"></td>`).join("") +
      `<td class="row-x no-print"></td>`;
    tbody.appendChild(tr);
    tr.querySelector("[contenteditable]").focus();
  } else if (b.dataset.delRow !== undefined) {
    const rows = currentValue(fid) || [];
    rows.splice(+b.dataset.delRow, 1);
    setValue(fid, rows);
  } else if (b.dataset.delImg !== undefined) {
    const imgs = currentValue(fid) || [];
    imgs.splice(+b.dataset.delImg, 1);
    setValue(fid, imgs);
  }
});

// ---------------------------------------------------------------- chat
function renderChat() {
  const log = $("#log");
  log.innerHTML = S.chat.map((m) => {
    const av = m.role === "user" ? "You".slice(0, 1) : "A";
    const imgs = (m.images || []).length ? `<div class="imgs">${m.images.map((u) => `<img src="${esc(u)}" alt="">`).join("")}</div>` : "";
    const docs = (m.files || []).length ? `<div class="imgs">${m.files.map((n) => `<span class="file-chip">${esc(n)}</span>`).join("")}</div>` : "";
    return `<div class="msg ${esc(m.role)}"><div class="av">${av}</div><div class="bubble">${esc(m.text)}${imgs}${docs}</div></div>`;
  }).join("") + (S.busy ? `<div class="msg"><div class="av">A</div><div class="bubble typing"><span></span><span></span><span></span></div></div>` : "");
  log.scrollTop = log.scrollHeight;
}

function renderNeeds() {
  const box = $("#needs");
  const skipped = S.state.skipped.filter((id) => fieldDef(id) && !S.values[id]);
  if (!S.missing.length && !skipped.length) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML =
    (S.missing.length ? `<div class="needs-title">Still needed</div><div class="chips">${S.missing.map((m) =>
      `<span class="chip ${m.required ? "req" : ""}"><button class="ask" data-ask="${esc(m.id)}" title="${esc(fieldDef(m.id)?.hint || "")}">${esc(m.label)}</button><button class="skip" data-skip="${esc(m.id)}" title="Skip this">Skip</button></span>`).join("")}</div>` : "") +
    (skipped.length ? `<div class="skipped-line">Skipped: ${skipped.map((id) => `${esc(fieldDef(id).label)} <button data-unskip="${esc(id)}">undo</button>`).join(", ")}</div>` : "");
}

$("#needs").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.skip) {
    S.state.skipped = [...new Set([...S.state.skipped, b.dataset.skip])];
    S.chat.push({ role: "note", text: `Skipped ${fieldDef(b.dataset.skip).label}.` });
    save(); renderChat(); refresh();
  } else if (b.dataset.unskip) {
    S.state.skipped = S.state.skipped.filter((x) => x !== b.dataset.unskip);
    save(); refresh();
  } else if (b.dataset.ask) {
    const f = fieldDef(b.dataset.ask);
    const input = $("#input");
    input.value = (input.value ? input.value + "\n" : "") + `${f.label}: `;
    input.focus();
  }
});

// documents the server can read (keep in sync with app/files.py DOC_EXT)
const DOC_EXT = [".pdf", ".docx", ".doc", ".odt", ".pptx", ".ppt", ".odp", ".xlsx", ".xls", ".ods", ".txt", ".md", ".csv", ".tsv", ".json", ".rtf"];
const isDoc = (file) => DOC_EXT.some((x) => file.name.toLowerCase().endsWith(x));
function addFiles(files) {
  for (const file of files) {
    if (file.type.startsWith("image/")) S.pending.push({ file, url: URL.createObjectURL(file) });
    else if (isDoc(file)) S.pending.push({ file, url: null });
    else toast(`${file.name}: unsupported file type`);
  }
  renderThumbs();
}
function renderThumbs() {
  $("#thumbs").innerHTML = S.pending.map((p, i) => `<span class="thumb">${p.url
    ? `<img src="${p.url}" alt="">`
    : `<span class="file-chip" title="${esc(p.file.name)}">${esc(p.file.name)}</span>`}<button type="button" data-rm="${i}" title="Remove">×</button></span>`).join("");
}
$("#thumbs").addEventListener("click", (e) => {
  const i = e.target.dataset.rm;
  if (i !== undefined) { S.pending.splice(+i, 1); renderThumbs(); }
});
$("#files").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });
const composer = $("#composer");
composer.addEventListener("dragover", (e) => { e.preventDefault(); composer.classList.add("drag"); });
composer.addEventListener("dragleave", () => composer.classList.remove("drag"));
composer.addEventListener("drop", (e) => { e.preventDefault(); composer.classList.remove("drag"); addFiles(e.dataTransfer.files); });
$("#input").addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) { e.preventDefault(); addFiles(files); }
});
$("#input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); composer.requestSubmit(); }
});

composer.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (S.busy) return;
  const text = $("#input").value.trim();
  if (!text && !S.pending.length) return;
  S.busy = true; $("#send").disabled = true;
  const pendingFiles = S.pending; S.pending = []; renderThumbs();
  $("#input").value = "";
  try {
    let uploaded = [];
    if (pendingFiles.length) {
      const fd = new FormData();
      pendingFiles.forEach((p) => fd.append("files", p.file));
      const res = await fetch("/api/uploads", { method: "POST", body: fd });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "Upload failed");
      uploaded = await res.json();
    }
    const history = S.chat.filter((m) => m.role === "user" || m.role === "assistant").slice(-10).map((m) => ({ role: m.role, text: m.text }));
    const imgs = uploaded.filter((u) => u.kind !== "document"), docs = uploaded.filter((u) => u.kind === "document");
    S.chat.push({ role: "user", text: text || "(attachments)", images: imgs.map((u) => u.url), files: docs.map((u) => u.name) });
    renderChat();
    const data = await getJSON("/api/chat", {
      template_id: S.tid, state: S.state, message: text, image_ids: imgs.map((u) => u.id), file_ids: docs.map((u) => u.id), history,
    });
    S.state = data.state;
    S.chat.push({ role: "assistant", text: data.reply || "Done." });
    save();
    await refresh(data.changed || []);
  } catch (err) {
    S.chat.push({ role: "error", text: `Something went wrong: ${err.message}` });
    if (!text) S.pending = pendingFiles; else $("#input").value = $("#input").value || text;
  } finally {
    S.busy = false; $("#send").disabled = false;
    renderChat(); save();
  }
});

// ---------------------------------------------------------------- toolbar
$("#new-doc").addEventListener("click", () => {
  if (!confirm("Clear this form and the conversation?")) return;
  S.state = emptyState();
  S.chat = []; greet(); save(); renderChat(); refresh();
});

async function download(kind) {
  const req = S.missing.filter((m) => m.required);
  if (req.length && !confirm(`Still empty: ${req.map((m) => m.label).join(", ")}.\nDownload anyway? Empty fields print as blank lines.`)) return;
  if (kind === "pdf" && !S.health.pdf_export) {
    toast("PDF export needs LibreOffice on the server; using your browser's Save as PDF instead.");
    setTimeout(() => window.print(), 400);
    return;
  }
  const btn = kind === "pdf" ? $("#dl-pdf") : $("#dl-docx");
  btn.disabled = true;
  try {
    const res = await api(`/api/export/${kind}`, { template_id: S.tid, state: S.state });
    const blob = await res.blob();
    const cd = res.headers.get("Content-Disposition") || "";
    const m = /filename\*=UTF-8''([^;]+)/.exec(cd) || /filename="([^"]+)"/.exec(cd);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = m ? decodeURIComponent(m[1]) : `document.${kind}`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  } catch (err) {
    toast(`Export failed: ${err.message}`);
  } finally {
    btn.disabled = false;
  }
}
$("#dl-docx").addEventListener("click", () => download("docx"));
$("#dl-pdf").addEventListener("click", () => download("pdf"));

init().catch((err) => toast(`Could not start: ${err.message}`));
