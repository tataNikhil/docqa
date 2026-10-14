// DocQA front end: talks to the backend at /server/docqa_api/...
// The browser never decides who the user is - the backend reads that from
// Catalyst's login cookie on every request.

const API = "/server/docqa_api";
const LOGIN_URL = "/__catalyst/auth/login";

const state = {
    documents: [],           // [{id, file_name, summary, suggested_questions, ...}]
    conversations: [],       // [{id, title}]
    conversationId: null,    // the open chat, or null for a new one
    selectedDocIds: new Set(),
    asking: false,
};

const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------------ talking to the backend

function csrfHeader() {
    // Catalyst checks this header on cookie-authenticated requests to block
    // other websites from making requests in the user's name (CSRF).
    const match = document.cookie.match(/(?:^|;\s*)ZD_CSRF_TOKEN=([^;]+)/);
    return match ? { "X-ZCSRF-TOKEN": "zd_csrparam=" + match[1] } : {};
}

async function api(path, options = {}) {
    const response = await fetch(API + path, {
        credentials: "same-origin",
        ...options,
        headers: { ...csrfHeader(), ...(options.headers || {}) },
    });
    if (response.status === 401) {
        window.location.href = LOGIN_URL;
        throw new Error("Please log in.");
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(data.error || `Request failed (${response.status})`);
    }
    return data;
}

function toast(message) {
    const el = $("toast");
    el.textContent = message;
    el.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => (el.hidden = true), 4000);
}

// Never insert user or AI text as HTML: escape it first, so a document that
// contains "<script>" is shown as text instead of being run.
function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}

// ------------------------------------------------------------------ documents

function renderDocuments() {
    const list = $("doc-list");
    list.innerHTML = "";
    if (!state.documents.length) {
        list.innerHTML = '<li class="muted small">No documents yet.</li>';
    }
    for (const doc of state.documents) {
        const li = document.createElement("li");
        li.innerHTML = `
            <div class="doc-head">
                <input type="checkbox" ${state.selectedDocIds.has(doc.id) ? "checked" : ""} title="Ask only selected documents">
                <span class="doc-name">${escapeHtml(doc.file_name)}</span>
                <button class="icon-btn" title="Delete document">✕</button>
            </div>
            ${doc.summary ? `<div class="doc-summary muted small">${escapeHtml(doc.summary)}</div>` : ""}`;

        li.querySelector("input").addEventListener("change", (e) => {
            if (e.target.checked) state.selectedDocIds.add(doc.id);
            else state.selectedDocIds.delete(doc.id);
            renderScope();
            renderSuggestions();
        });
        li.querySelector(".icon-btn").addEventListener("click", () => deleteDocument(doc));
        list.appendChild(li);
    }
    renderScope();
}

async function loadDocuments() {
    state.documents = await api("/documents");
    const ids = new Set(state.documents.map((d) => d.id));
    state.selectedDocIds = new Set([...state.selectedDocIds].filter((id) => ids.has(id)));
    renderDocuments();
}

// Uploads go one at a time: each file is read, chunked and summarised by the
// backend before the next one starts, so no single request runs too long.
async function uploadFiles(fileList) {
    const files = [...fileList];
    if (!files.length) return;

    const zone = $("drop-zone");
    const original = $("drop-text").innerHTML;
    zone.classList.add("busy");

    const ready = [];
    const failed = [];
    for (const [i, file] of files.entries()) {
        $("drop-text").textContent =
            `Reading ${i + 1} of ${files.length}: “${file.name}”… this can take a few seconds`;
        if (file.size > 5 * 1024 * 1024) {
            failed.push(`${file.name}: larger than 5 MB`);
            continue;
        }
        try {
            const form = new FormData();
            form.append("file", file);
            const doc = await api("/documents", { method: "POST", body: form });
            state.documents.unshift(doc);
            ready.push(doc);
            renderDocuments();
        } catch (err) {
            failed.push(`${file.name}: ${err.message}`);
        }
    }

    zone.classList.remove("busy");
    $("drop-text").innerHTML = original;
    $("file-input").value = "";

    if (ready.length) renderSuggestions(ready[ready.length - 1].suggested_questions);
    if (failed.length) {
        toast(`${ready.length} uploaded, ${failed.length} failed — ${failed.join("; ")}`);
    } else {
        toast(ready.length === 1
            ? `“${ready[0].file_name}” is ready. Ask away!`
            : `All ${ready.length} documents are ready. Ask away!`);
    }
}

