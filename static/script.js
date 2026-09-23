const TOKEN_KEY = "rag_token";
let token = localStorage.getItem(TOKEN_KEY) || "";
let docs = [];
let selectedIds = new Set(); // doc_ids currently included in chat
let instructions = "";

// Must match the DISCLAIMER string in main.py.
const DISCLAIMER =
  "context not found in your document — AI-generated, please double-check.";

const $ = (id) => document.getElementById(id);

function escapeText(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

let toastTimer = null;
function toast(message, kind = "") {
  const t = $("toast");
  t.textContent = message;
  t.className = `toast ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 2600);
}

// Adds the JWT header and drops back to login on any 401.
async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      ...(options.headers || {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
  });
  if (res.status === 401 && token) {
    logout(false);
    throw new Error("Session expired — please log in again.");
  }
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, status: res.status, data };
}

function showAuth() {
  $("app-view").classList.add("hidden");
  $("auth-view").classList.remove("hidden");
}

function showApp() {
  $("auth-view").classList.add("hidden");
  $("app-view").classList.remove("hidden");
}

function logout(clearToken = true) {
  if (clearToken) localStorage.removeItem(TOKEN_KEY);
  token = "";
  docs = [];
  selectedIds.clear();
  instructions = "";
  showAuth();
}

let authMode = "login";

function setAuthMode(mode) {
  authMode = mode;
  $("tab-login").classList.toggle("active", mode === "login");
  $("tab-signup").classList.toggle("active", mode === "signup");
  $("auth-submit").textContent = mode === "login" ? "Log in" : "Create account";
  $("auth-error").textContent = "";
}

$("tab-login").addEventListener("click", () => setAuthMode("login"));
$("tab-signup").addEventListener("click", () => setAuthMode("signup"));

$("auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const email = $("auth-email").value.trim();
  const password = $("auth-password").value;
  const errEl = $("auth-error");
  errEl.textContent = "";

  $("auth-submit").disabled = true;
  try {
    const { ok, data } = await api(`/api/auth/${authMode}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    if (!ok) {
      errEl.textContent = data.detail || "Something went wrong.";
      return;
    }
    token = data.token;
    localStorage.setItem(TOKEN_KEY, token);
    await enterApp(data.email);
  } catch {
    errEl.textContent = "Cannot reach the server — is it running?";
  } finally {
    $("auth-submit").disabled = false;
  }
});

async function enterApp(email) {
  showApp();
  $("user-email").textContent = email;

  const [docsRes, prefsRes] = await Promise.all([
    api("/api/documents"),
    api("/api/preferences"),
  ]);

  docs = docsRes.ok ? docsRes.data : [];
  instructions = prefsRes.ok ? prefsRes.data.custom_instructions : "";
  $("instructions-input").value = instructions;

  if (docs.length && selectedIds.size === 0) selectedIds.add(docs[0].doc_id);

  renderDocs();
  updateChatScope();

  const win = $("chat-window");
  win.innerHTML = "";
  if (docs.length === 0) {
    addMessage("bot",
      `Welcome! 👋 Upload a document (PDF, TXT or MD) using the button on the ` +
      `left — then ask me anything about it.`);
  } else {
    addMessage("bot",
      `Welcome back! 👋 You have <b>${docs.length}</b> document` +
      `${docs.length > 1 ? "s" : ""}. Pick one (or several) in the sidebar ` +
      `and ask away.`);
  }
}

function formatDate(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

function renderDocs() {
  const list = $("doc-list");
  list.innerHTML = "";
  $("doc-count").textContent = docs.length ? `(${docs.length})` : "";

  if (docs.length === 0) {
    list.innerHTML = `<div class="doc-empty">No documents yet.<br>Upload a PDF, TXT or MD file to get started.</div>`;
    return;
  }

  for (const doc of docs) {
    const item = document.createElement("div");
    item.className = "doc-item" + (selectedIds.has(doc.doc_id) ? " selected" : "");
    item.innerHTML = `
      <div class="doc-check">✓</div>
      <div class="doc-meta">
        <div class="doc-name" title="${escapeText(doc.filename)}">${escapeText(doc.filename)}</div>
        <div class="doc-sub">${formatDate(doc.upload_date)} · ${doc.chunks} chunks</div>
      </div>
      <button class="doc-delete" title="Delete document">🗑</button>`;

    item.addEventListener("click", (e) => {
      if (e.target.classList.contains("doc-delete")) return;
      toggleSelect(doc.doc_id);
    });
    item.querySelector(".doc-delete").addEventListener("click", () => deleteDoc(doc));
    list.appendChild(item);
  }
  updateChatScope();
}

function toggleSelect(docId) {
  selectedIds.has(docId) ? selectedIds.delete(docId) : selectedIds.add(docId);
  renderDocs();
}

async function deleteDoc(doc) {
  if (!confirm(`Delete "${doc.filename}"? Its vector data is removed too.`)) return;
  const { ok, data } = await api(`/api/documents/${doc.doc_id}`, { method: "DELETE" });
  if (!ok) { toast(data.detail || "Delete failed", "error"); return; }
  docs = docs.filter((d) => d.doc_id !== doc.doc_id);
  selectedIds.delete(doc.doc_id);
  renderDocs();
  toast("Document deleted", "ok");
}

async function uploadFile(file) {
  const list = $("doc-list");

  // Temporary card with an indeterminate progress bar.
  const busy = document.createElement("div");
  busy.className = "doc-item uploading";
  busy.innerHTML = `
    <div class="doc-check">…</div>
    <div class="doc-meta">
      <div class="doc-name">${escapeText(file.name)}</div>
      <div class="doc-sub">chunking + embedding…</div>
      <div class="upload-bar"></div>
    </div>`;
  list.prepend(busy);

  try {
    const body = new FormData();
    body.append("file", file);
    const { ok, data } = await api("/api/documents", { method: "POST", body });
    if (!ok) throw new Error(data.detail || "Upload failed");

    docs.unshift(data);            // newest first, same as the API
    selectedIds.add(data.doc_id);  // auto-select the fresh upload
    renderDocs();
    toast(`✅ ${data.filename} — ${data.chunks} chunks indexed`, "ok");
  } catch (err) {
    busy.remove();
    toast(err.message.includes("reach") ? err.message : `❌ ${err.message}`, "error");
  }
}

$("upload-btn").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", async () => {
  const files = [...$("file-input").files];
  $("file-input").value = ""; // allow re-uploading the same file
  for (const f of files) await uploadFile(f); // sequential: keeps rows tidy
});

