// Випадаюче меню вибору формату і якості. Доступне як window.YTDLMenu.
(function () {
  const VIDEO_Q = ["360", "480", "720", "1080", "1440", "2160"];
  const AUDIO_Q = ["128", "192", "256", "320"];
  const BASE_DEFAULTS = {
    type: "video", videoQuality: "1080", audioQuality: "320", subtitles: false,
    metadata: true, normalize: true, skipExisting: true, limitMode: "all",
  };
  const TIME_RE = /^\d{1,2}(:\d{1,2}){1,2}$/;

  function h(tag, props, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (v === true) el.setAttribute(k, "");
      else if (v !== false && v != null) el.setAttribute(k, v);
    }
    for (const c of children.flat()) if (c != null) el.append(c);
    return el;
  }

  let current = null;

  function close() {
    if (!current) return;
    document.removeEventListener("mousedown", current.onOutside, true);
    document.removeEventListener("keydown", current.onKey, true);
    window.removeEventListener("resize", close);
    current.root.remove();
    current = null;
  }

  async function loadDefaults() {
    try {
      const { menuDefaults } = await chrome.storage.local.get("menuDefaults");
      return { ...BASE_DEFAULTS, ...(menuDefaults || {}) };
    } catch (_) {
      return { ...BASE_DEFAULTS };
    }
  }

  function saveDefaults(d) {
    try { chrome.storage.local.set({ menuDefaults: d }); } catch (_) { /* ігноруємо */ }
  }

  function checkbox(id, label, checked) {
    const input = h("input", { type: "checkbox", id, checked });
    return { input, row: h("label", { class: "ytdl-check", for: id }, input, h("span", { text: label })) };
  }

  async function open(anchor, opts) {
    close();
    const isPlaylist = opts.mode === "playlist";
    const d = await loadDefaults();

    const typeVideo = h("input", { type: "radio", name: "ytdl-type", value: "video", checked: d.type === "video" });
    const typeAudio = h("input", { type: "radio", name: "ytdl-type", value: "audio", checked: d.type === "audio" });
    const quality = h("select", { class: "ytdl-select" });

    function fillQuality() {
      const audio = typeAudio.checked;
      const list = audio ? AUDIO_Q : VIDEO_Q;
      const want = audio ? d.audioQuality : d.videoQuality;
      quality.replaceChildren(...list.map((q) =>
        h("option", { value: q, selected: q === want, text: audio ? q + " kbps" : q + "p" })));
      subs.input.disabled = audio;
    }

    const trimOn = checkbox("ytdl-trim", isPlaylist ? "Обрізати кожен елемент" : "Обрізати", false);
    const trimStart = h("input", { class: "ytdl-input", type: "text", placeholder: "00:00", "aria-label": "Початок" });
    const trimEnd = h("input", { class: "ytdl-input", type: "text", placeholder: "05:00", "aria-label": "Кінець" });
    const trimFields = h("div", { class: "ytdl-row ytdl-hidden" },
      h("label", { class: "ytdl-field" }, "Початок", trimStart),
      h("label", { class: "ytdl-field" }, "Кінець", trimEnd));
    trimOn.input.addEventListener("change", () => trimFields.classList.toggle("ytdl-hidden", !trimOn.input.checked));

    const subs = checkbox("ytdl-subs", "Субтитри окремим файлом .srt", d.subtitles);
    const meta = checkbox("ytdl-meta", "Вбудовувати метадані (назва, автор, обкладинка)", d.metadata);
    const norm = checkbox("ytdl-norm", "Нормалізація гучності", d.normalize);
    const skip = checkbox("ytdl-skip", "Пропускати вже завантажені", d.skipExisting);

    // Режим обмеження плейлиста
    const limitMode = h("select", { class: "ytdl-select" },
      h("option", { value: "all", text: "Усі", selected: d.limitMode === "all" }),
      h("option", { value: "first", text: "Перші N", selected: d.limitMode === "first" }),
      h("option", { value: "range", text: "Діапазон (від–до)", selected: d.limitMode === "range" }));
    const limitN = h("input", { class: "ytdl-input", type: "number", min: "1", max: "5000", value: "20", "aria-label": "N" });
    const rangeFrom = h("input", { class: "ytdl-input", type: "number", min: "1", value: "1", "aria-label": "Від" });
    const rangeTo = h("input", { class: "ytdl-input", type: "number", min: "1", value: "10", "aria-label": "До" });
    const firstRow = h("div", { class: "ytdl-row" }, h("label", { class: "ytdl-field" }, "Кількість", limitN));
    const rangeRow = h("div", { class: "ytdl-row" },
      h("label", { class: "ytdl-field" }, "Від", rangeFrom),
      h("label", { class: "ytdl-field" }, "До", rangeTo));
    function syncLimit() {
      firstRow.classList.toggle("ytdl-hidden", limitMode.value !== "first");
      rangeRow.classList.toggle("ytdl-hidden", limitMode.value !== "range");
    }
    limitMode.addEventListener("change", syncLimit);

    const status = h("div", { class: "ytdl-status", role: "status" });
    const submit = h("button", { class: "ytdl-btn ytdl-btn-primary", type: "button", text: "Завантажити" });
    const cancel = h("button", { class: "ytdl-btn", type: "button", text: "Закрити", onclick: close });

    const optionsBox = h("details", { class: "ytdl-options" },
      h("summary", { text: "Опції" }),
      trimOn.row, trimFields, subs.row, meta.row, norm.row);

    const root = h("div", { class: "ytdl-menu", role: "dialog", "aria-label": "Завантаження" },
      h("div", { class: "ytdl-menu-title", text: isPlaylist ? "Завантажити плейлист" : "Завантажити" }),
      opts.title ? h("div", { class: "ytdl-menu-sub", text: opts.title }) : null,
      h("div", { class: "ytdl-group" },
        h("div", { class: "ytdl-label", text: "Тип" }),
        h("div", { class: "ytdl-row" },
          h("label", { class: "ytdl-radio" }, typeVideo, h("span", { text: "Відео (MP4)" })),
          h("label", { class: "ytdl-radio" }, typeAudio, h("span", { text: "Аудіо (MP3)" })))),
      h("div", { class: "ytdl-group" }, h("div", { class: "ytdl-label", text: "Якість" }), quality),
      isPlaylist ? h("div", { class: "ytdl-group" },
        h("div", { class: "ytdl-label", text: "Що завантажувати" }), limitMode, firstRow, rangeRow) : null,
      isPlaylist ? skip.row : null,
      optionsBox,
      status,
      h("div", { class: "ytdl-actions" }, cancel, submit));

    typeVideo.addEventListener("change", fillQuality);
    typeAudio.addEventListener("change", fillQuality);
    fillQuality();
    syncLimit();

    function setStatus(text, kind) {
      status.textContent = text || "";
      status.className = "ytdl-status" + (kind ? " ytdl-" + kind : "");
    }

    function collect() {
      const audio = typeAudio.checked;
      const payload = {
        url: opts.url,
        kind: (isPlaylist ? "playlist_" : "") + (audio ? "audio" : "video"),
        quality: quality.value,
        playlist_items: null,
        trim: null,
        subtitles: !audio && subs.input.checked,
        metadata: meta.input.checked,
        normalize: norm.input.checked,
        skip_existing: isPlaylist ? skip.input.checked : true,
        _title: opts.title || "",
      };
      if (trimOn.input.checked) {
        const s = trimStart.value.trim() || "0:00";
        const e = trimEnd.value.trim();
        if (!TIME_RE.test(s) || !TIME_RE.test(e)) {
          throw new Error("Вкажіть початок і кінець у форматі мм:сс або гг:мм:сс.");
        }
        payload.trim = { start: s, end: e };
      }
      if (isPlaylist) {
        if (limitMode.value === "first") {
          const n = parseInt(limitN.value, 10);
          if (!(n >= 1 && n <= 5000)) throw new Error("Кількість має бути від 1 до 5000.");
          payload.playlist_items = { limit: n };
        } else if (limitMode.value === "range") {
          const a = parseInt(rangeFrom.value, 10);
          const b = parseInt(rangeTo.value, 10);
          if (!(a >= 1 && b >= a)) throw new Error("Діапазон: «від» не може бути більшим за «до».");
          payload.playlist_items = a + "-" + b;
        }
      }
      return payload;
    }

    submit.addEventListener("click", () => {
      let payload;
      try {
        payload = collect();
      } catch (e) {
        setStatus(e.message, "error");
        return;
      }
      saveDefaults({
        ...d,
        type: typeAudio.checked ? "audio" : "video",
        videoQuality: typeAudio.checked ? d.videoQuality : quality.value,
        audioQuality: typeAudio.checked ? quality.value : d.audioQuality,
        subtitles: subs.input.checked, metadata: meta.input.checked, normalize: norm.input.checked,
        skipExisting: isPlaylist ? skip.input.checked : d.skipExisting,
        limitMode: isPlaylist ? limitMode.value : d.limitMode,
      });
      submit.disabled = true;
      submit.textContent = "Надсилаю…";
      setStatus("Надсилаю завдання на сервер…", "info");
      // sendMessage викликається прямо в обробнику кліку, щоб сторінка могла відкрити бічну панель
      chrome.runtime.sendMessage({ type: "download", payload, openPanel: true }, (res) => {
        submit.disabled = false;
        submit.textContent = "Завантажити";
        if (chrome.runtime.lastError || !res) {
          setStatus("Розширення було оновлено. Перезавантажте сторінку.", "error");
          return;
        }
        if (!res.ok) {
          setStatus(res.error || "Не вдалося поставити завдання в чергу.", "error");
          return;
        }
        close();
        if (opts.onQueued) opts.onQueued(res);
      });
    });

    document.body.append(root);
    const r = anchor.getBoundingClientRect();
    const mw = root.offsetWidth || 340;
    const mh = root.offsetHeight || 400;
    let left = Math.min(Math.max(8, r.left), window.innerWidth - mw - 8);
    let top = r.bottom + 6;
    if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 6);
    root.style.left = left + "px";
    root.style.top = top + "px";

    const onOutside = (ev) => {
      if (!root.contains(ev.target) && !anchor.contains(ev.target)) close();
    };
    const onKey = (ev) => { if (ev.key === "Escape") close(); };
    document.addEventListener("mousedown", onOutside, true);
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", close);
    current = { root, onOutside, onKey };
  }

  window.YTDLMenu = { open, close, h };
})();