async function deleteDocument(doc) {
    if (!confirm(`Delete “${doc.file_name}”? This removes the file and everything learned from it.`)) return;
    try {
        await api(`/documents/${doc.id}`, { method: "DELETE" });
        state.documents = state.documents.filter((d) => d.id !== doc.id);
        state.selectedDocIds.delete(doc.id);
        renderDocuments();
        renderSuggestions();
    } catch (err) {
        toast(err.message);
    }
}

function renderScope() {
    const selected = state.documents.filter((d) => state.selectedDocIds.has(d.id));
    $("scope").textContent = selected.length
        ? "Searching only: " + selected.map((d) => d.file_name).join(", ")
        : "Searching all your documents";
}

// Suggested questions: from a given list, or gathered from the (selected) documents.
function renderSuggestions(questions) {
    const box = $("suggestions");
    box.innerHTML = "";
    if (!questions) {
        const pool = state.selectedDocIds.size
            ? state.documents.filter((d) => state.selectedDocIds.has(d.id))
            : state.documents;
        questions = pool.flatMap((d) => d.suggested_questions.slice(0, 2)).slice(0, 4);
    }
    // Only offer suggestions at the start of a chat.
    if (state.conversationId || $("messages").querySelector(".msg")) return;
    for (const q of questions) {
        const btn = document.createElement("button");
        btn.textContent = q;
        btn.addEventListener("click", () => askQuestion(q));
        box.appendChild(btn);
    }
}

// ------------------------------------------------------------------ conversations

function renderConversations() {
    const list = $("conv-list");
    list.innerHTML = "";
    if (!state.conversations.length) {
        list.innerHTML = '<li class="muted small">No conversations yet.</li>';
    }
    for (const conv of state.conversations) {
        const li = document.createElement("li");
        if (conv.id === state.conversationId) li.classList.add("active");
        li.innerHTML = `
            <button class="conv-title" title="${escapeHtml(conv.title)}">${escapeHtml(conv.title)}</button>
            <button class="icon-btn" title="Delete conversation">✕</button>`;
        li.querySelector(".conv-title").addEventListener("click", () => openConversation(conv.id));
        li.querySelector(".icon-btn").addEventListener("click", () => deleteConversation(conv));
        list.appendChild(li);
    }
}

async function loadConversations() {
    state.conversations = await api("/conversations");
    renderConversations();
}

function newChat() {
    state.conversationId = null;
    $("messages").innerHTML = `<div class="empty">
        <p><strong>Ask anything about your documents.</strong></p>
        <p class="small">Answers come only from your own files, with numbered citations you can click.</p>
    </div>`;
    closeSource();
    renderConversations();
    renderSuggestions();
    $("question").focus();
}

async function openConversation(id) {
    state.conversationId = id;
    renderConversations();
    $("suggestions").innerHTML = "";
    $("messages").innerHTML = '<div class="empty">Loading…</div>';
    try {
        const messages = await api(`/conversations/${id}/messages`);
        $("messages").innerHTML = "";
        messages.forEach(addMessage);
    } catch (err) {
        toast(err.message);
    }
}

async function deleteConversation(conv) {
    if (!confirm(`Delete the conversation “${conv.title}”?`)) return;
    try {
        await api(`/conversations/${conv.id}`, { method: "DELETE" });
        state.conversations = state.conversations.filter((c) => c.id !== conv.id);
        if (state.conversationId === conv.id) newChat();
        else renderConversations();
    } catch (err) {
        toast(err.message);
    }
}

// ------------------------------------------------------------------ messages