$("sidebar-toggle").addEventListener("click", () =>
  $("sidebar").classList.toggle("open"));

function addMessage(kind, html) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${kind}`;
  wrap.innerHTML = `<div class="bubble">${html}</div>`;
  $("chat-window").appendChild(wrap);
  $("chat-window").scrollTop = $("chat-window").scrollHeight;
  return wrap;
}

function updateChatScope() {
  const el = $("chat-scope");
  const picked = docs.filter((d) => selectedIds.has(d.doc_id));
  el.innerHTML = picked.length
    ? `Chatting against: <b>${picked.map((d) => escapeText(d.filename)).join("</b>, <b>")}</b>`
    : "No document selected — tick one in the sidebar";
}

$("chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("chat-input");
  const question = input.value.trim();
  if (!question) return;

  if (selectedIds.size === 0) {
    addMessage("bot",
      `Select at least one document in the sidebar first (tick its checkbox) — ` +
      `I only answer from <b>your</b> files.`);
    return;
  }

  addMessage("user", escapeText(question));
  input.value = "";
  $("send-btn").disabled = true;

  const typing = addMessage("bot",
    `<span class="typing"><span class="d"></span><span class="d"></span><span class="d"></span></span>`);

  try {
    const { ok, status, data } = await api("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, document_ids: [...selectedIds] }),
    });

    if (status === 400 || status === 403) {
      typing.querySelector(".bubble").textContent = data.detail || "Request rejected.";
      return;
    }
    if (!ok) throw new Error(data.detail || `Server error (${status})`);

    let html = escapeText(data.answer);
    if (data.grounded) {
      html += `<div class="source-tag">✓ source: ${escapeText(data.source)}</div>`;
    } else {
      // Lift the disclaimer out of the bubble into the compact banner.
      const answer = data.answer.includes(DISCLAIMER)
        ? data.answer.replace(DISCLAIMER, "").trim()
        : data.answer;
      html = `${escapeText(answer)}<div class="warning-banner">${escapeText(DISCLAIMER)}</div>`;
    }
    typing.querySelector(".bubble").innerHTML = html;
  } catch (err) {
    typing.querySelector(".bubble").innerHTML = `❌ ${escapeText(err.message)}`;
  } finally {
    $("send-btn").disabled = false;
    input.focus();
  }
});

function openSettings() {
  $("instructions-input").value = instructions;
  $("settings-backdrop").classList.remove("hidden");
  const panel = $("settings-panel");
  panel.classList.remove("closed");
  panel.setAttribute("aria-hidden", "false");
  setTimeout(() => $("instructions-input").focus(), 300); // wait for the slide
}

function closeSettings() {
  $("settings-backdrop").classList.add("hidden");
  const panel = $("settings-panel");
  panel.classList.add("closed");
  panel.setAttribute("aria-hidden", "true");
}

$("settings-btn").addEventListener("click", openSettings);
$("settings-close").addEventListener("click", closeSettings);
$("settings-backdrop").addEventListener("click", closeSettings);

$("settings-save").addEventListener("click", async () => {
  const text = $("instructions-input").value.trim();
  const { ok, data } = await api("/api/preferences", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ custom_instructions: text }),
  });
  if (!ok) { toast(data.detail || "Could not save", "error"); return; }
  instructions = data.custom_instructions;
  closeSettings();
  toast(instructions ? "Instructions saved ✓" : "Instructions cleared", "ok");
});

$("logout-btn").addEventListener("click", () => logout());

(async function init() {
  setAuthMode("login");
  if (!token) { showAuth(); return; }
  try {
    const { ok, data } = await api("/api/me");
    if (!ok) return; // api() already logged us out on 401
    await enterApp(data.email);
  } catch {
    // network down — auth view is already shown by logout()
  }
})();
