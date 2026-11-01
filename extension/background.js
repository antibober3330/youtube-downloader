// Service worker: проксі запитів до локального сервера, cookies і SSE-з'єднання.
const API = "http://127.0.0.1:8765";
const MAX_TASKS = 30;
const MAX_COOKIE_HEADER = 60000;

class ApiError extends Error {
  constructor(message, code, status) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

// ---------------------------------------------------------------------------
// Стан завдань (зберігається в chrome.storage.session, щоб пережити сплячку SW)
// ---------------------------------------------------------------------------
let tasks = {};
let connected = false;
const lastBroadcast = {};

const ready = (async () => {
  try {
    const { tasks: saved } = await chrome.storage.session.get("tasks");
    if (saved) tasks = saved;
  } catch (_) { /* storage.session недоступний */ }
})();

let persistTimer = null;
function persist() {
  clearTimeout(persistTimer);
  persistTimer = setTimeout(() => {
    const ids = Object.keys(tasks)
      .sort((a, b) => (tasks[b].createdAt || 0) - (tasks[a].createdAt || 0))
      .slice(0, MAX_TASKS);
    const trimmed = {};
    ids.forEach((id) => (trimmed[id] = tasks[id]));
    tasks = trimmed;
    chrome.storage.session.set({ tasks }).catch(() => {});
  }, 400);
}

function isActive(t) {
  return !t.hidden && !t.finished && (t.status === "queued" || t.status === "running");
}

function updateBadge() {
  const n = Object.values(tasks).filter(isActive).length;
  chrome.action.setBadgeText({ text: n ? String(n) : "" }).catch(() => {});
  chrome.action.setBadgeBackgroundColor({ color: "#d32f2f" }).catch(() => {});
}

function broadcast(msg) {
  chrome.runtime.sendMessage(msg).catch(() => {});
}

function notifyTask(t, force) {
  const now = Date.now();
  if (!force && now - (lastBroadcast[t.id] || 0) < 250) return;
  lastBroadcast[t.id] = now;
  broadcast({ type: "task", task: t });
  persist();
  updateBadge();
}

function ensureTask(id) {
  if (!tasks[id]) {
    tasks[id] = {
      id, title: "", kind: "", url: "", payload: null, status: "queued", finished: false,
      counts: { ok: 0, skipped: 0, failed: 0 }, current: null, message: "", errors: [],
      pending: null, lastFile: null, createdAt: Date.now() / 1000,
    };
  }
  return tasks[id];
}

function normStatus(s) {
  return s === "finished" ? "done" : s;
}

// ---------------------------------------------------------------------------
// Токен, cookies, запити
// ---------------------------------------------------------------------------
async function getToken() {
  const { token } = await chrome.storage.local.get("token");
  return (token || "").trim();
}

// Cookies YouTube із поточної сесії; розширення їх не зберігає, лише передає серверу.
async function getCookieHeader() {
  try {
    const list = await chrome.cookies.getAll({ domain: ".youtube.com" });
    const compact = list
      .map((c) => ({ name: c.name, value: c.value, domain: c.domain, path: c.path, secure: c.secure }))
      .filter((c) => !/[^\x20-\x7e]/.test(c.name + c.value));
    const s = JSON.stringify(compact);
    if (!compact.length || s.length > MAX_COOKIE_HEADER) return null;
    return s;
  } catch (_) {
    return null;
  }
}

async function api(path, { method = "GET", body, cookies = false } = {}) {
  const token = await getToken();
  if (!token) {
    throw new ApiError(
      "Токен не вказано. Відкрийте налаштування розширення (іконка в панелі Chrome) і вставте токен із вікна сервера.",
      "no_token"
    );
  }
  const headers = { Authorization: "Bearer " + token };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (cookies) {
    const c = await getCookieHeader();
    if (c) headers["X-YTDL-Cookies"] = c;
  }
  let res;
  try {
    res = await fetch(API + path, {
      method, headers, body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (_) {
    throw new ApiError("Сервер не запущено. Запустіть youtube_downloader.py і спробуйте ще раз.", "offline");
  }
  let data = null;
  try { data = await res.json(); } catch (_) { /* порожнє тіло */ }
  if (!res.ok) {
    let msg = (data && data.error) || "Помилка сервера (" + res.status + ").";
    let code = "http";
    if (res.status === 401) {
      msg = "Невірний токен. Скопіюйте токен із вікна сервера в налаштування розширення.";
      code = "unauthorized";
    } else if (res.status === 403) {
      msg = "Сервер не дозволив це розширення. Додайте його ID (" + chrome.runtime.id +
        ") до allowed_extension_ids у config.json і перезапустіть сервер.";
      code = "forbidden";
    } else if (res.status === 429) {
      msg = "Забагато запитів. Зачекайте кілька секунд і повторіть.";
      code = "rate";
    }
    throw new ApiError(msg, code, res.status);
  }
  return data;
}

// ---------------------------------------------------------------------------
// Синхронізація з /api/queue
// ---------------------------------------------------------------------------
async function syncQueue() {
  const data = await api("/api/queue");
  for (const s of data.tasks || []) {
    const t = ensureTask(s.task_id);
    t.title = s.title || t.title;
    t.kind = s.kind || t.kind;
    t.url = s.url || t.url;
    t.createdAt = s.created || t.createdAt;
    t.counts = s.counts || t.counts;
    t.message = s.message || t.message;
    const st = normStatus(s.status);
    if (["done", "error", "cancelled"].includes(st)) {
      t.finished = true;
      t.status = st;
      t.current = null;
    } else if (!t.finished) {
      t.status = st;
    }
    if (t.status === "running" && Array.isArray(s.items)) {
      const run = s.items.find((i) => i.status && !["queued", "done", "ok", "skipped", "failed"].includes(i.status));
      if (run && !t.current) t.current = { item: run.title, percent: run.percent || 0, stage: "download" };
    }
  }
  persist();
  updateBadge();
}

// ---------------------------------------------------------------------------
// SSE
// ---------------------------------------------------------------------------
let sseRunning = false;
let sseAbort = null;

function setConnected(v) {
  if (connected !== v) {
    connected = v;
    broadcast({ type: "connection", connected });
  }
}

function handleEvent(event, d) {
  if (!d || !d.task_id) return;
  const t = ensureTask(d.task_id);
  if (event === "progress") {
    if (!t.finished) t.status = "running";
    t.current = { item: d.item, percent: d.percent, speed: d.speed, eta: d.eta, stage: d.stage };
    if (d.item && !t.title) t.title = d.item;
    notifyTask(t, false);
  } else if (event === "done") {
    if (d.final) {
      t.finished = true;
      t.status = normStatus(d.status || "done");
      t.counts = d.counts || t.counts;
      t.message = d.message || t.message;
      t.current = null;
    } else {
      if (d.status === "skipped") t.counts.skipped += 1;
      else t.counts.ok += 1;
      t.lastFile = d.file || t.lastFile;
      t.current = null;
    }
    notifyTask(t, true);
  } else if (event === "error") {
    if (d.kind === "quality") {
      t.pending = { type: "quality", requested: d.requested, available: d.available || [] };
    } else if (d.kind === "space") {
      t.pending = { type: "space", message: d.message };
    } else {
      t.errors.push({ kind: d.kind, message: d.message });
      if (t.errors.length > 20) t.errors.shift();
      if (["permanent", "transient", "setup"].includes(d.kind)) t.counts.failed += 1;
    }
    notifyTask(t, true);
  }
}

function parseSSE(block) {
  let event = "message";
  const data = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trim());
  }
  if (!data.length) return;
  try { handleEvent(event, JSON.parse(data.join("\n"))); } catch (_) { /* пошкоджена подія */ }
}

async function connectSSE() {
  if (sseRunning) return;
  sseRunning = true;
  try {
    while (true) {
      const token = await getToken();
      if (!token) break;
      let fatal = false;
      try {
        sseAbort = new AbortController();
        const res = await fetch(API + "/api/events", {
          headers: { Authorization: "Bearer " + token }, signal: sseAbort.signal,
        });
        if (!res.ok) {
          fatal = res.status === 401 || res.status === 403;
          throw new Error("HTTP " + res.status);
        }
        setConnected(true);
        syncQueue().catch(() => {});
        const reader = res.body.getReader();
        const dec = new TextDecoder("utf-8");
        let buf = "";
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true }).replace(/\r\n/g, "\n");
          let idx;
          while ((idx = buf.indexOf("\n\n")) >= 0) {
            parseSSE(buf.slice(0, idx));
            buf = buf.slice(idx + 2);
          }
        }
      } catch (_) { /* розрив з'єднання */ }
      setConnected(false);
      if (fatal) break;
      await new Promise((r) => setTimeout(r, 3000));
    }
  } finally {
    sseRunning = false;
  }
}

