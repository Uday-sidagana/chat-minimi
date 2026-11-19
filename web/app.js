"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const safeURL = (s) => {
  try {
    const u = new URL(s);
    return ["https:", "http:"].includes(u.protocol) ? u : null;
  } catch {
    return null;
  }
};
const md = window.markdownit({ html: false, linkify: true, breaks: true });
md.validateLink = (s) => Boolean(safeURL(s));
md.renderer.rules.image = (tokens, i) => `[image: ${esc(tokens[i].content)}]`;
const baseLink =
  md.renderer.rules.link_open ||
  ((tokens, i, options, env, self) => self.renderToken(tokens, i, options));
md.renderer.rules.link_open = (tokens, i, options, env, self) => {
  tokens[i].attrSet("target", "_blank");
  tokens[i].attrSet("rel", "noopener noreferrer");
  return baseLink(tokens, i, options, env, self);
};
let selected = null,
  rows = [],
  busy = false,
  controller = null,
  current = null,
  memoryData = null,
  pinnedTab = false,
  memoryTimer = null;
let zone =
  localStorage.getItem("minimi-zone") ||
  Intl.DateTimeFormat().resolvedOptions().timeZone ||
  "UTC";
let requestKey = null,
  lastText = null;
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (response.status === 204) return null;
  const data = await response.json();
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : "Please check your input and try again.",
    );
  return data;
}
function error(text = "") {
  $("#error").textContent = text;
  $("#error").classList.toggle("hidden", !text);
}
function datetime(value, full = false) {
  try {
    return new Intl.DateTimeFormat("en", {
      timeZone: zone,
      ...(full
        ? { dateStyle: "medium", timeStyle: "short" }
        : {
            month: "short",
            day: "numeric",
            hour: "numeric",
            minute: "2-digit",
          }),
    }).format(new Date(value));
  } catch {
    return value;
  }
}
function bottom() {
  const el = $("#scroll-area");
  el.scrollTop = el.scrollHeight;
}
function nearBottom() {
  const el = $("#scroll-area");
  return el.scrollHeight - el.scrollTop - el.clientHeight < 130;
}
function answerHTML(text, citations = [], memories = []) {
  let result = text || "";
  const slots = [];
  const sources = [];
  const byURL = new Map();
  for (const c of citations) {
    if (!safeURL(c.url)) continue;
    if (!byURL.has(c.url)) {
      byURL.set(c.url, sources.length + 1);
      sources.push(c);
    }
  }
  let points = Array.from(result);
  const replacements = [
    ...citations,
    ...memories.map((r, i) => ({ ...r, memoryIndex: i + 1 })),
  ]
    .filter(
      (c) =>
        (safeURL(c.url) || c.memoryIndex) &&
        Number.isInteger(c.start) &&
        Number.isInteger(c.end) &&
        c.end > c.start &&
        c.end <= points.length,
    )
    .sort((a, b) => b.start - a.start);
  let boundary = Infinity;
  for (const c of replacements) {
    if (c.end > boundary) continue;
    boundary = c.start;
    const token = `MINIMICITATION${slots.length}END`;
    slots.push([
      token,
      c.memoryIndex
        ? `<a class="citation memory-citation" href="#" data-recall-chat="${esc(c.chat_id)}" data-recall-message="${esc(c.id)}" title="${esc(datetime(c.asked_at_utc, true))} · ${esc(c.content.slice(0, 120))}">M${c.memoryIndex}</a>`
        : `<a class="citation" href="${esc(c.url)}" target="_blank" rel="noopener noreferrer" title="${esc(c.title)}">${byURL.get(c.url)}</a>`,
    ]);
    points = [
      ...points.slice(0, c.start),
      ...Array.from(token),
      ...points.slice(c.end),
    ];
  }
  result = points
    .join("")
    .replace(/[^]*/g, "")
    .replace(/\[(?:M:[^\]\n]*|M\d+)(?:\]|$)/g, "");
  let html = md.render(result);
  for (const [token, anchor] of slots) html = html.replaceAll(token, anchor);
  return { html, sources };
}
function details(meta) {
  const seconds = (n) => (typeof n === "number" ? n.toFixed(2) + "s" : "—");
  let html = `<details class="detail-disclosure"><summary>Response details${meta.memory?.length ? " · " + meta.memory.length + " remembered records" : ""}</summary><div class="detail-content">First token: ${seconds(meta.first_token_seconds)} · Total: ${seconds(meta.total_seconds)} · Web calls: ${meta.searches?.length || 0}<br>Model: ${esc(meta.model || "—")}${meta.usage?.total_tokens ? " · " + meta.usage.total_tokens + " tokens" : ""}`;
  if (meta.retrieval)
    html += `<br>Memory: ${esc(meta.retrieval.status === "completed" ? "semantic + keyword" : meta.retrieval.status === "date_window" ? "date-window lookup" : meta.retrieval.status === "fallback" ? "keyword fallback (semantic lookup unavailable)" : "keyword")}${typeof meta.retrieval.seconds === "number" ? " · " + seconds(meta.retrieval.seconds) : ""}`;
  for (const r of meta.memory || [])
    html += `<a class="memory-evidence" href="#" data-recall-chat="${esc(r.chat_id)}" data-recall-message="${esc(r.id)}">${esc(r.content.slice(0, 180))}<br><small>${esc(datetime(r.asked_at_utc, true))} · ${esc(zone)}${r.retrieval_reasons?.length ? " · " + esc(r.retrieval_reasons.join(" + ").replaceAll("_", " ")) : ""}</small></a>`;
  return html + "</div></details>";
}
function messageHTML(row) {
  if (row.role === "user")
    return `<article class="message user" id="msg-${esc(row.id)}"><div class="user-bubble">${esc(row.content)}</div><div class="message-meta"><time datetime="${esc(row.created_at)}">${esc(datetime(row.created_at))}</time><button class="pin-message ${row.pinned ? "pinned" : ""}" data-id="${esc(row.id)}" data-pinned="${row.pinned ? "true" : "false"}">${row.pinned ? "Pinned ↗" : "Pin to memory"}</button></div></article>`;
  const meta = row.metadata || {},
    rendered = answerHTML(
      row.content,
      meta.citations || [],
      meta.memory_citations || [],
    );
  const status =
    row.status === "completed"
      ? ""
      : `<div class="stream-status ${row.status === "failed" ? "status-failed" : ""}">${row.status === "streaming" ? '<span class="pulse"></span>' : ""}${esc(row.status === "streaming" ? "Answer in progress" : row.status === "interrupted" ? "Interrupted — partial answer" : row.status === "cancelled" ? "Stopped — partial answer" : "Incomplete — " + (meta.error || "please try again"))}</div>`;
  let html = `<article class="message assistant" id="msg-${esc(row.id)}"><div class="message-header"><span class="mini-mark">m</span>minimi</div><div class="answer">${rendered.html}</div>${status}`;
  if (rendered.sources.length)
    html += `<div class="sources">${rendered.sources.map((s, i) => `<a class="source-card" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer" title="${esc(s.title)}"><small>${i + 1}</small><span>${esc(safeURL(s.url).hostname.replace(/^www\./, ""))}</span>↗</a>`).join("")}</div>`;
  if (row.status === "completed" && meta.memory_citations?.length) {
    const unique = [
      ...new Map(meta.memory_citations.map((r) => [r.id, r])).values(),
    ];
    html += `<div class="memory-references" aria-label="Conversation references"><span>From your conversations</span>${unique.map((r) => `<button data-recall-chat="${esc(r.chat_id)}" data-recall-message="${esc(r.id)}" title="${esc(r.content)}">↗ ${esc(datetime(r.asked_at_utc, true))}</button>`).join("")}</div>`;
  }
  if (row.status === "completed")
    html += `<div class="message-meta"><span>${meta.searches?.length ? "Searched the web" : "Answered directly"}</span><time>${esc(datetime(row.created_at))}</time></div>${details(meta)}`;
  return html + "</article>";
}
function bindMessages() {
  document
    .querySelectorAll(".pin-message")
    .forEach(
      (button) =>
        (button.onclick = () =>
          pin(button.dataset.id, button.dataset.pinned !== "true")),
    );
  document.querySelectorAll("[data-recall-chat]").forEach(
    (a) =>
      (a.onclick = (e) => {
        e.preventDefault();
        openChat(a.dataset.recallChat, a.dataset.recallMessage);
      }),
  );
}
function render() {
  const stick = nearBottom();
  $("#welcome").classList.toggle("hidden", rows.length > 0);
  $("#messages").innerHTML = rows.map(messageHTML).join("");
  bindMessages();
  if (stick) bottom();
}
async function chats() {
  const all = await api("/api/chats");
  $("#chats").innerHTML =
    all
      .map(
        (c) =>
          `<button class="chat-link ${c.id === selected ? "active" : ""}" data-id="${esc(c.id)}" title="${esc(c.title)}">${esc(c.title)}</button>`,
      )
      .join("") ||
    '<p class="sidebar-empty">Your next good question starts here.</p>';
  document
    .querySelectorAll(".chat-link")
    .forEach((b) => (b.onclick = () => openChat(b.dataset.id)));
  return all;
}
async function newChat() {
  if (busy) return;
  const c = await api("/api/chats", { method: "POST" });
  selected = c.id;
  localStorage.setItem("minimi-chat", selected);
  rows = [];
  render();
  await chats();
  $("#prompt").focus();
  $("#sidebar").classList.remove("open");
  syncBackdrop();
}
async function openChat(id, mid = null) {
  if (busy) {
    error("Stop the current reply before switching conversations.");
    return;
  }
  try {
    error();
    const c = await api("/api/chats/" + id);
    selected = id;
    rows = c.messages;
    localStorage.setItem("minimi-chat", id);
    render();
    await chats();
    $("#sidebar").classList.remove("open");
    syncBackdrop();
    if (mid) {
      let target = document.getElementById("msg-" + mid);
      while (!target && rows.length) {
        const older = await api("/api/chats/" + id + "?before=" + rows[0].seq);
        if (!older.messages.length) break;
        rows = [...older.messages, ...rows];
        render();
        target = document.getElementById("msg-" + mid);
      }
      target?.scrollIntoView({ block: "center", behavior: "smooth" });
      if (target) {
        target.classList.add("memory-target");
        setTimeout(() => target.classList.remove("memory-target"), 2200);
      }
    } else bottom();
  } catch (e) {
    error(e.message);
  }
}
async function pin(id, value) {
  try {
    await api("/api/messages/" + id, {
      method: "PATCH",
      body: JSON.stringify({ pinned: value }),
    });
    const row = rows.find((r) => r.id === id);
    if (row) row.pinned = value;
    render();
    await loadMemory();
  } catch (e) {
    error(e.message);
  }
}
function syncBackdrop() {
  const side = $("#sidebar").classList.contains("open");
  const memory = !$("#memory-panel").classList.contains("hidden");
  $("#backdrop").classList.toggle(
    "hidden",
    !side && !(memory && innerWidth < 1191),
  );
}
function showMemory(show) {
  $("#memory-panel").classList.toggle("hidden", !show);
  $("#memory-toggle").setAttribute("aria-expanded", String(show));
  syncBackdrop();
  if (show) loadMemory();
}
async function loadMemory() {
  try {
    const p = new URLSearchParams({
      q: $("#memory-search").value,
      timezone: zone,
    });
    if ($("#memory-day").value) p.set("day", $("#memory-day").value);
    memoryData = await api("/api/memory?" + p);
    $("#memory-count").textContent = memoryData.total;
    renderMemory();
  } catch (e) {
    $("#memory-results").textContent = e.message;
  }
}
function renderMemory() {
  if (!memoryData) return;
  const list = pinnedTab ? memoryData.pinned : memoryData.records;
  const count = pinnedTab ? memoryData.pinned_total : memoryData.total;
  $("#memory-results").innerHTML = list.length
    ? `<div class="memory-group-label">${list.length} OF ${count} ${pinnedTab ? "PINNED NOTES" : "QUESTIONS"} · ${esc(zone)}</div>` +
      list
        .map(
          (r) =>
            `<article class="memory-card"><time>${esc(datetime(r.asked_at_utc, true))}</time><button class="quote" data-chat="${esc(r.chat_id)}" data-message="${esc(r.id)}">${esc(r.content.slice(0, 260))}${r.content.length > 260 ? "…" : ""}</button><button class="pin ${r.pinned ? "active" : ""}" data-id="${esc(r.id)}" data-value="${r.pinned ? "false" : "true"}">${r.pinned ? "↗ Pinned · unpin" : "＋ Pin this question"}</button></article>`,
        )
        .join("")
    : `<p class="memory-empty">${pinnedTab ? "Pin a question or start a message with “Remember that…” to keep important context close." : "No matching questions yet. Your conversations will appear here with their original timestamps."}</p>`;
  document.querySelectorAll(".memory-card .quote").forEach(
    (b) =>
      (b.onclick = () => {
        showMemory(false);
        openChat(b.dataset.chat, b.dataset.message);
      }),
  );
  document
    .querySelectorAll(".memory-card .pin")
    .forEach(
      (b) => (b.onclick = () => pin(b.dataset.id, b.dataset.value === "true")),
    );
}
function setBusy(value) {
  busy = value;
  $("#send").classList.toggle("hidden", value);
  $("#stop").classList.toggle("hidden", !value);
  $("#new-chat").disabled = value;
  $("#web-mode").disabled = value;
  $("#timezone").disabled = value;
}
function streamRow() {
  return rows.find((r) => r.id === current);
}
function processFrame(frame) {
  const lines = frame.split("\n").filter((line) => line.startsWith("data: "));
  if (!lines.length) return;
  const item = JSON.parse(lines.map((line) => line.slice(6)).join("\n"));
  const row = streamRow();
  if (item.type === "accepted") {
    const user = rows.find((r) => r.id === "pending-user");
    if (user) user.id = item.user_id;
    if (row) {
      row.id = item.assistant_id;
      current = item.assistant_id;
    }
    render();
  } else if (item.type === "delta" && row) {
    row.content += item.text;
    render();
  } else if (item.type === "status") {
    const el = document.querySelector(
      `#msg-${CSS.escape(current)} .stream-status`,
    );
    if (el) el.innerHTML = '<span class="pulse"></span>' + esc(item.text);
  } else if (item.type === "memory") {
    $("#memory-hint").classList.toggle("hidden", !item.records.length);
    $("#memory-hint").textContent = item.records.length
      ? `Bringing ${item.records.length} timestamped memory record${item.records.length === 1 ? "" : "s"} into this reply.`
      : "";
  } else if (item.type === "done" && item.message) {
    const i = rows.findIndex((r) => r.id === current);
    if (i >= 0) rows[i] = item.message;
    render();
  } else if (item.type === "error") {
    if (row) {
      row.status = "failed";
      row.metadata = { ...row.metadata, error: item.message };
      render();
    }
    error(item.message);
  }
}
$("#composer").onsubmit = async (e) => {
  e.preventDefault();
  if (busy) return;
  const text = $("#prompt").value.trim();
  if (!text) return;
  error();
  if (!selected) await newChat();
  if (!selected) return;
  const signature = JSON.stringify([
    selected,
    text,
    zone,
    $("#web-mode").value,
  ]);
  if (lastText !== signature) {
    requestKey = crypto.randomUUID();
    lastText = signature;
  }
  controller = new AbortController();
  setBusy(true);
  current = "pending-assistant";
  const now = new Date().toISOString();
  rows.push(
    {
      id: "pending-user",
      role: "user",
      content: text,
      created_at: now,
      pinned: false,
      status: "completed",
    },
    {
      id: current,
      role: "assistant",
      content: "",
      created_at: now,
      status: "streaming",
      metadata: {},
    },
  );
  render();
  bottom();
  $("#prompt").value = "";
  $("#prompt").style.height = "auto";
  try {
    const response = await fetch("/api/chats/" + selected + "/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        request_id: requestKey,
        timezone: zone,
        web: $("#web-mode").value,
      }),
      signal: controller.signal,
    });
    if (!response.ok) {
      const body = await response.json();
      throw new Error(
        typeof body.detail === "string" ? body.detail : "Request rejected.",
      );
    }
    const reader = response.body.getReader(),
      decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = buffer.replace(/\r\n/g, "\n");
      let split;
      while ((split = buffer.indexOf("\n\n")) >= 0) {
        processFrame(buffer.slice(0, split));
        buffer = buffer.slice(split + 2);
      }
    }
    if (streamRow()?.status === "streaming") {
      streamRow().status = "interrupted";
      render();
      error("The stream ended early. Your saved conversation can be reopened.");
    }
    requestKey = null;
    lastText = null;
  } catch (e) {
    const row = streamRow();
    if (row && e.name === "AbortError") {
      row.status = "cancelled";
      render();
    } else {
      if (row) {
        row.status = "failed";
        row.metadata = { error: e.message };
        render();
      }
      error(e.message);
    }
    if (rows.find((r) => r.id === "pending-user")) {
      $("#prompt").value = text;
      rows = rows.filter((r) => !r.id.startsWith("pending-"));
      render();
    }
  } finally {
    setBusy(false);
    controller = null;
    try {
      if (selected) {
        const canonical = await api("/api/chats/" + selected);
        rows = canonical.messages;
        render();
      }
      await chats();
      await loadMemory();
    } catch {}
    $("#prompt").focus();
  }
};
$("#stop").onclick = () => controller?.abort();
$("#prompt").oninput = () => {
  $("#prompt").style.height = "auto";
  $("#prompt").style.height = Math.min($("#prompt").scrollHeight, 175) + "px";
};
$("#prompt").onkeydown = (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    if (!busy) $("#composer").requestSubmit();
  }
};
$("#new-chat").onclick = () => newChat().catch((e) => error(e.message));
document.querySelectorAll("[data-prompt]").forEach(
  (b) =>
    (b.onclick = () => {
      $("#prompt").value = b.dataset.prompt;
      $("#prompt").focus();
    }),
);
$("#memory-toggle").onclick = () =>
  showMemory($("#memory-panel").classList.contains("hidden"));
