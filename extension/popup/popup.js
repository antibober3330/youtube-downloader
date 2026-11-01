const $ = (id) => document.getElementById(id);
const result = $("result");

function show(text, cls) {
  result.textContent = text;
  result.className = "result " + (cls || "");
}

function send(msg) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(msg, (res) => {
      if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
      else resolve(res || { ok: false, error: "Порожня відповідь." });
    });
  });
}

async function init() {
  const { token } = await chrome.storage.local.get("token");
  $("token").value = token || "";
  $("ext-id").textContent = chrome.runtime.id;
}

$("toggle").addEventListener("click", () => {
  const f = $("token");
  f.type = f.type === "password" ? "text" : "password";
});

$("save").addEventListener("click", async () => {
  const value = $("token").value.trim();
  if (!value) {
    show("Токен порожній. Скопіюйте його з вікна сервера.", "err");
    return;
  }
  await chrome.storage.local.set({ token: value });
  show("Токен збережено. Натисніть «Перевірити з’єднання».", "ok");
});

$("check").addEventListener("click", async () => {
  show("Перевіряю…", "wait");
  const res = await send({ type: "health" });
  if (!res.ok) {
    show("❌ Сервер недоступний. Запустіть youtube_downloader.py (іконка в треї).", "err");
  } else if (res.tokenOk === false) {
    show("✅ Сервер працює, але: " + (res.error || "токен не підійшов."), "err");
  } else {
    show("✅ Сервер працює", "ok");
  }
});

$("folder").addEventListener("click", async () => {
  const res = await send({ type: "openFolder", body: {} });
  if (res.ok) show("Папку відкрито.", "ok");
  else show(res.error || "Не вдалося відкрити папку.", "err");
});

$("copy-id").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(chrome.runtime.id);
    show("ID скопійовано.", "ok");
  } catch (_) {
    show("Не вдалося скопіювати. Виділіть ID вручну.", "err");
  }
});

init();