function restartSSE() {
  if (sseAbort) sseAbort.abort();
  setTimeout(() => connectSSE(), 200);
}

// ---------------------------------------------------------------------------
// Дії
// ---------------------------------------------------------------------------
async function submitDownload(payload) {
  const { _title, ...body } = payload;
  const res = await api("/api/download", { method: "POST", body, cookies: true });
  const t = ensureTask(res.task_id);
  t.payload = body;
  t.kind = body.kind;
  t.url = body.url;
  t.title = _title || t.title || body.url;
  t.createdAt = Date.now() / 1000;
  notifyTask(t, true);
  connectSSE();
  return res.task_id;
}

async function handleMessage(msg, sender) {
  await ready;
  switch (msg.type) {
    case "download": {
      const id = await submitDownload(msg.payload);
      return { ok: true, task_id: id };
    }
    case "info":
      return { ok: true, info: await api("/api/info?url=" + encodeURIComponent(msg.url), { cookies: true }) };
    case "health": {
      const out = { ok: false, tokenOk: null, error: null };
      try {
        const res = await fetch(API + "/api/health");
        const j = await res.json();
        out.ok = res.ok && j.status === "ok";
        out.info = j;
      } catch (_) {
        return out;
      }
      try {
        await api("/api/queue");
        out.tokenOk = true;
      } catch (e) {
        out.tokenOk = false;
        out.error = e.message;
      }
      return out;
    }
    case "getState":
      try { await syncQueue(); } catch (_) { /* показуємо кеш */ }
      connectSSE();
      return { tasks: Object.values(tasks).filter((t) => !t.hidden), connected };
    case "cancel":
      await api("/api/queue/" + encodeURIComponent(msg.id), { method: "DELETE" });
      return { ok: true };
    case "openFolder":
      return { ok: true, ...(await api("/api/open-folder", { method: "POST", body: msg.body || {} })) };
    case "resolvePending": {
      const t = tasks[msg.id];
      if (!t || !t.pending || !t.payload) return { ok: false, error: "Завдання вже неактуальне." };
      const next = { ...t.payload };
      if (t.pending.type === "quality") next.quality = String(msg.value);
      else next.ignore_space_check = true;
      t.pending = null;
      notifyTask(t, true);
      const id = await submitDownload({ ...next, _title: t.title });
      return { ok: true, task_id: id };
    }
    case "dismiss": {
      const t = tasks[msg.id];
      if (t) {
        if (msg.what === "pending") t.pending = null;
        else t.hidden = true;
        persist();
        broadcast({ type: "state" });
      }
      return { ok: true };
    }
    case "clearFinished":
      for (const id of Object.keys(tasks)) if (tasks[id].finished && !tasks[id].pending) tasks[id].hidden = true;
      persist();
      updateBadge();
      return { ok: true };
    case "reconnect":
      restartSSE();
      return { ok: true };
    default:
      return { ok: false, error: "Невідома команда." };
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || !msg.type || msg.type === "task" || msg.type === "connection" || msg.type === "state") return false;
  let panel = null;
  // Панель треба відкривати синхронно, у межах жесту користувача.
  if (msg.openPanel && sender.tab && chrome.sidePanel && chrome.sidePanel.open) {
    panel = chrome.sidePanel.open({ tabId: sender.tab.id }).then(() => true, () => false);
  }
  handleMessage(msg, sender)
    .then(async (r) => sendResponse({ ...r, panelOpened: panel ? await panel : false }))
    .catch((e) => sendResponse({ ok: false, error: e.message, code: e.code }));
  return true;
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.token) restartSSE();
});

chrome.alarms.create("ytdl-keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(async (a) => {
  if (a.name !== "ytdl-keepalive") return;
  await ready;
  if (await getToken()) connectSSE();
});

chrome.runtime.onStartup.addListener(() => connectSSE());
chrome.runtime.onInstalled.addListener(() => connectSSE());
ready.then(() => { updateBadge(); connectSSE(); });
