// Інжекція кнопок «Завантажити» на сторінки YouTube / YouTube Music (SPA-навігація).
(function () {
  if (window.__ytdlInjected) return;
  window.__ytdlInjected = true;

  const BTN_ID = "ytdl-page-button";
  const IS_MUSIC = location.hostname === "music.youtube.com";

  const TARGETS = {
    video: IS_MUSIC
      ? ["ytmusic-player-bar .middle-controls .content-info-wrapper", "ytmusic-player-bar .middle-controls"]
      : [
          "ytd-watch-metadata #actions #top-level-buttons-computed",
          "#actions #top-level-buttons-computed",
          "ytd-watch-metadata #actions-inner #menu",
          "ytd-watch-metadata #actions",
        ],
    playlist: IS_MUSIC
      ? [
          "ytmusic-responsive-header-renderer .buttons",
          "ytmusic-responsive-header-renderer .action-buttons",
          "ytmusic-detail-header-renderer .buttons",
        ]
      : [
          "ytd-playlist-header-renderer #top-level-buttons-computed",
          "ytd-playlist-header-renderer .metadata-action-bar",
          "yt-page-header-view-model yt-flexible-actions-view-model",
          "ytd-playlist-header-renderer #title",
        ],
  };

  function pageInfo() {
    const p = new URLSearchParams(location.search);
    const origin = location.origin;
    if (location.pathname === "/watch" && p.get("v")) {
      return { mode: "video", url: origin + "/watch?v=" + encodeURIComponent(p.get("v")), key: "v:" + p.get("v") };
    }
    if (location.pathname === "/playlist" && p.get("list")) {
      return { mode: "playlist", url: origin + "/playlist?list=" + encodeURIComponent(p.get("list")), key: "l:" + p.get("list") };
    }
    return null;
  }

  function pageTitle(mode) {
    let t = "";
    if (mode === "video" && IS_MUSIC) {
      const el = document.querySelector("ytmusic-player-bar .title");
      t = el ? el.textContent.trim() : "";
    } else if (mode === "playlist") {
      const el = document.querySelector("ytd-playlist-header-renderer h1, yt-page-header-view-model h1, ytmusic-responsive-header-renderer h2, ytmusic-detail-header-renderer h2");
      t = el ? el.textContent.trim() : "";
    }
    if (!t) t = document.title.replace(/^\(\d+\)\s*/, "").replace(/\s*-\s*YouTube( Music)?$/, "").trim();
    return t;
  }

  function toast(text) {
    const old = document.querySelector(".ytdl-toast");
    if (old) old.remove();
    const el = document.createElement("div");
    el.className = "ytdl-toast";
    el.textContent = text;
    document.body.append(el);
    setTimeout(() => el.remove(), 6000);
  }

  function makeButton(info) {
    const b = document.createElement("button");
    b.id = BTN_ID;
    b.type = "button";
    b.className = "ytdl-btn-page";
    b.dataset.ytdlKey = info.key;
    b.textContent = info.mode === "playlist" ? "⬇ Завантажити плейлист" : "⬇ Завантажити";
    b.title = "Завантажити через YouTube Downloader";
    b.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const cur = pageInfo() || info;
      window.YTDLMenu.open(b, {
        mode: cur.mode,
        url: cur.url,
        title: pageTitle(cur.mode),
        onQueued: (res) => toast(res.panelOpened
          ? "Додано в чергу. Прогрес — у бічній панелі розширення."
          : "Додано в чергу. Прогрес дивіться в розширенні (іконка в панелі Chrome)."),
      });
    });
    return b;
  }

  function findTarget(mode) {
    for (const sel of TARGETS[mode]) {
      const el = document.querySelector(sel);
      if (el && el.offsetParent !== null) return el;
    }
    return null;
  }

  let floatingSince = 0;

  function ensureButton() {
    if (!chrome.runtime || !chrome.runtime.id) return;
    const info = pageInfo();
    const existing = document.getElementById(BTN_ID);
    if (!info) {
      if (existing) existing.remove();
      window.YTDLMenu.close();
      floatingSince = 0;
      return;
    }
    if (existing && existing.dataset.ytdlKey !== info.key) {
      existing.remove();
      window.YTDLMenu.close();
    } else if (existing && existing.isConnected) {
      const floating = existing.classList.contains("ytdl-floating");
      // Якщо кнопка плаває, а рідне місце вже з'явилося, переносимо її
      if (!floating || !findTarget(info.mode)) return;
      existing.remove();
    }
    const btn = makeButton(info);
    const target = findTarget(info.mode);
    if (target) {
      target.append(btn);
      floatingSince = 0;
    } else {
      // Рідне місце ще не з'явилося: даємо сторінці 4 с, потім показуємо плаваючу кнопку
      if (!floatingSince) floatingSince = Date.now();
      if (Date.now() - floatingSince < 4000) return;
      btn.classList.add("ytdl-floating");
      document.body.append(btn);
    }
  }

  let timer = null;
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(ensureButton, 300);
  }

  window.addEventListener("yt-navigate-finish", () => { floatingSince = 0; schedule(); });
  window.addEventListener("popstate", schedule);
  new MutationObserver(schedule).observe(document.documentElement, { childList: true, subtree: true });
  setInterval(ensureButton, 2000);
  ensureButton();
})();