function addMessage(msg) {
    const box = $("messages");
    box.querySelector(".empty")?.remove();

    const div = document.createElement("div");
    div.className = `msg ${msg.role}`;

    if (msg.role === "user") {
        div.textContent = msg.content;
    } else {
        // Escape first, then turn [1], [2]... into clickable citation buttons.
        const sources = msg.sources || [];
        let html = escapeHtml(msg.content).replace(/\n/g, "<br>");
        html = html.replace(/\[(\d+)\]/g, (whole, n) =>
            sources.some((s) => String(s.n) === n)
                ? `<button class="cite" data-n="${n}">${n}</button>`
                : whole);
        if (sources.length) {
            html += '<div class="sources">' + sources.map((s) =>
                `<button class="source-chip cite-chip" data-n="${s.n}">[${s.n}] ${escapeHtml(s.file_name)} · part ${s.chunk_index + 1}</button>`
            ).join("") + "</div>";
        }
        div.innerHTML = html;
        div.querySelectorAll("[data-n]").forEach((btn) => {
            btn.addEventListener("click", () => {
                const source = sources.find((s) => String(s.n) === btn.dataset.n);
                if (source) showSource(source);
            });
        });
    }
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
    return div;
}

async function askQuestion(question) {
    question = question.trim();
    if (!question || state.asking) return;
    if (!state.documents.length) {
        toast("Upload a document first.");
        return;
    }

    state.asking = true;
    $("ask-btn").disabled = true;
    $("question").value = "";
    $("suggestions").innerHTML = "";

    addMessage({ role: "user", content: question });
    const pending = addMessage({ role: "assistant", content: "Searching your documents…" });
    pending.classList.add("pending");

    try {
        const result = await api("/ask", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                question,
                conversation_id: state.conversationId,
                document_ids: [...state.selectedDocIds],
            }),
        });
        pending.remove();
        addMessage(result.answer);

        if (state.conversationId !== result.conversation_id) {
            state.conversationId = result.conversation_id;
            await loadConversations();
        }
    } catch (err) {
        pending.remove();
        toast(err.message);
    } finally {
        state.asking = false;
        $("ask-btn").disabled = false;
        $("question").focus();
    }
}

// ------------------------------------------------------------------ citation panel

function showSource(source) {
    $("source-title").textContent = `Source [${source.n}]`;
    $("source-meta").textContent = `${source.file_name} · part ${source.chunk_index + 1}`;
    $("source-text").textContent = source.text;
    $("source-panel").hidden = false;
}

function closeSource() {
    $("source-panel").hidden = true;
}

// ------------------------------------------------------------------ start-up

function signOut() {
    const loginPage = `${location.protocol}//${location.host}${LOGIN_URL}`;
    if (window.catalyst?.auth?.signOut) catalyst.auth.signOut(loginPage);
    else window.location.href = loginPage;
}

function wireEvents() {
    $("file-input").addEventListener("change", (e) => uploadFiles(e.target.files));

    const zone = $("drop-zone");
    zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("dragging"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("dragging"));
    zone.addEventListener("drop", (e) => {
        e.preventDefault();
        zone.classList.remove("dragging");
        uploadFiles(e.dataTransfer.files);
    });

    $("ask-form").addEventListener("submit", (e) => {
        e.preventDefault();
        askQuestion($("question").value);
    });
    // Enter sends, Shift+Enter makes a new line.
    $("question").addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            askQuestion($("question").value);
        }
    });

    $("new-chat").addEventListener("click", newChat);
    $("close-source").addEventListener("click", closeSource);
    $("sign-out").addEventListener("click", signOut);
}

async function start() {
    try {
        const me = await api("/me");   // redirects to the login page if not logged in
        $("user-email").textContent = me.email;
        wireEvents();
        await Promise.all([loadDocuments(), loadConversations()]);
        $("loading").hidden = true;
        $("app").hidden = false;
        newChat();
    } catch (err) {
        $("loading").textContent = err.message;
    }
}

start();