$("#memory-close").onclick = () => showMemory(false);
$("#menu").onclick = () => {
  $("#sidebar").classList.toggle("open");
  syncBackdrop();
};
$("#backdrop").onclick = () => {
  showMemory(false);
  $("#sidebar").classList.remove("open");
  syncBackdrop();
};
window.addEventListener("resize", syncBackdrop);
$("#timeline-tab").onclick = () => {
  pinnedTab = false;
  $("#timeline-tab").classList.add("active");
  $("#pinned-tab").classList.remove("active");
  renderMemory();
};
$("#pinned-tab").onclick = () => {
  pinnedTab = true;
  $("#pinned-tab").classList.add("active");
  $("#timeline-tab").classList.remove("active");
  renderMemory();
};
$("#memory-search").oninput = () => {
  clearTimeout(memoryTimer);
  memoryTimer = setTimeout(loadMemory, 250);
};
$("#memory-day").onchange = loadMemory;
$("#clear-date").onclick = () => {
  $("#memory-day").value = "";
  loadMemory();
};
$("#forget").onclick = async () => {
  if (
    !confirm(
      "Delete all conversations and pinned memory in this workspace? This cannot be undone.",
    )
  )
    return;
  controller?.abort();
  try {
    await api("/api/data", { method: "DELETE" });
    selected = null;
    rows = [];
    localStorage.removeItem("minimi-chat");
    $("#memory-hint").classList.add("hidden");
    render();
    await chats();
    await loadMemory();
  } catch (e) {
    error(e.message);
  }
};
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    showMemory(false);
    $("#sidebar").classList.remove("open");
    syncBackdrop();
  }
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "o") {
    e.preventDefault();
    newChat();
  }
});
(async () => {
  try {
    const zones = [
      ...new Set([
        zone,
        "UTC",
        "Asia/Kolkata",
        "America/New_York",
        "Europe/London",
        "Asia/Tokyo",
        "Australia/Sydney",
      ]),
    ];
    $("#timezone").innerHTML = zones
      .map(
        (z) =>
          `<option value="${esc(z)}"${z === zone ? " selected" : ""}>${esc(z.replaceAll("_", " "))}</option>`,
      )
      .join("");
    $("#timezone").onchange = () => {
      zone = $("#timezone").value;
      localStorage.setItem("minimi-zone", zone);
      render();
      loadMemory();
    };
    const session = await api("/api/session");
    $("#model-label").textContent = session.model;
    if (!session.configured) {
      $("#setup-notice").classList.remove("hidden");
      $("#setup-notice").textContent =
        "The interface and memory are ready. Add OPENAI_API_KEY to the server environment to enable answers.";
      $("#send").disabled = true;
    }
    const all = await chats();
    const saved = localStorage.getItem("minimi-chat");
    if (all.some((c) => c.id === saved)) await openChat(saved);
    await loadMemory();
  } catch (e) {
    error(e.message);
    $("#model-label").textContent = "Setup needed";
  }
})();
