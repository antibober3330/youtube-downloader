#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YouTube Downloader — локальний сервер (HTTP + SSE), CLI та вікно Tkinter.

Запуск:
    python youtube_downloader.py                 # вікно Tkinter + сервер
    python youtube_downloader.py --server        # лише сервер (без вікна)
    python youtube_downloader.py URL --kind audio --quality 320
    python youtube_downloader.py --help
"""
import argparse
import errno
import hmac
import json
import logging
import os
import queue
import random
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

try:
    import yt_dlp
    from yt_dlp.utils import download_range_func
except Exception:  # yt-dlp може бути не встановлений (--help, --dry-run покажуть це)
    yt_dlp = None
    download_range_func = None

APP_NAME = "YouTube Downloader"
APP_VERSION = "1.0.0"
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
IS_WINDOWS = os.name == "nt"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0

DEFAULT_CONFIG = {
    "server": {
        "host": "127.0.0.1",
        "port": 8765,
        "token": "",
        "allowed_extension_ids": [],
        "max_workers": 1,
        "rate_limit_per_minute": 20,
    },
    "paths": {
        "base_dir": ".",
        "video_dir": "завантажені відео",
        "audio_dir": "завантажені аудіо",
    },
    "defaults": {
        "video_quality": "1080",
        "video_container": "mp4",
        "audio_format": "mp3",
        "audio_quality": "320",
        "subtitles": False,
        "metadata": True,
        "normalize": True,
        "trim": None,
    },
    "cookies": {"file": None, "from_browser": None},
    "pause": {
        "between_items_min": 1.0,
        "between_items_max": 3.0,
        "long_break_every": 25,
        "long_break_min": 30.0,
        "long_break_max": 60.0,
    },
}

KINDS = ("video", "audio", "playlist_video", "playlist_audio")
AUDIO_QUALITIES = ("128", "192", "256", "320")
ALLOWED_HOSTS = ("youtube.com", "youtu.be")
MIN_FILE_SIZE = 1024 * 1024  # 1 МБ: менші файли не вважаються «наявними»
MAX_NAME_LEN = 180
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
YTDLP_MAX_AGE_DAYS = 45

log = logging.getLogger("ytdl")


# --------------------------------------------------------------------------- #
# Конфіг і логування
# --------------------------------------------------------------------------- #
def _deep_merge(base, override):
    out = json.loads(json.dumps(base))
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path=CONFIG_PATH):
    """Читає config.json; створює його (з новим токеном), якщо файлу немає."""
    user = {}
    if path.exists():
        try:
            user = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"config.json пошкоджений: {exc}. Виправте або видаліть файл.")
    cfg = _deep_merge(DEFAULT_CONFIG, user)
    changed = not path.exists()
    if not isinstance(cfg["server"].get("token"), str) or len(cfg["server"]["token"]) < 16:
        cfg["server"]["token"] = secrets.token_urlsafe(24)  # рівно 32 символи
        changed = True
    if changed:
        save_config(cfg, path)
    cfg["server"]["max_workers"] = max(1, min(3, int(cfg["server"].get("max_workers") or 1)))
    return cfg


def save_config(cfg, path=CONFIG_PATH):
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def setup_logging(console=True):
    log.setLevel(logging.DEBUG)
    if log.handlers:
        return
    fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(SCRIPT_DIR / "log.txt", maxBytes=10 * 1024 * 1024,
                             backupCount=2, encoding="utf-8")  # 3 файли по 10 МБ
    fh.setFormatter(fmt)
    fh.setLevel(logging.INFO)
    log.addHandler(fh)
    if console and sys.stderr is not None:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        sh.setLevel(logging.WARNING)
        log.addHandler(sh)


def resolve_dirs(cfg):
    base = Path(cfg["paths"]["base_dir"])
    base = base if base.is_absolute() else (SCRIPT_DIR / base)
    base = base.resolve()

    def sub(name):
        p = Path(cfg["paths"][name])
        return p if p.is_absolute() else base / p

    return base, sub("video_dir"), sub("audio_dir")


def ensure_dirs(cfg):
    base, vdir, adir = resolve_dirs(cfg)
    for d in (base, vdir, adir):
        d.mkdir(parents=True, exist_ok=True)
    return base, vdir, adir


# --------------------------------------------------------------------------- #
# Утиліти
# --------------------------------------------------------------------------- #
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def sanitize_filename(name, max_len=MAX_NAME_LEN):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or ""))
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    if len(name) > max_len:
        cut = name[:max_len]
        if " " in cut and cut[max_len - 1:] != " ":
            head = cut.rsplit(" ", 1)[0]
            if len(head) >= max_len * 0.5:
                cut = head
        name = cut.rstrip(". _-")
    if not name:
        name = "untitled"
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    return name


def is_allowed_url(url):
    try:
        u = urlparse(url)
    except Exception:
        return False
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not host:
        return False
    return any(host == d or host.endswith("." + d) for d in ALLOWED_HOSTS)


_TIME_RE = re.compile(r"^(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:\.\d+)?$")


def parse_time(value):
    """'mm:ss', 'hh:mm:ss' або число секунд -> секунди (float)."""
    s = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    m = _TIME_RE.match(s)
    if not m:
        raise ValueError(f"Невірний формат часу: «{value}». Використовуйте mm:ss або hh:mm:ss.")
    h, mi, se = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
    if mi > 59 or se > 59:
        raise ValueError(f"Невірний час: «{value}».")
    return float(h * 3600 + mi * 60 + se)


def hms_tag(sec):
    sec = int(sec)
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m{sec % 60:02d}s"


def file_ok(path):
    try:
        return path.is_file() and path.stat().st_size > MIN_FILE_SIZE
    except OSError:
        return False


def open_in_explorer(path):
    path = Path(path)
    if IS_WINDOWS:
        if path.is_file():
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(str(path))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path.parent if path.is_file() else path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent if path.is_file() else path)])


def ytdlp_age_days():
    if yt_dlp is None:
        return None
    try:
        y, m, d = (int(x) for x in yt_dlp.version.__version__.split(".")[:3])
        return (date.today() - date(y, m, d)).days
    except Exception:
        return None


def update_ytdlp():
    """pip install -U yt-dlp[default]. Повертає (ok, вивід)."""
    cmd = [sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]"]
    if sys.executable.lower().endswith("pythonw.exe"):
        cmd[0] = str(Path(sys.executable).with_name("python.exe"))
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                             creationflags=NO_WINDOW)
        out = (res.stdout or "") + (res.stderr or "")
        log.info("Оновлення yt-dlp: код %s", res.returncode)
        return res.returncode == 0, out.strip()
    except Exception as exc:
        return False, str(exc)


def check_environment():
    """Список (назва, ok, деталі) для --dry-run і вікна."""
    out = []
    out.append(("yt-dlp", yt_dlp is not None,
                yt_dlp.version.__version__ if yt_dlp else "не встановлено: pip install -U \"yt-dlp[default]\""))
    for tool in ("ffmpeg", "ffprobe", "deno"):
        p = shutil.which(tool)
        out.append((tool, bool(p), p or "не знайдено в PATH"))
    return out


# --------------------------------------------------------------------------- #
# Cookies (передаються розширенням у заголовку X-YTDL-Cookies)
# --------------------------------------------------------------------------- #
def cookies_to_netscape(raw):
    """raw: JSON-рядок: {"name":"value"} або список об'єктів chrome.cookies. -> текст Netscape."""
    data = json.loads(raw)
    if isinstance(data, dict):
        data = [{"name": k, "value": v} for k, v in data.items()]
    lines = ["# Netscape HTTP Cookie File"]
    for c in data:
        if not isinstance(c, dict) or "name" not in c:
            continue
        name, value = str(c["name"]), str(c.get("value", ""))
        domain = str(c.get("domain") or ".youtube.com")
        if not (domain.lstrip(".").endswith("youtube.com") or domain.lstrip(".").endswith("google.com")):
            continue
        if any(ch in name + value + domain for ch in "\t\r\n"):
            continue
        sub = "TRUE" if domain.startswith(".") else "FALSE"
        path = str(c.get("path") or "/")
        secure = "TRUE" if c.get("secure", True) else "FALSE"
        exp = c.get("expirationDate") or c.get("expires") or 0
        try:
            exp = int(float(exp))
        except (TypeError, ValueError):
            exp = 0
        prefix = "#HttpOnly_" if c.get("httpOnly") else ""
        lines.append(f"{prefix}{domain}\t{sub}\t{path}\t{secure}\t{exp}\t{name}\t{value}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Класифікація помилок
# --------------------------------------------------------------------------- #
class Cancelled(Exception):
    pass


class FatalError(Exception):
    pass


class QualityUnavailable(Exception):
    def __init__(self, requested, available):
        self.requested, self.available = requested, available
        super().__init__(f"Обраної якості ({requested}p) немає. Доступно: "
                         f"{', '.join(str(a) for a in available)}.")


_ERR_PATTERNS = [
    ("fatal", r"ffmpeg|ffprobe|avconv"),  # уточнюється нижче (має містити «not found»)
    ("fatal", r"no space left|disk full|\[errno 28\]|permission denied|\[errno 13\]|access is denied"),
    ("permanent", r"video unavailable|private video|this video is private|has been removed|"
                  r"no longer available|age[- ]restricted|confirm your age|inappropriate for some|"
                  r"not available in your country|blocked it in your country|members[- ]only|"
                  r"join this channel|copyright|account .* terminated|does not exist|"
                  r"live event will begin|premieres in|is not a valid url|unsupported url"),
    ("block", r"429|too many requests|captcha|not a bot|sign in to confirm|http error 403|forbidden"),
    ("setup", r"requested format is not available|javascript runtime|js runtime|signature|nsig|"
              r"unable to extract|no video formats|only images are available|po token"),
    ("transient", r"timed out|timeout|connection|network|temporary|unreachable|name resolution|"
                  r"ssl|incomplete read|http error 5\d\d|remote end closed|getaddrinfo|reset by peer"),
]


def classify_error(exc):
    """Повертає одну з: fatal, permanent, block, setup, transient."""
    if isinstance(exc, FatalError):
        return "fatal"
    if isinstance(exc, PermissionError):
        return "fatal"
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.ENOSPC:
        return "fatal"
    msg = str(exc).lower()
    for kind, pattern in _ERR_PATTERNS:
        if re.search(pattern, msg):
            if kind == "fatal" and "ffmpeg|" in pattern:
                if re.search(r"not found|not installed|no such file|is not recognized|"
                             r"could not be found|install", msg):
                    return "fatal"
                continue
            # вікове обмеження важливіше за «sign in»
            return kind
    return "transient"


SETUP_HINTS = ("Підказки: 1) оновіть yt-dlp (кнопка «Оновити yt-dlp»); 2) встановіть Deno "
               "(winget install DenoLand.Deno); 3) перевірте, що FFmpeg є в PATH.")


# --------------------------------------------------------------------------- #
# Шина подій (SSE) і черга
# --------------------------------------------------------------------------- #
class Hub:
    def __init__(self):
        self._subs = set()
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subs.discard(q)

    def publish(self, event, data):
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait((event, data))
            except queue.Full:
                pass  # повільний клієнт не блокує завантаження


class Task:
    def __init__(self, params, cookies_raw=None):
        self.id = str(uuid.uuid4())
        self.params = params
        self.cookies_raw = cookies_raw
        self.status = "queued"  # queued/running/done/error/cancelled
        self.created = datetime.now().isoformat(timespec="seconds")
        self.title = params["url"]
        self.items = []  # [{title,status,percent}]
        self.counts = {"ok": 0, "skipped": 0, "failed": 0}
        self.message = ""
        self.cancel = threading.Event()
        self.finished = threading.Event()
        self.halt_reason = None

    def to_dict(self):
        return {
            "task_id": self.id, "status": self.status, "kind": self.params["kind"],
            "url": self.params["url"], "title": self.title, "created": self.created,
            "counts": dict(self.counts), "message": self.message,
            "items": [dict(i) for i in self.items[-50:]],
        }


def normalize_params(raw, cfg):
    """Перевіряє тіло запиту й підставляє значення за замовчуванням. ValueError -> HTTP 400."""
    if not isinstance(raw, dict):
        raise ValueError("Очікується JSON-об'єкт.")
    url = str(raw.get("url") or "").strip()
    if not is_allowed_url(url):
        raise ValueError("Дозволені лише посилання youtube.com, youtu.be, music.youtube.com.")
    kind = raw.get("kind", "video")
    if kind not in KINDS:
        raise ValueError(f"kind має бути одним із: {', '.join(KINDS)}.")
    audio = kind.endswith("audio")
    d = cfg["defaults"]
    quality = str(raw.get("quality") or (d["audio_quality"] if audio else d["video_quality"]))
    if audio and quality not in AUDIO_QUALITIES:
        raise ValueError("Якість аудіо: 128, 192, 256 або 320.")
    if not audio and not (quality.isdigit() and 144 <= int(quality) <= 4320):
        raise ValueError("Якість відео — висота в пікселях (наприклад 720, 1080).")

    items = raw.get("playlist_items")
    if items is not None:
        if isinstance(items, dict):
            lim = items.get("limit")
            if not isinstance(lim, int) or isinstance(lim, bool) or not 1 <= lim <= 5000:
                raise ValueError("playlist_items.limit має бути цілим числом 1–5000.")
            items = {"limit": lim}
        elif isinstance(items, str):
            if not re.fullmatch(r"\d+(-\d+)?(,\d+(-\d+)?)*", items.strip()):
                raise ValueError("playlist_items: очікується діапазон на кшталт «5-10».")
            items = items.strip()
        else:
            raise ValueError("playlist_items має бути null, рядком або {\"limit\": N}.")

    trim = raw.get("trim", d.get("trim"))
    if trim:
        if not isinstance(trim, dict):
            raise ValueError("trim має бути {\"start\":..., \"end\":...}.")
        start = parse_time(trim.get("start") or "0:00")
        end = parse_time(trim.get("end") or "")
        if end <= start:
            raise ValueError("Кінець фрагмента має бути пізніше за початок.")
        trim = {"start": start, "end": end}
    else:
        trim = None

    def flag(key, default):
        v = raw.get(key, default)
        if not isinstance(v, bool):
            raise ValueError(f"{key} має бути true/false.")
        return v

    fallback = raw.get("quality_fallback", "ask")
    if fallback not in ("ask", "auto"):
        raise ValueError("quality_fallback: ask або auto.")
    return {
        "url": url, "kind": kind, "quality": quality, "playlist_items": items, "trim": trim,
        "subtitles": flag("subtitles", d["subtitles"]),
        "metadata": flag("metadata", d["metadata"]),
        "normalize": flag("normalize", d["normalize"]),
        "skip_existing": flag("skip_existing", True),
        "ignore_space_check": flag("ignore_space_check", False),
        "quality_fallback": fallback,
    }


class _YdlLogger:
    def __init__(self, tag):
        self.tag = tag

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        log.warning("[%s] yt-dlp: %s", self.tag, msg)

    def error(self, msg):
        log.error("[%s] yt-dlp: %s", self.tag, msg)


class DownloadManager:
    def __init__(self, cfg, hub):
        self.cfg, self.hub = cfg, hub
        self.tasks = {}
        self.order = []
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.file_lock = threading.Lock()
        self.consecutive_blocks = 0
        self.consecutive_setup = 0
        self.stopping = threading.Event()
        self.workers = []
        self.base, self.video_dir, self.audio_dir = ensure_dirs(cfg)
        for i in range(cfg["server"]["max_workers"]):
            t = threading.Thread(target=self._worker, name=f"worker-{i}", daemon=True)
            t.start()
            self.workers.append(t)

    # ---- публічне API ------------------------------------------------------
    def submit(self, params, cookies_raw=None):
        task = Task(params, cookies_raw)
        with self.lock:
            self.tasks[task.id] = task
            self.order.append(task.id)
            if len(self.order) > 200:
                for old in self.order[:-200]:
                    t = self.tasks.get(old)
                    if t and t.finished.is_set():
                        self.tasks.pop(old, None)
                self.order = [i for i in self.order if i in self.tasks]
        self.q.put(task)
        self.tlog(task, "INFO", "Поставлено в чергу: %s (%s, %s)", params["url"], params["kind"],
                  params["quality"])
        return task

    def snapshot(self):
        with self.lock:
            return [self.tasks[i].to_dict() for i in self.order if i in self.tasks]

    def cancel(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
        if not task or task.finished.is_set():
            return False
        task.cancel.set()
        if task.status == "queued":
            self._finish(task, "cancelled", "Скасовано")
        return True

    def cancel_all(self):
        for tid in list(self.tasks):
            self.cancel(tid)

    def shutdown(self):
        self.stopping.set()
        self.cancel_all()
        for _ in self.workers:
            self.q.put(None)

    # ---- службове ----------------------------------------------------------
    def tlog(self, task, level, msg, *args):
        log.log(getattr(logging, level), "[%s] " + msg, task.id, *args)

    def emit_error(self, task, message, kind, **extra):
        data = {"task_id": task.id, "message": message, "kind": kind, **extra}
        self.hub.publish("error", data)

    def _finish(self, task, status, message=""):
        if task.finished.is_set():
            return
        task.status, task.message = status, message
        task.finished.set()
        data = {"task_id": task.id, "file": None, "status": status if status != "done" else "finished",
                "final": True, "counts": dict(task.counts), "message": message}
        self.hub.publish("done", data)
        self.tlog(task, "INFO", "Завершено: %s %s %s", status, task.counts, message)

    def halt(self, task, reason, kind):
        """Зупиняє поточне завдання і все, що в черзі."""
        self.tlog(task, "ERROR", "ЗУПИНКА ЧЕРГИ (%s): %s", kind, reason)
        task.halt_reason = reason
        self.emit_error(task, reason, kind, stop_queue=True)
        self.consecutive_blocks = self.consecutive_setup = 0
        with self.lock:
            pending = [t for t in self.tasks.values() if not t.finished.is_set() and t is not task]
        for t in pending:
            t.cancel.set()
            if t.status == "queued":
                self._finish(t, "error", "Чергу зупинено: " + reason)
        raise Cancelled()

    def _sleep(self, task, seconds, stage="waiting", item=None):
        """Сон із можливістю скасування; публікує прогрес очікування."""
        end = time.time() + seconds
        last = 0
        while True:
            left = end - time.time()
            if left <= 0:
                return
            if task.cancel.is_set() or self.stopping.is_set():
                raise Cancelled()
            if time.time() - last >= 5 and seconds >= 5:
                last = time.time()
                self.hub.publish("progress", {
                    "task_id": task.id, "item": item or task.title, "percent": 0,
                    "speed": "", "eta": time.strftime("%M:%S", time.gmtime(left)), "stage": stage})
            task.cancel.wait(min(1.0, left))

    def _worker(self):
        while True:
            task = self.q.get()
            if task is None:
                return
            if task.finished.is_set() or task.cancel.is_set():
                self._finish(task, "cancelled", "Скасовано")
                continue
            task.status = "running"
            try:
                self._run_task(task)
            except Cancelled:
                if task.halt_reason:
                    self._finish(task, "error", task.halt_reason)
                else:
                    self._finish(task, "cancelled", "Скасовано")
            except FatalOrTaskError as exc:
                self._finish(task, "error", str(exc))
            except FatalError as exc:
                try:
                    self.halt(task, str(exc), "fatal")
                except Cancelled:
                    pass
                self._finish(task, "error", str(exc))
            except Exception as exc:  # непередбачена помилка не має класти воркер
                self.tlog(task, "ERROR", "Непередбачена помилка: %r", exc)
                log.exception("[%s] traceback", task.id)
                self.emit_error(task, f"Внутрішня помилка: {exc}", "permanent")
                self._finish(task, "error", str(exc))

    # ---- виконання завдання -----------------------------------------------
    def _ydl_opts(self, task, cookiefile, extra=None):
        c = self.cfg["cookies"]
        opts = {
            "quiet": True, "no_warnings": True, "noprogress": True, "logger": _YdlLogger(task.id),
            "socket_timeout": 30, "retries": 3, "fragment_retries": 3, "extractor_retries": 2,
            "ignoreerrors": False, "noplaylist": True, "overwrites": False,
        }
        if cookiefile:
            opts["cookiefile"] = cookiefile
        elif c.get("file") and Path(c["file"]).expanduser().is_file():
            opts["cookiefile"] = str(Path(c["file"]).expanduser())
        elif (SCRIPT_DIR / "cookies.txt").is_file():
            opts["cookiefile"] = str(SCRIPT_DIR / "cookies.txt")
        elif c.get("from_browser"):
            opts["cookiesfrombrowser"] = (c["from_browser"],)
        opts.update(extra or {})
        return opts

    def _preflight(self, task):
        if yt_dlp is None:
            raise FatalError("yt-dlp не встановлено. Виконайте: pip install -U \"yt-dlp[default]\"")
        for tool in ("ffmpeg", "ffprobe"):
            if not shutil.which(tool):
                raise FatalError(f"{tool} не знайдено в PATH. Встановіть FFmpeg (winget install Gyan.FFmpeg).")

    def _run_task(self, task):
        p = task.params
        self._preflight(task)
        age = ytdlp_age_days()
        if age is not None and age > YTDLP_MAX_AGE_DAYS:
            self.tlog(task, "WARNING", "yt-dlp застарів (%s днів) — оновіть його", age)
        cookiefile = None
        if task.cookies_raw:
            try:
                fd, cookiefile = tempfile.mkstemp(prefix="ytdl_cookies_", suffix=".txt")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(cookies_to_netscape(task.cookies_raw))
                self.tlog(task, "INFO", "Cookies отримано від розширення")
            except Exception as exc:
                self.tlog(task, "WARNING", "Не вдалося обробити cookies: %s", exc)
                cookiefile = None
        else:
            self.tlog(task, "INFO", "Cookies від розширення не передано — використовую cookies.txt/конфіг")
        try:
            self._run_task_inner(task, cookiefile)
        finally:
            if cookiefile:
                try:
                    os.unlink(cookiefile)
                except OSError:
                    pass

    def _run_task_inner(self, task, cookiefile):
        p = task.params
        audio = p["kind"].endswith("audio")
        root = self.audio_dir if audio else self.video_dir
        is_playlist = p["kind"].startswith("playlist")
        out_dir, entries = root, [{"url": p["url"], "title": None}]

        if is_playlist:
            title, entries = self._list_playlist(task, cookiefile)
            task.title = title
            out_dir = root / sanitize_filename(title)
            if not entries:
                self.emit_error(task, "Плейлист порожній або недоступний.", "permanent")
                return self._finish(task, "error", "Плейлист порожній")
            if not p["ignore_space_check"]:
                need = self._estimate_bytes(entries, audio, p["quality"])
                root.mkdir(parents=True, exist_ok=True)
                free = shutil.disk_usage(root).free
                if need > free:
                    msg = (f"Мало місця на диску: потрібно ≈{need / 2**30:.1f} ГБ, вільно "
                           f"{free / 2**30:.1f} ГБ. Повторіть із ignore_space_check=true, щоб продовжити.")
                    self.emit_error(task, msg, "space", needed=need, free=free)
                    return self._finish(task, "error", msg)
        for e in entries:
            task.items.append({"title": e.get("title") or e["url"], "status": "queued", "percent": 0})

        pause = self.cfg["pause"]
        downloaded = 0
        prev_downloaded = False
        for idx, entry in enumerate(entries):
            if task.cancel.is_set():
                raise Cancelled()
            if idx > 0 and prev_downloaded:
                if pause["long_break_every"] and downloaded % int(pause["long_break_every"]) == 0:
                    wait = random.uniform(pause["long_break_min"], pause["long_break_max"])
                    self.tlog(task, "INFO", "Довга пауза %.0f с", wait)
                else:
                    wait = random.uniform(pause["between_items_min"], pause["between_items_max"])
                self._sleep(task, wait, "waiting")
            item = task.items[idx]
            status = self._process_item(task, entry, out_dir, item, cookiefile, is_playlist, audio)
            prev_downloaded = status == "ok"
            downloaded += 1 if status == "ok" else 0
            item["status"] = {"ok": "done", "skipped": "skipped", "failed": "error"}.get(status, status)
            if status in ("ok", "skipped", "failed"):
                task.counts[status] += 1
        final = "done" if task.counts["failed"] == 0 else "error"
        self._finish(task, final, f"Завантажено: {task.counts['ok']}, пропущено: "
                                  f"{task.counts['skipped']}, помилок: {task.counts['failed']}")

    def _list_playlist(self, task, cookiefile):
        p = task.params
        extra = {"extract_flat": "in_playlist", "noplaylist": False}
        items = p["playlist_items"]
        if isinstance(items, dict):
            extra["playlistend"] = items["limit"]
        elif items:
            extra["playlist_items"] = items
        for attempt in range(1, 4):
            try:
                with yt_dlp.YoutubeDL(self._ydl_opts(task, cookiefile, extra)) as ydl:
                    info = ydl.extract_info(p["url"], download=False)
                break
            except Exception as exc:
                cat = classify_error(exc)
                self.tlog(task, "WARNING", "Не вдалося отримати плейлист (%s, спроба %d): %s", cat, attempt, exc)
                if cat == "fatal":
                    raise FatalError(str(exc))
                if cat == "block":
                    self._handle_block(task, str(exc))
                    continue
                if cat == "transient" and attempt < 3:
                    self._sleep(task, 10 * 2 ** (attempt - 1) + random.uniform(0, 3))
                    continue
                self.emit_error(task, f"Не вдалося отримати плейлист: {exc}", cat)
                raise FatalOrTaskError(str(exc), cat)
        entries = []
        for e in (info.get("entries") or []):
            if not e:
                continue
            vid = e.get("id")
            url = e.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else None)
            if url and not url.startswith("http"):
                url = f"https://www.youtube.com/watch?v={url}"
            if url:
                entries.append({"url": url, "title": e.get("title"), "id": vid,
                                "duration": e.get("duration")})
        return info.get("title") or "playlist", entries

    @staticmethod
    def _estimate_bytes(entries, audio, quality):
        total = 0
        for e in entries:
            dur = e.get("duration") or (300 if audio else 600)
            total += dur * int(quality) * 125 if audio else dur / 3600 * 500 * 2 ** 20
        return int(total * 1.1)

    def _handle_block(self, task, msg):
        self.consecutive_blocks += 1
        n = self.consecutive_blocks
        if n >= 3:
            self.halt(task, "YouTube блокує запити (429/captcha) 3 рази поспіль. Зачекайте кілька годин, "
                            "оновіть yt-dlp і перевірте cookies. Чергу зупинено, щоб не «добити» акаунт.",
                      "block")
        wait = 300 * n
        self.tlog(task, "WARNING", "Блокування (спроба %d): пауза %d с. %s", n, wait, msg)
        self.emit_error(task, f"YouTube обмежує запити. Пауза {wait // 60} хв (спроба {n} з 3).", "block")
        self._sleep(task, wait, "blocked")

    # ---- один елемент -------------------------------------------------------
    def _process_item(self, task, entry, out_dir, item, cookiefile, is_playlist, audio):
        p = task.params
        transient_attempts = 0
        item["status"] = "downloading"
        while True:
            if task.cancel.is_set():
                raise Cancelled()
            try:
                return self._download_one(task, entry, out_dir, item, cookiefile, is_playlist, audio)
            except Cancelled:
                raise
            except QualityUnavailable as q:
                self.emit_error(task, str(q), "quality", requested=q.requested, available=q.available)
                task.message = str(q)
                self._finish(task, "error", str(q))
                raise Cancelled()
            except FatalOrTaskError:
                raise
            except Exception as exc:
                cat = classify_error(exc)
                reason = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).strip()
                self.tlog(task, "WARNING", "Помилка [%s] %s: %s", cat, entry["url"], reason)
                if cat == "fatal":
                    raise FatalError(reason)
                if cat == "block":
                    self._handle_block(task, reason)
                    continue
                if cat == "transient":
                    transient_attempts += 1
                    if transient_attempts < 3:
                        wait = 10 * 2 ** (transient_attempts - 1) + random.uniform(0, 3)
                        self._sleep(task, wait, "waiting", item["title"])
                        continue
                if cat == "setup":
                    self.consecutive_setup += 1
                    if self.consecutive_setup >= 2:
                        self.halt(task, f"Проблема налаштування yt-dlp ({reason}). {SETUP_HINTS}", "setup")
                    reason += " — " + SETUP_HINTS
                self._record_failed(entry["url"], item["title"], reason)
                self._archive(entry.get("id") or "-", entry["url"], "audio" if audio else "video",
                              p["quality"], "failed", "")
                self.emit_error(task, reason, "permanent" if cat == "permanent" else cat,
                                item=item["title"], url=entry["url"])
                return "failed"

    def _download_one(self, task, entry, out_dir, item, cookiefile, is_playlist, audio):
        p = task.params
        url = entry["url"]
        kind_name = "audio" if audio else "video"
        d = self.cfg["defaults"]
        ext = d["audio_format"] if audio else d["video_container"]

        with yt_dlp.YoutubeDL(self._ydl_opts(task, cookiefile)) as ydl:
            info = ydl.extract_info(url, download=False)
        if info.get("_type") == "playlist" and info.get("entries"):
            info = next(e for e in info["entries"] if e)
        title = info.get("title") or entry.get("title") or info.get("id", "video")
        item["title"] = title
        vid = info.get("id") or "-"

        if audio:
            artist = info.get("artist") or (", ".join(info["artists"]) if info.get("artists") else None) \
                or info.get("creator")
            name = f"{artist} - {info.get('track') or title}" if artist else title
        else:
            name = title
        name = sanitize_filename(name)
        if p["trim"]:
            tag = f" [{hms_tag(p['trim']['start'])}-{hms_tag(p['trim']['end'])}]"
            name = sanitize_filename(name, MAX_NAME_LEN - len(tag)) + tag
        target = out_dir / f"{name}.{ext}"

        if p["skip_existing"] and file_ok(target):
            self.tlog(task, "INFO", "Пропущено (вже є): %s", target)
            self._archive(vid, url, kind_name, p["quality"], "skipped", str(target))
            self.hub.publish("done", {"task_id": task.id, "file": str(target), "status": "skipped",
                                      "item": title})
            return "skipped"

        heights = sorted({f["height"] for f in info.get("formats", [])
                          if f.get("height") and f.get("vcodec") not in (None, "none")}, reverse=True)
        q = int(p["quality"]) if not audio else None
        if not audio and not is_playlist and heights and q not in heights \
                and p["quality_fallback"] == "ask":
            lower = [h for h in heights if h < q] or heights
            raise QualityUnavailable(q, lower)

        out_dir.mkdir(parents=True, exist_ok=True)
        stem = str(out_dir / name).replace("%", "%%")
        opts = self._ydl_opts(task, cookiefile, {
            "outtmpl": stem + ".%(ext)s",
            "progress_hooks": [self._progress_hook(task, item, title)],
            "postprocessor_hooks": [self._pp_hook(task, item, title)],
            "writethumbnail": bool(p["metadata"]),
            "postprocessors": [],
        })
        pps = opts["postprocessors"]
        if p["metadata"]:
            pps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
        if audio:
            opts["format"] = "bestaudio/best"
            pps.append({"key": "FFmpegExtractAudio", "preferredcodec": d["audio_format"],
                        "preferredquality": p["quality"]})
            if p["normalize"]:
                opts["postprocessor_args"] = {"extractaudio+ffmpeg_o": ["-af", LOUDNORM]}
        else:
            opts["format"] = (f"bv*[ext=mp4][height<={q}]+ba[ext=m4a]/bv*[height<={q}]+ba/"
                              f"b[height<={q}]/b")
            opts["merge_output_format"] = d["video_container"]
        if p["metadata"]:
            pps.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True})
            pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
        if p["subtitles"] and not audio:
            subs, auto = info.get("subtitles") or {}, info.get("automatic_captions") or {}
            lang = next((l for l in ("uk", "en") if l in subs or l in auto), None)
            if lang:
                opts.update({"writesubtitles": True, "writeautomaticsub": lang not in subs,
                             "subtitleslangs": [lang]})
                pps.insert(0, {"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"})
        if p["trim"]:
            opts["download_ranges"] = download_range_func(None, [(p["trim"]["start"], p["trim"]["end"])])
            opts["force_keyframes_at_cuts"] = False

        with yt_dlp.YoutubeDL(opts) as ydl:
            result = ydl.process_ie_result(info, download=True)

        final = None
        for rd in (result.get("requested_downloads") or []):
            fp = rd.get("filepath")
            if fp and Path(fp).exists():
                final = Path(fp)
        if final is None:
            final = target if target.exists() else next(iter(sorted(out_dir.glob(name + ".*"))), None)
        if final is None or not final.exists():
            raise RuntimeError("yt-dlp завершився без помилки, але файл не знайдено")

        if not audio and p["normalize"]:
            self._normalize_video(task, final, item, title)
        if p["subtitles"] and not audio:
            for srt in out_dir.glob(glob_escape(name) + ".*.srt"):
                try:
                    os.replace(srt, out_dir / f"{name}.srt")
                except OSError:
                    pass
        self.tlog(task, "INFO", "Готово: %s", final)
        self.consecutive_blocks = 0
        self.consecutive_setup = 0
        item["percent"] = 100
        self._archive(vid, url, kind_name, p["quality"], "ok", str(final))
        self.hub.publish("done", {"task_id": task.id, "file": str(final), "status": "ok", "item": title})
        return "ok"

    # ---- прогрес ------------------------------------------------------------
    def _progress_hook(self, task, item, title):
        last = [0.0]

        def hook(d):
            if task.cancel.is_set() or self.stopping.is_set():
                raise Cancelled()
            if d.get("status") != "downloading":
                return
            now = time.time()
            if now - last[0] < 0.4:
                return
            last[0] = now
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            pct = round(d["downloaded_bytes"] * 100 / total, 1) if total else 0.0
            item["percent"] = pct
            self.hub.publish("progress", {
                "task_id": task.id, "item": title, "percent": pct,
                "speed": (d.get("_speed_str") or "").strip(),
                "eta": (d.get("_eta_str") or "").strip(), "stage": "download"})
        return hook

    def _pp_hook(self, task, item, title):
        def hook(d):
            if task.cancel.is_set():
                raise Cancelled()
            if d.get("status") == "started":
                item["status"] = "converting"
                self.hub.publish("progress", {"task_id": task.id, "item": title, "percent": 100,
                                              "speed": "", "eta": "", "stage": "convert"})
        return hook

    def _normalize_video(self, task, path, item, title):
        item["status"] = "converting"
        self.hub.publish("progress", {"task_id": task.id, "item": title, "percent": 100,
                                      "speed": "", "eta": "", "stage": "normalize"})
        tmp = path.with_name(path.stem + ".norm" + path.suffix)
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-map", "0", "-map_metadata", "0",
               "-c", "copy", "-c:a", "aac", "-b:a", "192k", "-af", LOUDNORM, str(tmp)]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                creationflags=NO_WINDOW)
        while proc.poll() is None:
            if task.cancel.is_set():
                proc.kill()
                tmp.unlink(missing_ok=True)
                raise Cancelled()
            time.sleep(0.3)
        if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            os.replace(tmp, path)
        else:
            err = (proc.stderr.read() or b"").decode("utf-8", "replace")[-300:]
            self.tlog(task, "WARNING", "Нормалізація не вдалася, залишено оригінал: %s", err)
            tmp.unlink(missing_ok=True)

    # ---- звіти ---------------------------------------------------------------
    def _archive(self, vid, url, kind, quality, status, path):
        line = "\t".join([vid, url, kind, str(quality), status, path,
                          datetime.now().isoformat(timespec="seconds")])
        with self.file_lock, open(SCRIPT_DIR / "downloaded.txt", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def _record_failed(self, url, title, reason):
        reason = " ".join(str(reason).split())
        with self.file_lock, open(SCRIPT_DIR / "failed.txt", "a", encoding="utf-8") as fh:
            fh.write(f"{url} | {title} | {reason}\n")


class FatalOrTaskError(Exception):
    """Невідновна помилка підготовки завдання (не зупиняє всю чергу)."""

    def __init__(self, msg, kind):
        super().__init__(msg)
        self.kind = kind


def glob_escape(s):
    return re.sub(r"([\[\]*?])", r"[\1]", s)


# --------------------------------------------------------------------------- #
# Метадані для /api/info
# --------------------------------------------------------------------------- #
def fetch_info(manager, url, cookies_raw=None):
    if yt_dlp is None:
        raise RuntimeError("yt-dlp не встановлено")
    dummy = Task({"url": url, "kind": "video", "quality": "0"})
    cookiefile = None
    try:
        if cookies_raw:
            fd, cookiefile = tempfile.mkstemp(prefix="ytdl_cookies_", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(cookies_to_netscape(cookies_raw))
        is_pl = "/playlist" in urlparse(url).path
        extra = {"noplaylist": not is_pl}
        if is_pl:
            extra["extract_flat"] = "in_playlist"
        with yt_dlp.YoutubeDL(manager._ydl_opts(dummy, cookiefile, extra)) as ydl:
            info = ydl.extract_info(url, download=False)
    finally:
        if cookiefile:
            try:
                os.unlink(cookiefile)
            except OSError:
                pass
    if is_pl:
        entries = [e for e in (info.get("entries") or []) if e]
        return {"type": "playlist", "title": info.get("title"), "uploader": info.get("uploader"),
                "count": len(entries), "id": info.get("id")}
    heights = sorted({f["height"] for f in info.get("formats", [])
                      if f.get("height") and f.get("vcodec") not in (None, "none")}, reverse=True)
    return {"type": "video", "id": info.get("id"), "title": info.get("title"),
            "duration": info.get("duration"), "uploader": info.get("uploader"),
            "artist": info.get("artist"), "thumbnail": info.get("thumbnail"),
            "available_qualities": heights, "audio_qualities": list(AUDIO_QUALITIES)}


# --------------------------------------------------------------------------- #
# HTTP-сервер
# --------------------------------------------------------------------------- #
class RateLimiter:
    def __init__(self, per_minute):
        self.per_minute = per_minute
        self.hits = {}
        self.lock = threading.Lock()

    def check(self, key):
        """None, якщо дозволено; інакше кількість секунд до наступної спроби."""
        now = time.time()
        with self.lock:
            q = [t for t in self.hits.get(key, []) if now - t < 60]
            if len(q) >= self.per_minute:
                self.hits[key] = q
                return max(1, int(60 - (now - q[0])) + 1)
            q.append(now)
            self.hits[key] = q
        return None


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.hub = Hub()
        self.manager = DownloadManager(cfg, self.hub)
        self.limiter = RateLimiter(int(cfg["server"]["rate_limit_per_minute"]))
        self.shutdown_event = threading.Event()
        self.on_shutdown_request = None  # колбек для POST /api/shutdown
        self._cfg_mtime = None

    def refresh_cfg(self):
        """Підхоплює token і allowed_extension_ids із config.json без перезапуску."""
        try:
            mtime = CONFIG_PATH.stat().st_mtime
            if mtime != self._cfg_mtime:
                srv = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("server", {})
                if srv.get("token"):
                    self.cfg["server"]["token"] = str(srv["token"]).strip()
                self.cfg["server"]["allowed_extension_ids"] = [str(i) for i in srv.get("allowed_extension_ids", [])]
                self._cfg_mtime = mtime
        except (OSError, ValueError):
            pass
        return self.cfg["server"]


class Handler(BaseHTTPRequestHandler):
    server_version = "YTDL/" + APP_VERSION
    MAX_BODY = 64 * 1024

    def log_message(self, fmt, *args):
        log.debug("HTTP %s", fmt % args)

    # --- допоміжні ---
    @property
    def app(self):
        return self.server.app

    def _cors(self):
        origin = self.headers.get("Origin")
        if origin and self._origin_allowed(origin):
            return {"Access-Control-Allow-Origin": origin, "Vary": "Origin",
                    "Access-Control-Allow-Headers": "Authorization, Content-Type, X-YTDL-Cookies",
                    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                    "Access-Control-Max-Age": "600"}
        return {}

    def _origin_allowed(self, origin):
        m = re.fullmatch(r"chrome-extension://([a-p]{32})", origin or "")
        return bool(m and m.group(1) in self.app.refresh_cfg()["allowed_extension_ids"])

    def _send(self, code, payload=None, extra=None):
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in {**self._cors(), **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _body_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > self.MAX_BODY:
            raise ValueError("Некоректний розмір запиту.")
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Тіло запиту має бути коректним JSON.")

    def _authorized(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        return hmac.compare_digest(auth[7:].strip().encode(), self.app.refresh_cfg()["token"].encode())

    # --- маршрутизація ---
    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def do_OPTIONS(self):
        self._dispatch()

    def _dispatch(self):
        try:
            self._handle()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            log.exception("HTTP помилка")
            try:
                self._send(500, {"error": f"Внутрішня помилка: {exc}"})
            except Exception:
                pass

    def _handle(self):
        host = (self.headers.get("Host") or "").lower()
        port = self.app.cfg["server"]["port"]
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return self._send(403, {"error": "Недозволений Host."})
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        if not path.startswith("/api/") and path != "/api":
            return self._send(404, {"error": "Не знайдено."})

        origin = self.headers.get("Origin")
        if path == "/api/health":
            if self.command != "GET" and self.command != "OPTIONS":
                return self._send(405, {"error": "Метод не дозволено."})
            if self.command == "OPTIONS":
                return self._send(204)
            age = ytdlp_age_days()
            return self._send(200, {"status": "ok", "version": APP_VERSION,
                                    "yt_dlp": yt_dlp.version.__version__ if yt_dlp else None,
                                    "yt_dlp_age_days": age})
        if origin and not self._origin_allowed(origin):
            return self._send(403, {"error": "Origin не дозволено. Додайте ID розширення до "
                                             "allowed_extension_ids у config.json."})
        if self.command == "OPTIONS":
            return self._send(204)
        if not self._authorized():
            return self._send(401, {"error": "Потрібен токен: Authorization: Bearer <token>."},
                              {"WWW-Authenticate": "Bearer"})
        if path != "/api/events":
            wait = self.app.limiter.check(origin or "local")
            if wait:
                return self._send(429, {"error": "Забагато запитів. Спробуйте за кілька секунд."},
                                  {"Retry-After": str(wait)})

        m = self.command
        mgr = self.app.manager
        qs = parse_qs(url.query)
        cookies_raw = self.headers.get("X-YTDL-Cookies")

        if path == "/api/events" and m == "GET":
            return self._sse()
        if path == "/api/queue" and m == "GET":
            return self._send(200, {"tasks": mgr.snapshot()})
        if path.startswith("/api/queue/") and m == "DELETE":
            ok = mgr.cancel(path.rsplit("/", 1)[1])
            return self._send(200 if ok else 404, {"cancelled": ok} if ok else {"error": "Завдання не знайдено."})
        if path == "/api/download" and m == "POST":
            try:
                params = normalize_params(self._body_json(), self.app.cfg)
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            task = mgr.submit(params, cookies_raw)
            return self._send(202, {"task_id": task.id, "status": "queued"})
        if path == "/api/info" and m == "GET":
            target = (qs.get("url") or [""])[0]
            if not is_allowed_url(target):
                return self._send(400, {"error": "Дозволені лише посилання YouTube."})
            try:
                return self._send(200, fetch_info(mgr, target, cookies_raw))
            except Exception as exc:
                cat = classify_error(exc)
                return self._send(502, {"error": re.sub(r"\x1b\[[0-9;]*m", "", str(exc)), "kind": cat})
        if path == "/api/open-folder" and m == "POST":
            return self._open_folder()
        if path == "/api/shutdown" and m == "POST":
            self._send(200, {"status": "shutting_down"})
            if self.app.on_shutdown_request:
                threading.Thread(target=self.app.on_shutdown_request, daemon=True).start()
            return
        return self._send(404 if path.startswith("/api/") else 405,
                         {"error": "Невідомий ендпоінт або метод."})

    def _open_folder(self):
        try:
            body = self._body_json()
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        mgr = self.app.manager
        target = None
        if body.get("file"):
            f = Path(str(body["file"])).resolve()
            roots = [mgr.base.resolve(), mgr.video_dir.resolve(), mgr.audio_dir.resolve()]
            if not any(f == r or r in f.parents for r in roots):
                return self._send(400, {"error": "Шлях поза папками завантажень."})
            target = f if f.exists() else f.parent
        else:
            kind = body.get("kind")
            target = {"video": mgr.video_dir, "audio": mgr.audio_dir}.get(kind, mgr.base)
        target.mkdir(parents=True, exist_ok=True) if not target.exists() else None
        try:
            open_in_explorer(target)
        except Exception as exc:
            return self._send(500, {"error": f"Не вдалося відкрити папку: {exc}"})
        return self._send(200, {"opened": str(target)})

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        for k, v in self._cors().items():
            self.send_header(k, v)
        self.end_headers()
        q = self.app.hub.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while not self.app.shutdown_event.is_set():
                try:
                    event, data = q.get(timeout=15)
                    msg = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    msg = ": ping\n\n"
                self.wfile.write(msg.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.hub.unsubscribe(q)


class ApiServer:
    """Обгортка над ThreadingHTTPServer з можливістю start/stop (для вікна)."""

    def __init__(self, app):
        self.app = app
        self.httpd = None
        self.thread = None

    @property
    def running(self):
        return self.httpd is not None

    def start(self):
        s = self.app.cfg["server"]
        ThreadingHTTPServer.daemon_threads = True
        self.app.shutdown_event.clear()
        self.httpd = ThreadingHTTPServer((s["host"], int(s["port"])), Handler)
        self.httpd.app = self.app
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="http", daemon=True)
        self.thread.start()
        log.info("Сервер запущено на http://%s:%s", s["host"], s["port"])

    def stop(self):
        if not self.httpd:
            return
        self.app.shutdown_event.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd = None
        log.info("Сервер зупинено")


# --------------------------------------------------------------------------- #
# Вікно Tkinter
# --------------------------------------------------------------------------- #
def run_gui(app, server, start_minimized=False):
    import tkinter as tk
    from tkinter import ttk, messagebox

    cfg = app.cfg
    mgr = app.manager
    root = tk.Tk()
    root.title(f"{APP_NAME} {APP_VERSION}")
    root.geometry("860x700")
    ui_calls = queue.Queue()
    state = {"tray": None}

    def call_ui(fn):
        ui_calls.put(fn)

    def pump():
        while True:
            try:
                ui_calls.get_nowait()()
            except queue.Empty:
                break
            except Exception:
                log.exception("UI")
        root.after(200, pump)

    # --- статус ---
    top = ttk.LabelFrame(root, text="Сервер")
    top.pack(fill="x", padx=8, pady=6)
    status_var = tk.StringVar()
    ttk.Label(top, textvariable=status_var, font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=4,
                                                                             sticky="w", padx=6, pady=2)
    ttk.Label(top, text="Токен:").grid(row=1, column=0, sticky="w", padx=6)
    token_var = tk.StringVar(value="•" * 32)
    ttk.Entry(top, textvariable=token_var, state="readonly", width=40).grid(row=1, column=1, padx=4)
    shown = {"v": False}

    def toggle_token():
        shown["v"] = not shown["v"]
        token_var.set(cfg["server"]["token"] if shown["v"] else "•" * 32)
        btn_show.config(text="Сховати" if shown["v"] else "Показати")

    def copy_token():
        root.clipboard_clear()
        root.clipboard_append(cfg["server"]["token"])
        messagebox.showinfo(APP_NAME, "Токен скопійовано в буфер обміну.")

    btn_show = ttk.Button(top, text="Показати", command=toggle_token)
    btn_show.grid(row=1, column=2, padx=2)
    ttk.Button(top, text="Копіювати", command=copy_token).grid(row=1, column=3, padx=2)
    warn_var = tk.StringVar()
    ttk.Label(top, textvariable=warn_var, foreground="#b45309").grid(row=2, column=0, columnspan=4,
                                                                    sticky="w", padx=6, pady=2)

    def refresh_status():
        if server.running:
            status_var.set(f"● Запущено — http://{cfg['server']['host']}:{cfg['server']['port']}")
            btn_stop.config(text="Зупинити сервер")
        else:
            status_var.set("○ Зупинено")
            btn_stop.config(text="Запустити сервер")
        age = ytdlp_age_days()
        if yt_dlp is None:
            warn_var.set("⚠ yt-dlp не встановлено.")
        elif age is not None and age > YTDLP_MAX_AGE_DAYS:
            warn_var.set(f"⚠ yt-dlp застарів ({age} дн.). Натисніть «Оновити yt-dlp».")
        else:
            warn_var.set("")

    # --- завантаження вручну ---
    form = ttk.LabelFrame(root, text="Завантажити")
    form.pack(fill="x", padx=8, pady=6)
    url_var = tk.StringVar()
    ttk.Label(form, text="URL:").grid(row=0, column=0, sticky="w", padx=6, pady=2)
    ttk.Entry(form, textvariable=url_var, width=70).grid(row=0, column=1, columnspan=5, sticky="we", padx=4)
    kind_var = tk.StringVar(value="video")
    ttk.Label(form, text="Тип:").grid(row=1, column=0, sticky="w", padx=6)
    for i, (txt, val) in enumerate((("Відео (MP4)", "video"), ("Аудіо (MP3)", "audio"))):
        ttk.Radiobutton(form, text=txt, variable=kind_var, value=val,
                        command=lambda: set_quality_choices()).grid(row=1, column=1 + i, sticky="w")
    playlist_var = tk.BooleanVar()
    ttk.Checkbutton(form, text="Це плейлист", variable=playlist_var).grid(row=1, column=3, sticky="w")
    ttk.Label(form, text="Якість:").grid(row=2, column=0, sticky="w", padx=6)
    quality_var = tk.StringVar(value=cfg["defaults"]["video_quality"])
    quality_cb = ttk.Combobox(form, textvariable=quality_var, width=8, state="normal")
    quality_cb.grid(row=2, column=1, sticky="w")

    def set_quality_choices():
        if kind_var.get() == "audio":
            quality_cb["values"] = AUDIO_QUALITIES
            quality_var.set(cfg["defaults"]["audio_quality"])
        else:
            quality_cb["values"] = ("360", "480", "720", "1080", "1440", "2160")
            quality_var.set(cfg["defaults"]["video_quality"])

    set_quality_choices()
    ttk.Label(form, text="Перші N (плейлист):").grid(row=2, column=2, sticky="e")
    limit_var = tk.StringVar()
    ttk.Entry(form, textvariable=limit_var, width=6).grid(row=2, column=3, sticky="w")
    trim_var = tk.BooleanVar()
    ttk.Checkbutton(form, text="Обрізати:", variable=trim_var).grid(row=3, column=0, sticky="w", padx=6)
    start_var, end_var = tk.StringVar(value="0:00"), tk.StringVar()
    ttk.Entry(form, textvariable=start_var, width=10).grid(row=3, column=1, sticky="w")
    ttk.Label(form, text="—").grid(row=3, column=2)
    ttk.Entry(form, textvariable=end_var, width=10).grid(row=3, column=3, sticky="w")
    subs_var = tk.BooleanVar(value=cfg["defaults"]["subtitles"])
    meta_var = tk.BooleanVar(value=cfg["defaults"]["metadata"])
    norm_var = tk.BooleanVar(value=cfg["defaults"]["normalize"])
    skip_var = tk.BooleanVar(value=True)
    opts = ttk.Frame(form)
    opts.grid(row=4, column=0, columnspan=6, sticky="w", padx=6, pady=2)
    for txt, var in (("Субтитри (.srt)", subs_var), ("Метадані", meta_var),
                     ("Нормалізація гучності", norm_var), ("Пропускати наявні", skip_var)):
        ttk.Checkbutton(opts, text=txt, variable=var).pack(side="left", padx=4)

    def do_download():
        raw = {"url": url_var.get().strip(),
               "kind": ("playlist_" if playlist_var.get() else "") + kind_var.get(),
               "quality": quality_var.get().strip(), "subtitles": subs_var.get(),
               "metadata": meta_var.get(), "normalize": norm_var.get(), "skip_existing": skip_var.get()}
        if limit_var.get().strip():
            if not limit_var.get().strip().isdigit():
                return messagebox.showerror(APP_NAME, "«Перші N» має бути числом.")
            raw["playlist_items"] = {"limit": int(limit_var.get())}
        if trim_var.get():
            raw["trim"] = {"start": start_var.get(), "end": end_var.get()}
        try:
            params = normalize_params(raw, cfg)
        except ValueError as exc:
            return messagebox.showerror(APP_NAME, str(exc))
        params["quality_fallback"] = "ask"
        mgr.submit(params)
        url_var.set("")

    ttk.Button(form, text="Завантажити", command=do_download).grid(row=5, column=1, sticky="w", pady=4)
    ttk.Button(form, text="Пошук за назвою…", command=lambda: search_dialog()).grid(row=5, column=2,
                                                                                    columnspan=2, sticky="w")

    def search_dialog():
        try:
            from ytmusicapi import YTMusic
        except Exception:
            return messagebox.showerror(APP_NAME, "ytmusicapi не встановлено: pip install ytmusicapi")
        import difflib
        win = tk.Toplevel(root)
        win.title("Пошук")
        qv = tk.StringVar()
        ttk.Entry(win, textvariable=qv, width=50).pack(padx=8, pady=6)
        lb = tk.Listbox(win, width=80, height=12)
        lb.pack(padx=8, pady=4)
        found = []

        def go():
            lb.delete(0, "end")
            found.clear()
            try:
                res = YTMusic().search(qv.get(), filter="songs", limit=15)
            except Exception as exc:
                return messagebox.showerror(APP_NAME, f"Пошук не вдався: {exc}")
            q = qv.get().lower()
            scored = []
            for r in res:
                artists = ", ".join(a["name"] for a in r.get("artists", []))
                label = f"{artists} - {r.get('title', '')}"
                scored.append((difflib.SequenceMatcher(None, q, label.lower()).ratio(), label, r.get("videoId")))
            for score, label, vid in sorted(scored, reverse=True):
                if vid:
                    found.append(vid)
                    lb.insert("end", f"{label}   ({score:.0%})")

        def pick(_=None):
            if lb.curselection():
                url_var.set(f"https://music.youtube.com/watch?v={found[lb.curselection()[0]]}")
                kind_var.set("audio")
                set_quality_choices()
                win.destroy()

        ttk.Button(win, text="Шукати", command=go).pack()
        lb.bind("<Double-Button-1>", pick)
        ttk.Button(win, text="Обрати", command=pick).pack(pady=4)

    # --- черга ---
    qf = ttk.LabelFrame(root, text="Черга")
    qf.pack(fill="both", expand=True, padx=8, pady=6)
    cols = ("title", "kind", "status", "progress")
    tree = ttk.Treeview(qf, columns=cols, show="headings", height=8)
    for c, t, w in (("title", "Назва", 380), ("kind", "Тип", 110), ("status", "Статус", 120),
                    ("progress", "Результат", 190)):
        tree.heading(c, text=t)
        tree.column(c, width=w)
    tree.pack(fill="both", expand=True, side="left")
    sb = ttk.Scrollbar(qf, command=tree.yview)
    sb.pack(side="right", fill="y")
    tree.config(yscrollcommand=sb.set)
    status_names = {"queued": "у черзі", "running": "завантажується", "done": "готово",
                    "error": "помилка", "cancelled": "скасовано"}

    def refresh_queue():
        sel = tree.selection()
        tree.delete(*tree.get_children())
        for t in reversed(mgr.snapshot()):
            c = t["counts"]
            tree.insert("", "end", iid=t["task_id"], values=(
                t["title"], t["kind"], status_names.get(t["status"], t["status"]),
                f"✔{c['ok']} ⏭{c['skipped']} ✖{c['failed']}" + (f" — {t['message']}" if t["status"] == "error" else "")))
        for s in sel:
            if tree.exists(s):
                tree.selection_add(s)
        refresh_status()
        root.after(1000, refresh_queue)

    def cancel_selected():
        for s in tree.selection():
            mgr.cancel(s)

    # --- кнопки ---
    bar = ttk.Frame(root)
    bar.pack(fill="x", padx=8, pady=6)

    def open_dl():
        try:
            open_in_explorer(mgr.base)
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def open_log():
        p = SCRIPT_DIR / "log.txt"
        try:
            p.touch(exist_ok=True)
            os.startfile(str(p)) if IS_WINDOWS else open_in_explorer(p)  # noqa: S606
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def do_update():
        btn_upd.config(state="disabled", text="Оновлення…")

        def work():
            ok, out = update_ytdlp()

            def done():
                btn_upd.config(state="normal", text="Оновити yt-dlp")
                messagebox.showinfo(APP_NAME, ("yt-dlp оновлено. Перезапустіть сервер, щоб застосувати." if ok
                                               else "Оновити не вдалося:\n" + out[-600:]))
            call_ui(done)
        threading.Thread(target=work, daemon=True).start()

    def toggle_server():
        try:
            if server.running:
                mgr.cancel_all()
                server.stop()
            else:
                server.start()
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Не вдалося запустити сервер: {exc}")
        refresh_status()

    def quit_app():
        if state["tray"]:
            try:
                state["tray"].stop()
            except Exception:
                pass
        mgr.shutdown()
        server.stop()
        root.destroy()

    def to_tray():
        root.withdraw()

    def show_window():
        root.deiconify()
        root.lift()

    btn_stop = ttk.Button(bar, text="Зупинити сервер", command=toggle_server)
    btn_upd = ttk.Button(bar, text="Оновити yt-dlp", command=do_update)
    for txt, cmd in (("Скасувати вибране", cancel_selected), ("Відкрити папку завантажень", open_dl),
                     ("Відкрити лог", open_log)):
        ttk.Button(bar, text=txt, command=cmd).pack(side="left", padx=3)
    btn_upd.pack(side="left", padx=3)
    btn_stop.pack(side="left", padx=3)
    ttk.Button(bar, text="Згорнути в трей", command=to_tray).pack(side="left", padx=3)
    ttk.Button(bar, text="Вийти", command=quit_app).pack(side="right", padx=3)

    # --- трей (опційно: pystray + Pillow) ---
    try:
        import pystray
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (64, 64), "#c4302b")
        ImageDraw.Draw(img).polygon([(24, 16), (24, 48), (50, 32)], fill="white")
        menu = pystray.Menu(
            pystray.MenuItem("Показати вікно", lambda: call_ui(show_window), default=True),
            pystray.MenuItem("Відкрити папку завантажень", lambda: call_ui(open_dl)),
            pystray.MenuItem("Вийти", lambda: call_ui(quit_app)))
        state["tray"] = pystray.Icon("ytdl", img, APP_NAME, menu)
        state["tray"].run_detached()
    except Exception as exc:
        log.info("Трей недоступний (%s): вікно просто мінімізуватиметься", exc)

    def on_close():
        if state["tray"]:
            to_tray()
        else:
            root.iconify()

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh_status()
    refresh_queue()
    pump()
    if start_minimized:
        root.after(100, on_close)
    root.mainloop()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        prog="youtube_downloader.py",
        description=f"{APP_NAME} {APP_VERSION}: локальний сервер для розширення Chrome, CLI та вікно Tkinter.",
        epilog="Приклади:\n"
               "  python youtube_downloader.py URL --kind video --quality 1080\n"
               "  python youtube_downloader.py URL --kind audio --quality 320\n"
               "  python youtube_downloader.py URL --kind playlist_audio --quality 320 --limit 10\n"
               "  python youtube_downloader.py --update\n"
               "  python youtube_downloader.py --dry-run [URL]",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("url", nargs="?", help="посилання YouTube / YouTube Music (без нього — вікно Tkinter)")
    p.add_argument("--kind", choices=KINDS, default="video", help="тип завантаження (за замовчуванням video)")
    p.add_argument("--quality", help="відео: висота (1080); аудіо: 128/192/256/320")
    p.add_argument("--limit", type=int, help="для плейлиста: перші N елементів")
    p.add_argument("--items", help="для плейлиста: діапазон, напр. 5-10")
    p.add_argument("--start", help="початок фрагмента (mm:ss або hh:mm:ss)")
    p.add_argument("--end", help="кінець фрагмента")
    p.add_argument("--subtitles", action="store_true", help="зберегти субтитри окремим .srt")
    p.add_argument("--no-metadata", action="store_true", help="не вбудовувати метадані й обкладинку")
    p.add_argument("--no-normalize", action="store_true", help="не нормалізувати гучність")
    p.add_argument("--no-skip", action="store_true", help="не пропускати вже наявні файли")
    p.add_argument("--auto-quality", action="store_true",
                   help="якщо обраної якості немає — взяти найближчу нижчу")
    p.add_argument("--ignore-space-check", action="store_true", help="не перевіряти вільне місце")
    p.add_argument("--update", action="store_true", help="оновити yt-dlp і вийти")
    p.add_argument("--dry-run", action="store_true",
                   help="лише перевірка (середовище; з URL — що саме буде завантажено), без завантаження")
    p.add_argument("--server", action="store_true", help="запустити лише сервер, без вікна")
    p.add_argument("--minimized", action="store_true", help="запустити вікно згорнутим у трей")
    p.add_argument("--config", help="шлях до config.json")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    return p


def out(msg=""):
    if sys.stdout is not None:
        print(msg, flush=True)


def cmd_dry_run(args, cfg):
    out("Перевірка середовища:")
    for name, ok, detail in check_environment():
        out(f"  {'✔' if ok else '✘'} {name}: {detail}")
    age = ytdlp_age_days()
    if age is not None:
        out(f"  yt-dlp: вік {age} дн." + (" — ЗАСТАРІВ, виконайте --update" if age > YTDLP_MAX_AGE_DAYS else ""))
    base, vdir, adir = resolve_dirs(cfg)
    out(f"  Папка відео: {vdir}\n  Папка аудіо: {adir}")
    out(f"  Сервер: http://{cfg['server']['host']}:{cfg['server']['port']} "
        f"(токен: {cfg['server']['token'][:4]}… у config.json)")
    if not args.url:
        return 0
    raw = _args_to_raw(args)
    try:
        params = normalize_params(raw, cfg)
    except ValueError as exc:
        out(f"✘ {exc}")
        return 2
    if yt_dlp is None:
        out("✘ yt-dlp не встановлено — деталі недоступні.")
        return 1
    hub = Hub()
    mgr = DownloadManager.__new__(DownloadManager)  # без воркерів і створення папок
    mgr.cfg, mgr.hub = cfg, hub
    task = Task(params)
    mgr.base, mgr.video_dir, mgr.audio_dir = base, vdir, adir
    audio = params["kind"].endswith("audio")
    root = adir if audio else vdir
    try:
        if params["kind"].startswith("playlist"):
            mgr.stopping = threading.Event()
            mgr.consecutive_blocks = 0
            mgr.lock = threading.Lock()
            mgr.tasks = {}
            title, entries = mgr._list_playlist(task, None)
            out(f"Плейлист «{title}»: {len(entries)} елементів → {root / sanitize_filename(title)}")
            for e in entries[:20]:
                out(f"  • {e.get('title') or e['url']}")
            if len(entries) > 20:
                out(f"  … і ще {len(entries) - 20}")
            need = mgr._estimate_bytes(entries, audio, params["quality"])
            out(f"Орієнтовний розмір: ≈{need / 2**30:.1f} ГБ, вільно {shutil.disk_usage(root if root.exists() else base).free / 2**30:.1f} ГБ")
        else:
            info = fetch_info(mgr, params["url"])
            ext = cfg["defaults"]["audio_format"] if audio else cfg["defaults"]["video_container"]
            out(f"Назва: {info['title']}\nТривалість: {info.get('duration')} с")
            if not audio:
                out(f"Доступні якості: {', '.join(map(str, info['available_qualities']))}")
            out(f"Файл: {root / (sanitize_filename(info['title']) + '.' + ext)}")
    except Exception as exc:
        out(f"✘ {exc}")
        return 1
    out("Dry-run: нічого не завантажено.")
    return 0


def _args_to_raw(args):
    raw = {"url": args.url, "kind": args.kind, "skip_existing": not args.no_skip,
           "metadata": not args.no_metadata, "normalize": not args.no_normalize,
           "subtitles": args.subtitles, "ignore_space_check": args.ignore_space_check,
           "quality_fallback": "auto" if args.auto_quality else "ask"}
    if args.quality:
        raw["quality"] = args.quality
    if args.limit:
        raw["playlist_items"] = {"limit": args.limit}
    elif args.items:
        raw["playlist_items"] = args.items
    if args.start or args.end:
        raw["trim"] = {"start": args.start or "0:00", "end": args.end}
    return raw


def cmd_download(args, cfg):
    try:
        params = normalize_params(_args_to_raw(args), cfg)
    except ValueError as exc:
        out(f"✘ {exc}")
        return 2
    cfg["server"]["max_workers"] = 1
    hub = Hub()
    mgr = DownloadManager(cfg, hub)
    q = hub.subscribe()
    task = mgr.submit(params)

    def printer():
        last = ""
        while True:
            try:
                event, d = q.get(timeout=0.5)
            except queue.Empty:
                if task.finished.is_set():
                    return
                continue
            if d.get("task_id") != task.id:
                continue
            if event == "progress":
                line = f"\r  {d['item'][:50]} {d['percent']:5.1f}% {d['speed']} ETA {d['eta']} [{d['stage']}]"
                if line != last and sys.stdout is not None:
                    sys.stdout.write(line + "   ")
                    sys.stdout.flush()
                    last = line
            elif event == "done" and not d.get("final"):
                out(f"\n✔ {d['status']}: {d['file']}")
            elif event == "error":
                out(f"\n✘ [{d['kind']}] {d['message']}")
                if d.get("available"):
                    out("  Підказка: повторіть із --quality <значення> або --auto-quality")
    t = threading.Thread(target=printer, daemon=True)
    t.start()
    try:
        while not task.finished.wait(0.5):
            pass
    except KeyboardInterrupt:
        out("\nСкасування…")
        task.cancel.set()
        task.finished.wait(10)
    t.join(2)
    mgr.shutdown()
    out(f"\n{task.message}")
    return 0 if task.status == "done" else 1


def main(argv=None):
    args = build_parser().parse_args(argv)
    global CONFIG_PATH
    if args.config:
        CONFIG_PATH = Path(args.config).resolve()
    setup_logging(console=bool(args.url or args.server or args.dry_run or args.update))

    if args.update:
        ok, output = update_ytdlp()
        out(output[-1500:])
        out("✔ yt-dlp оновлено." if ok else "✘ Оновлення не вдалося.")
        return 0 if ok else 1

    cfg = load_config(CONFIG_PATH)
    if args.dry_run:
        return cmd_dry_run(args, cfg)
    if args.url:
        return cmd_download(args, cfg)

    app = App(cfg)
    server = ApiServer(app)
    try:
        server.start()
    except OSError as exc:
        msg = (f"Не вдалося запустити сервер на порту {cfg['server']['port']}: {exc}.\n"
               "Можливо, він уже запущений (перевірте трей).")
        log.error(msg)
        if args.server:
            out(msg)
        else:
            try:
                import tkinter
                from tkinter import messagebox
                r = tkinter.Tk()
                r.withdraw()
                messagebox.showerror(APP_NAME, msg)
            except Exception:
                pass
        return 1

    if args.server:
        done = threading.Event()
        app.on_shutdown_request = done.set
        out(f"Сервер: http://{cfg['server']['host']}:{cfg['server']['port']}\nТокен: {cfg['server']['token']}\n"
            "Ctrl+C — зупинити.")
        try:
            while not done.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        app.manager.shutdown()
        server.stop()
        return 0

    minimized = args.minimized or sys.executable.lower().endswith("pythonw.exe")
    app.on_shutdown_request = lambda: os._exit(0)  # POST /api/shutdown: повний вихід із вікном
    run_gui(app, server, start_minimized=minimized)
    return 0


if __name__ == "__main__":
    sys.exit(main())
