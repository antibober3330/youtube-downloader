// Панель прогресу: працює і в бічній панелі Chrome, і всередині popup (запасний варіант).
(function () {
  const root = document.getElementById("ytdl-panel");
  if (!root) return;

  const STAGES = {
    download: "завантажується", convert: "конвертується", normalize: "нормалізація гучності",
    waiting: "пауза між елементами", blocked: "YouTube обмежує запити, пауза",
  };
  const STATUS = {
    queued: "у черзі", running: "завантажується", done: "готово", error: "помилка", cancelled: "скасовано",
  };

  let tasks = {};
  let connected = false;
  let banner = "";

  function h(tag, props, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (v != null && v !== false) el.setAttribute(k, v);
    }
    for (const c of children.flat()) if (c != null) el.append(c);
    return el;
  }

  function send(msg) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage(msg, (res) => {
        if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
        else resolve(res || { ok: false, error: "Порожня відповідь." });
      });
    });
  }

  async function act(msg) {
    const res = await send(msg);
    banner = res.ok === false ? res.error || "Не вдалося виконати дію." : "";
    if (msg.type === "dismiss" || msg.type === "clearFinished" || msg.type === "resolvePending") await refresh();
    render();
  }

  function activeStatus(t) {
    return !t.finished && (t.status === "queued" || t.status === "running");
  }

  function renderCard(t) {
    const status = t.status || "queued";
    const card = h("div", { class: "yp-card" });
    card.append(
      h("div", { class: "yp-card-title", text: t.title || t.url || t.id }),
      h("div", { class: "yp-meta" },
        h("span", { class: "yp-badge " + status, text: STATUS[status] || status }),
        " ", t.kind ? ({ video: "Відео", audio: "Аудіо", playlist_video: "Плейлист (відео)", playlist_audio: "Плейлист (аудіо)" }[t.kind] || t.kind) : ""));

    if (t.current && activeStatus(t)) {
      const pct = Math.max(0, Math.min(100, Number(t.current.percent) || 0));
      const stage = STAGES[t.current.stage] || t.current.stage || "";
      const bits = [];
      if (t.current.percent != null && t.current.stage === "download") bits.push(pct.toFixed(1) + "%");
      if (t.current.speed) bits.push(t.current.speed);
      if (t.current.eta) bits.push("залишилось " + t.current.eta);
      card.append(
        h("div", { class: "yp-item", text: t.current.item || "", title: t.current.item || "" }),
        h("div", { class: "yp-bar" }, h("div", { style: "width:" + pct + "%" })),
        h("div", { class: "yp-stats", text: [stage, ...bits].filter(Boolean).join(" · ") }));
    } else if (status === "queued") {
      card.append(h("div", { class: "yp-stats", text: "Очікує своєї черги…" }));
    }

    if (t.pending && t.pending.type === "quality") {
      const avail = t.pending.available || [];
      const box = h("div", { class: "yp-pending" });
      if (avail.length) {
        box.append(h("div", { text: "Обраної якості (" + t.pending.requested + "p) немає. Доступно: " + avail.join(", ") + ". Обрати?" }));
        box.append(h("div", { class: "yp-actions" },
          avail.map((q) => h("button", { class: "yp-btn primary", text: q + "p", onclick: () => act({ type: "resolvePending", id: t.id, value: q }) })),
          h("button", { class: "yp-btn", text: "Скасувати", onclick: () => act({ type: "dismiss", id: t.id, what: "pending" }) })));
      } else {
        box.append(h("div", { text: "Обраної якості немає, а доступних варіантів не знайдено." }));
        box.append(h("div", { class: "yp-actions" },
          h("button", { class: "yp-btn", text: "Закрити", onclick: () => act({ type: "dismiss", id: t.id, what: "pending" }) })));
      }
      card.append(box);
    } else if (t.pending && t.pending.type === "space") {
      card.append(h("div", { class: "yp-pending" },
        h("div", { text: (t.pending.message || "Може не вистачити місця на диску.") + " Продовжити все одно?" }),
        h("div", { class: "yp-actions" },
          h("button", { class: "yp-btn primary", text: "Продовжити", onclick: () => act({ type: "resolvePending", id: t.id }) }),
          h("button", { class: "yp-btn", text: "Скасувати", onclick: () => act({ type: "dismiss", id: t.id, what: "pending" }) }))));
    }

    const c = t.counts || { ok: 0, skipped: 0, failed: 0 };
    if (t.finished || c.ok || c.skipped || c.failed) {
      card.append(h("div", { class: "yp-stats", text: "Завантажено: " + c.ok + ", Пропущено: " + c.skipped + ", Помилок: " + c.failed }));
    }
    if (t.message && t.finished && status !== "done") card.append(h("div", { class: "yp-stats", text: t.message }));

    const errs = (t.errors || []).slice(-3);
    if (errs.length) card.append(h("ul", { class: "yp-notes" }, errs.map((e) => h("li", { text: e.message }))));

    const actions = h("div", { class: "yp-actions" });
    if (activeStatus(t)) {
      actions.append(h("button", { class: "yp-btn", text: "Скасувати", onclick: () => act({ type: "cancel", id: t.id }) }));
    } else {
      const audio = (t.kind || "").endsWith("audio");
      const body = t.lastFile ? { file: t.lastFile } : { kind: audio ? "audio" : "video" };
      actions.append(
        h("button", { class: "yp-btn primary", text: "Відкрити папку", onclick: () => act({ type: "openFolder", body }) }),
        h("button", { class: "yp-btn", text: "Прибрати", onclick: () => act({ type: "dismiss", id: t.id }) }));
    }
    card.append(actions);
    return card;
  }

  function render() {
    const list = Object.values(tasks).filter((t) => !t.hidden).sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
    const finished = list.some((t) => t.finished && !t.pending);
    root.replaceChildren(h("div", { class: "yp-wrap" },
      h("div", { class: "yp-head" },
        h("div", { class: "yp-title", text: "Завантаження" }),
        h("div", { class: "yp-conn" + (connected ? " ok" : ""), text: connected ? "● на зв'язку" : "○ немає зв'язку" })),
      banner ? h("div", { class: "yp-error-banner", text: banner }) : null,
      finished ? h("div", { class: "yp-toolbar" },
        h("button", { class: "yp-btn", text: "Очистити завершені", onclick: () => act({ type: "clearFinished" }) })) : null,
      list.length ? list.map(renderCard)
        : h("div", { class: "yp-empty", text: "Поки що немає завдань. Відкрийте відео або плейлист на YouTube й натисніть «Завантажити»." })));
  }

  async function refresh() {
    const res = await send({ type: "getState" });
    if (res.tasks) {
      tasks = {};
      res.tasks.forEach((t) => (tasks[t.id] = t));
      connected = !!res.connected;
      banner = "";
    } else {
      banner = res.error || "";
    }
  }

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === "task" && msg.task) {
      tasks[msg.task.id] = msg.task;
      render();
    } else if (msg.type === "connection") {
      connected = !!msg.connected;
      render();
    } else if (msg.type === "state") {
      refresh().then(render);
    }
  });

  render();
  refresh().then(render);
})();
