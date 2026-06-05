#!/usr/bin/env python3
"""Telegram <-> Claude Code bridge (solo / owner-only).

Gives the owner the FULL Claude Code agent over Telegram: it reads, edits and
runs commands inside the dev's projects, with persistent per-project sessions
so the conversation accumulates like a long desktop session.

SECURITY: this runs `claude` with --permission-mode bypassPermissions and ALL
tools, as root in WSL with access to /mnt/c. Any accepted message == remote
code execution on the PC. The ONLY barrier is the OWNER_ID allowlist, checked
BEFORE any claude invocation. Keep bridge.env at chmod 600. Rotate the bot
token if it ever leaks.

Stdlib only (no pip deps). Long-polls Telegram getUpdates.
"""
import html
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter

IS_MAC = sys.platform == "darwin"

# IPv4-first DNS. On an IPv4-only network (common on home/cellular links) a
# stale AAAA record can stall every outbound connection — the Telegram
# long-poll, getFile, media downloads — until the IPv6 attempt times out.
# Reorder getaddrinfo so A records are tried first; IPv6 stays as a fallback.
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_first_getaddrinfo(*args, **kwargs):
    res = _orig_getaddrinfo(*args, **kwargs)
    return sorted(res, key=lambda r: 0 if r[0] == socket.AF_INET else 1)


socket.getaddrinfo = _ipv4_first_getaddrinfo

# pythonw.exe (used by the Windows scheduled task) has no console, so
# sys.stderr is None and any .write() would crash. Route it to a log file.
if sys.stderr is None:
    _logdir = os.path.dirname(os.path.abspath(__file__))
    sys.stderr = open(os.path.join(_logdir, "bridge.log"), "a",
                      buffering=1, encoding="utf-8", errors="replace")

if os.name == "nt" or IS_MAC:
    # Windows and macOS keep bridge.env next to the script. macOS launchd
    # LaunchAgents run from an arbitrary cwd, so an absolute default beats a
    # relative one; the script dir is stable.
    DEF_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "bridge.env")
else:
    DEF_ENV = "/root/telegram-bridge/bridge.env"


# ---------------------------------------------------------------- config ----
def load_env(path):
    cfg = {}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip().lstrip("﻿")
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


ENV_PATH = os.environ.get("BRIDGE_ENV", DEF_ENV)
CFG = load_env(ENV_PATH)

TOKEN = CFG.get("TELEGRAM_BOT_TOKEN", "")
OWNER_ID = int(CFG.get("OWNER_ID", "0") or "0")
OAUTH = CFG.get("CLAUDE_CODE_OAUTH_TOKEN", "")
CLAUDE_BIN = CFG.get("CLAUDE_BIN", "/root/.local/bin/claude")
MODEL = CFG.get("CLAUDE_MODEL", "opus")
# Reasoning effort passed to claude as --effort. Persisted in STATE, changed at
# runtime with /effort. Ordered low -> max for the menu and validation.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
EFFORT = CFG.get("CLAUDE_EFFORT", "high")
if EFFORT not in EFFORT_LEVELS:
    EFFORT = "high"
# Fast model for /btw one-shot lookups (no --resume, never touches a project
# session, so it can run concurrently with the main task).
QUICK_MODEL = CFG.get("QUICK_MODEL", "sonnet")
# Defaults are intentionally generic so a fresh clone leaks no personal paths;
# real values live in the gitignored bridge.env.
DEFAULT_CWD = CFG.get("DEFAULT_CWD", os.path.expanduser("~"))
# Folders under DEFAULT_CWD that are *groups* of secondary projects rather than
# projects themselves: in the /ls browser they open into their own subfolders
# instead of becoming the cwd. Comma-separated, case-insensitive. Empty default.
GROUP_DIRS = [g.strip().lower()
              for g in CFG.get("GROUP_DIRS", "").split(",")
              if g.strip()]
# Pinned folders OUTSIDE DEFAULT_CWD, surfaced as one-tap shortcuts in /menu
# (the /ls browser only walks DEFAULT_CWD, so these would be unreachable there).
# Comma-separated absolute paths; only the ones that exist are shown. Empty default.
SHORTCUT_DIRS = [p.strip() for p in CFG.get("SHORTCUT_DIRS", "").split(",")
                 if p.strip()]
STATE_PATH = CFG.get("STATE_PATH", "/root/telegram-bridge/state.json")
CLAUDE_TIMEOUT = int(CFG.get("CLAUDE_TIMEOUT", "1800") or "1800")

# Speech-to-text for voice/audio. Two backends, tried in this order:
#   1) STT_CMD  — a LOCAL command (e.g. faster-whisper) the bridge shells out
#      to; the audio path is appended as the last arg, transcript read from
#      stdout. Fully offline, no key, no audio leaves the machine.
#   2) STT_API_KEY — an OpenAI-compatible /audio/transcriptions endpoint
#      (Groq by default; OpenAI by swapping URL+model). Cloud fallback.
# Neither set -> audio messages get a friendly "not configured" notice.
STT_CMD = CFG.get("STT_CMD", "")
STT_API_KEY = (CFG.get("STT_API_KEY", "") or CFG.get("GROQ_API_KEY", "")
               or CFG.get("OPENAI_API_KEY", ""))
STT_API_URL = CFG.get("STT_API_URL",
                      "https://api.groq.com/openai/v1/audio/transcriptions")
STT_MODEL = CFG.get("STT_MODEL", "whisper-large-v3")

API = "https://api.telegram.org/bot%s" % TOKEN

# Stop child claude.exe from flashing a console window when the bridge itself
# runs windowless (pythonw.exe via the scheduled task). No-op off Windows.
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Where downloaded Telegram media (images, later audio) lands. Gitignored;
# files persist through the session so the agent can re-read them.
TMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp")

# One-shot note describing the task that triggered a restart. Written before a
# deliberate restart; read + deleted on the next startup so its description is
# appended to the "Bridge reiniciado" message exactly once. Absent on a plain
# (crash / logon) restart, which then just announces the reboot.
RESTART_NOTE = os.path.join(TMP_DIR, "restart_note.txt")
# The note is renamed to this once the graceful restart actually fires, so a
# failed delete can't leave RESTART_NOTE behind and re-arm the watcher into a
# reboot loop. Read + removed (along with any RESTART_NOTE) on next startup.
RESTART_PENDING = os.path.join(TMP_DIR, "restart_pending.txt")
RESTART_DRAIN_TIMEOUT = 120  # max seconds to let in-flight runs finish before exec
RESTARTING = threading.Event()  # set while draining -> new prompts are deferred


# ----------------------------------------------------------------- state ----
# STATE = {"offset": int, "cwd": str, "model": str, "sessions": {cwd: sid}}
# Re-entrant: a mutation site holds the lock and calls save_state(), which
# re-acquires it. All size-changing mutations (sessions, resume_page) and
# save_state hold this so a worker thread can't resize STATE mid-serialize.
STATE_LOCK = threading.RLock()


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


STATE = load_state()
STATE.setdefault("offset", 0)
STATE.setdefault("cwd", DEFAULT_CWD)
STATE.setdefault("model", MODEL)
STATE.setdefault("effort", EFFORT)
if STATE["effort"] not in EFFORT_LEVELS:
    STATE["effort"] = EFFORT
STATE.setdefault("sessions", {})
STATE.setdefault("resume_page", {})
STATE.setdefault("mcp_disabled", [])  # user-scope MCP servers turned off
STATE.setdefault("live_runs", {})  # crash-recovery snapshots of streaming runs
STATE["ls_base"] = DEFAULT_CWD  # /ls browse root; always reset to home on boot

# Process start time, for /status uptime. time.time() is fine here (this is
# bridge.py, not a workflow script).
START_TIME = time.time()

# Registry of in-flight claude runs, keyed by cwd. Each record holds the live
# subprocess, the running tool list and the start time, so /cc can kill a run
# AND report how far it got. Acts as a non-blocking per-project lock: a second
# prompt for a cwd already running is refused (use /cc) rather than queued, so
# one stuck turn can't block later messages.
ACTIVE = {}
ACTIVE_LOCK = threading.Lock()


class _Counter:
    """Thread-safe in-flight counter. A turn is 'in flight' from when handle()
    enters run_claude until it finishes delivering — used by the restart
    watcher to drain runs before os.execv (a run leaves ACTIVE before its final
    message is sent, so ACTIVE alone would drain too early)."""
    def __init__(self):
        self._n = 0
        self._lock = threading.Lock()

    def increment(self):
        with self._lock:
            self._n += 1

    def decrement(self):
        with self._lock:
            self._n -= 1

    def value(self):
        with self._lock:
            return self._n


INFLIGHT = _Counter()


def inflight_count():
    return INFLIGHT.value()


def cwd_lock(cwd, rec):
    """Non-blocking. Register rec as the active run for cwd; return True if
    acquired, False if a run is already active there."""
    with ACTIVE_LOCK:
        if cwd in ACTIVE:
            return False
        ACTIVE[cwd] = rec
        return True


def cwd_unlock(cwd):
    with ACTIVE_LOCK:
        ACTIVE.pop(cwd, None)


def save_state():
    with STATE_LOCK:
        # Serialize under the lock (snapshot to a string) so a concurrent
        # mutation can't resize a dict mid-dump.
        data = json.dumps(STATE)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, STATE_PATH)


# ------------------------------------------------------------- turn log ----
# One JSON record per turn (outcome ok/error/timeout/cancelled/no_result),
# appended to turns.log and rotated at ~2 MB. Gitignored.
TURNS_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "turns.log")
TURNS_LOG_MAX = 2_000_000
LOG_LOCK = threading.Lock()


def log_event(kind, **fields):
    """Append a timestamped record to turns.log. Never raises."""
    try:
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind}
        rec.update(fields)
        line = json.dumps(rec, ensure_ascii=False)
        with LOG_LOCK:
            try:
                if os.path.getsize(TURNS_LOG) > TURNS_LOG_MAX:
                    os.replace(TURNS_LOG, TURNS_LOG + ".1")
            except OSError:
                pass  # file absent yet -> nothing to rotate
            with open(TURNS_LOG, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(line + "\n")
    except Exception as e:
        sys.stderr.write("log_event error: %s\n" % e)


# -------------------------------------------------------------- telegram ----
def tg(method, params, timeout=70):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request("%s/%s" % (API, method), data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# --- markdown -> Telegram HTML (Telegram HTML only needs & < > escaped) ---
_CODE_BLOCK = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD = re.compile(r"\*\*([^\n]+?)\*\*")
_ITALIC = re.compile(r"(?<![\*\w])\*([^\*\n]+?)\*(?![\*\w])")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$", re.MULTILINE)
_BULLET = re.compile(r"^(\s*)[-*]\s+(?=\S)", re.MULTILINE)
_STRIKE = re.compile(r"~~([^\n]+?)~~")
_QUOTE = re.compile(r"^\s*>\s?")
# A markdown table separator row, e.g. "| --- | :--: |" (the line under the
# header). Its presence is what tells a pipe-laden line apart from prose.
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")

# A quoted block longer than this collapses to a tap-to-expand blockquote so a
# long quote doesn't flood the chat. Telegram renders <blockquote expandable>.
QUOTE_EXPANDABLE_MIN = 400


def _inline_md(s):  # s is already HTML-escaped
    s = _HEADING.sub(r"<b>\1</b>", s)
    s = _BULLET.sub(r"\1• ", s)
    s = _LINK.sub(lambda m: '<a href="%s">%s</a>' % (m.group(2), m.group(1)), s)
    s = _BOLD.sub(r"<b>\1</b>", s)
    s = _STRIKE.sub(r"<s>\1</s>", s)
    s = _ITALIC.sub(r"<i>\1</i>", s)
    return s


def _segment_to_html(seg):
    # Protect inline `code`, escape everything, apply inline markdown to prose.
    out, last = [], 0
    for m in _INLINE_CODE.finditer(seg):
        out.append(_inline_md(html.escape(seg[last:m.start()], quote=False)))
        out.append("<code>%s</code>" % html.escape(m.group(1), quote=False))
        last = m.end()
    out.append(_inline_md(html.escape(seg[last:], quote=False)))
    return "".join(out)


def _split_row(line):
    """Split one markdown table row into trimmed cells, dropping the optional
    leading/trailing pipe."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _render_table(rows):
    """Render a markdown table (list of cell-lists, header first) as aligned
    monospace inside <pre> — Telegram has no <table>. Column widths use the
    raw (visible) cell length; cells are HTML-escaped after alignment so the
    padding still lines up once entities collapse to single glyphs."""
    cols = max(len(r) for r in rows)
    norm = [r + [""] * (cols - len(r)) for r in rows]
    widths = [max(len(row[c]) for row in norm) for c in range(cols)]
    lines = [" | ".join(row[c].ljust(widths[c]) for c in range(cols))
             for row in norm]
    return "<pre>%s</pre>" % html.escape("\n".join(lines), quote=False)


def _prose_to_html(seg):
    """Block-level pass over a prose segment (text between fenced code blocks):
    pull out markdown tables and > blockquotes; everything else is rendered
    line-by-line through the inline renderer."""
    lines = seg.split("\n")
    out, i, n = [], 0, len(lines)
    while i < n:
        line = lines[i]
        # Table: a pipe row immediately followed by a separator row.
        if "|" in line and i + 1 < n and _TABLE_SEP.match(lines[i + 1]):
            rows = [_split_row(line)]
            i += 2  # consume header + separator
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append(_split_row(lines[i]))
                i += 1
            out.append(_render_table(rows))
            continue
        # Blockquote: run of consecutive "> " lines.
        if _QUOTE.match(line):
            q = []
            while i < n and _QUOTE.match(lines[i]):
                q.append(_QUOTE.sub("", lines[i]))
                i += 1
            raw = "\n".join(q)
            inner = "\n".join(_segment_to_html(ql) for ql in q)
            tag = ("blockquote expandable"
                   if len(raw) > QUOTE_EXPANDABLE_MIN else "blockquote")
            out.append("<%s>%s</blockquote>" % (tag, inner))
            continue
        out.append(_segment_to_html(line))
        i += 1
    return "\n".join(out)


def md_to_html(text):
    out, pos = [], 0
    for m in _CODE_BLOCK.finditer(text):
        out.append(_prose_to_html(text[pos:m.start()]))
        out.append("<pre>%s</pre>" % html.escape(m.group(1), quote=False))
        pos = m.end()
    out.append(_prose_to_html(text[pos:]))
    return "".join(out)


def _chunks(text, limit=4000):
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


def send(chat_id, text):
    text = text if text else "(sem saída)"
    for chunk in _chunks(text):
        try:
            tg("sendMessage", {"chat_id": chat_id, "text": md_to_html(chunk),
                               "parse_mode": "HTML",
                               "disable_web_page_preview": "true"})
        except Exception as e:
            # Bad HTML entities (or any send error) -> retry as plain text.
            try:
                tg("sendMessage", {"chat_id": chat_id, "text": chunk})
            except Exception as e2:
                sys.stderr.write("send error: %s / %s\n" % (e, e2))


def typing(chat_id):
    try:
        tg("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass


CLEAR_WINDOW = 100  # how many recent messages /new tries to delete


def clear_chat(chat_id, upto_msg_id):
    """Best-effort 'clear the chat': delete a window of recent messages ending
    at upto_msg_id. The Bot API has no bulk delete, so we walk ids downward and
    swallow failures (a bot can delete its own messages anytime and the owner's
    within 48h; older/undeletable ones just no-op)."""
    if not upto_msg_id:
        return
    for mid in range(int(upto_msg_id), max(0, int(upto_msg_id) - CLEAR_WINDOW),
                      -1):
        try:
            tg("deleteMessage", {"chat_id": chat_id, "message_id": mid})
        except Exception:
            pass


# --- inline keyboards -------------------------------------------------------
# A keyboard is a list of rows; each row a list of (label, callback_data).
def _ikb(rows):
    return json.dumps({"inline_keyboard":
                       [[{"text": lbl, "callback_data": data}
                         for lbl, data in row] for row in rows]})


def send_kb(chat_id, text, rows):
    """Send a message carrying an inline keyboard."""
    try:
        tg("sendMessage", {"chat_id": chat_id, "text": md_to_html(text),
                           "parse_mode": "HTML",
                           "disable_web_page_preview": "true",
                           "reply_markup": _ikb(rows)})
    except Exception:
        tg("sendMessage", {"chat_id": chat_id, "text": text,
                           "reply_markup": _ikb(rows)})


def edit_kb(chat_id, message_id, text, rows):
    """Edit a message in place (text + keyboard) — used to navigate menus
    without spamming new messages."""
    try:
        tg("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                               "text": md_to_html(text), "parse_mode": "HTML",
                               "disable_web_page_preview": "true",
                               "reply_markup": _ikb(rows)})
    except Exception as e:
        sys.stderr.write("edit_kb error: %s\n" % e)


def answer_cb(cb_id, text=None):
    """Acknowledge a callback_query so Telegram stops the spinner; optional
    toast text."""
    params = {"callback_query_id": cb_id}
    if text:
        params["text"] = text
    try:
        tg("answerCallbackQuery", params)
    except Exception:
        pass


# --- live streaming ---------------------------------------------------------
# While a turn streams, ONE Telegram message is edited in place as a "working"
# view (plain text, throttled). On success the progress message collapses to a
# short trace and the full answer is sent as a NEW message — an edit never
# fires a Telegram notification, only a new message does, so the owner is
# pinged exactly once, on the final result.
MIN_INTERVAL = 1.4      # min seconds between progress edits
WINDOW = 3500           # tail of streamed text kept in the progress view


class LiveStream:
    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.buf = []
        self.status = ""
        self.msg_id = None
        self.last = 0.0
        self.lock = threading.Lock()

    def feed(self, text):
        with self.lock:
            self.buf.append(text)
        self.tick()

    def set_status(self, s):
        with self.lock:
            self.status = s
        self.tick(force=True)

    def _text(self):
        body = "".join(self.buf)
        if len(body) > WINDOW:
            body = "…" + body[-WINDOW:]
        cue = self.status
        if cue and body:
            return "%s\n\n%s" % (cue, body)
        return cue or body or "💬 trabalhando…"

    def tick(self, force=False):
        now = time.time()
        with self.lock:
            if not force and now - self.last < MIN_INTERVAL:
                return
            self.last = now
            txt = self._text()
            mid = self.msg_id
        try:
            if mid is None:
                # First paint: silent (disable_notification) so only the final
                # NEW message pings the owner.
                r = tg("sendMessage", {"chat_id": self.chat_id, "text": txt,
                                       "disable_notification": "true"})
                self.msg_id = (r.get("result") or {}).get("message_id")
            else:
                tg("editMessageText", {"chat_id": self.chat_id,
                                       "message_id": mid, "text": txt})
        except Exception:
            pass
        persist_live(self)

    def finish_new(self, body, footer, head="✅ pronto 👇"):
        """Collapse the progress message to a trace and send the full answer as
        a NEW (notifying) message."""
        try:
            if self.msg_id is not None:
                tg("editMessageText", {"chat_id": self.chat_id,
                                       "message_id": self.msg_id,
                                       "text": head + ("\n" + footer if footer
                                                       else "")})
        except Exception:
            pass
        send(self.chat_id, (footer + "\n\n" if footer else "") + (body or ""))
        clear_live(self)

    def cap_in_place(self, banner):
        """Cancel/timeout path: stamp the partial in place with a banner; no new
        message (the owner is already looking)."""
        try:
            if self.msg_id is not None:
                tg("editMessageText", {"chat_id": self.chat_id,
                                       "message_id": self.msg_id,
                                       "text": "%s\n\n%s" % (banner,
                                                             self._text())})
        except Exception:
            pass
        clear_live(self)


def persist_live(live):
    """Snapshot a streaming run (chat, msg id, partial text) into STATE so a
    hard restart can recover it. Cleared on delivery."""
    if live.msg_id is None:
        return
    try:
        with STATE_LOCK:
            STATE.setdefault("live_runs", {})[str(live.msg_id)] = {
                "chat": live.chat_id, "msg_id": live.msg_id,
                "text": live._text()}
            save_state()
    except Exception as e:
        sys.stderr.write("persist_live error: %s\n" % e)


def clear_live(live):
    if live.msg_id is None:
        return
    try:
        with STATE_LOCK:
            STATE.get("live_runs", {}).pop(str(live.msg_id), None)
            save_state()
    except Exception:
        pass


def tg_download_file(file_id, dest_dir, name_hint=None):
    """Resolve a Telegram file_id and download its bytes into dest_dir.
    Returns the absolute local path, or None on failure. Bot API caps
    downloads at 20 MB. name_hint (e.g. the original file name) is used only
    to pick the extension, so the agent's Read tool detects the file type."""
    try:
        info = tg("getFile", {"file_id": file_id})
        fp = (info.get("result") or {}).get("file_path") if info.get("ok") else None
        if not fp:
            return None
        url = "https://api.telegram.org/file/bot%s/%s" % (TOKEN, fp)
        os.makedirs(dest_dir, exist_ok=True)
        ext = os.path.splitext(name_hint or fp)[1] or ".bin"
        dest = os.path.join(dest_dir, "tg_%d%s" % (int(time.time() * 1000), ext))
        with urllib.request.urlopen(url, timeout=120) as r:
            data = r.read()
        with open(dest, "wb") as fh:
            fh.write(data)
        return dest
    except Exception as e:
        sys.stderr.write("download error: %s\n" % e)
        return None


def transcribe_audio(path):
    """Transcribe an audio file to text. Prefers the local STT_CMD backend;
    falls back to the cloud API. Returns the transcript, or None on failure."""
    if STT_CMD:
        return _transcribe_local(path)
    if STT_API_KEY:
        return _transcribe_cloud(path)
    return None


def _transcribe_local(path):
    """Run STT_CMD as a subprocess with the audio path appended as the last
    argument; the transcript is read from stdout. shlex(posix=False) keeps
    Windows backslashes intact; surrounding quotes are stripped per token."""
    try:
        args = [p.strip('"') for p in shlex.split(STT_CMD, posix=False)] + [path]
        # Forward STT_WHISPER_* tunables from bridge.env (CFG) into the child's
        # environment — the helper reads them from os.environ, and CFG values
        # are not otherwise exported.
        env = dict(os.environ)
        for k in ("STT_WHISPER_MODEL", "STT_WHISPER_DEVICE",
                  "STT_WHISPER_COMPUTE", "STT_WHISPER_LANG"):
            if CFG.get(k):
                env[k] = CFG[k]
        proc = subprocess.run(args, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=300,
                              creationflags=CREATE_NO_WINDOW, env=env)
        if proc.returncode != 0:
            sys.stderr.write("stt_cmd rc=%s: %s\n"
                             % (proc.returncode, (proc.stderr or "")[-800:]))
            return None
        return (proc.stdout or "").strip()
    except Exception as e:
        sys.stderr.write("stt_cmd error: %s\n" % e)
        return None


def _transcribe_cloud(path):
    """Upload an audio file to the OpenAI-compatible /audio/transcriptions
    endpoint (Groq by default) and return the transcript text, or None.
    Builds the multipart/form-data body by hand to stay stdlib-only."""
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
        boundary = "----bridge%d" % int(time.time() * 1000)
        bnd = ("--" + boundary).encode()
        body = bytearray()
        for name, value in (("model", STT_MODEL), ("response_format", "json")):
            body += bnd + b"\r\n"
            body += ('Content-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                     % (name, value)).encode()
        body += bnd + b"\r\n"
        body += ('Content-Disposition: form-data; name="file"; filename="%s"\r\n'
                 "Content-Type: application/octet-stream\r\n\r\n"
                 % os.path.basename(path)).encode()
        body += blob + b"\r\n" + bnd + b"--\r\n"
        req = urllib.request.Request(STT_API_URL, data=bytes(body))
        req.add_header("Authorization", "Bearer %s" % STT_API_KEY)
        req.add_header("Content-Type",
                       "multipart/form-data; boundary=%s" % boundary)
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.load(r)
        return (data.get("text") or "").strip()
    except Exception as e:
        sys.stderr.write("transcribe error: %s\n" % e)
        return None


# ---------------------------------------------------------------- claude ----
def run_claude(chat_id, cwd, prompt, _retry=False, rec=None):
    if rec is None:
        rec = {"tools": [], "t_start": time.time(), "proc": None,
               "cancelled": False}
    sid = STATE["sessions"].get(cwd)
    args = [CLAUDE_BIN, "-p", prompt,
            "--model", STATE.get("model", MODEL),
            "--effort", STATE.get("effort", EFFORT),
            "--permission-mode", "bypassPermissions",
            "--output-format", "stream-json", "--verbose",
            "--include-partial-messages"]
    if sid:
        args += ["--resume", sid]

    # MCP toggle: if any user-scope server is turned off, hand claude an
    # explicit config of ONLY the active ones (--strict-mcp-config so it
    # ignores the user config). The temp file is chmod 0600 to keep server
    # tokens out of the argv and is removed after the run.
    mcp_cfg_path = None
    disabled = STATE.get("mcp_disabled", [])
    if disabled:
        servers = user_mcp_servers()
        active = {n: c for n, c in servers.items() if n not in disabled}
        if servers:
            try:
                os.makedirs(TMP_DIR, exist_ok=True)
                mcp_cfg_path = os.path.join(
                    TMP_DIR, "mcp_%d.json" % int(time.time() * 1000))
                with open(mcp_cfg_path, "w", encoding="utf-8") as fh:
                    json.dump({"mcpServers": active}, fh)
                try:
                    os.chmod(mcp_cfg_path, 0o600)
                except OSError:
                    pass  # best-effort on Windows
                args += ["--strict-mcp-config", "--mcp-config", mcp_cfg_path]
            except Exception as e:
                sys.stderr.write("mcp config error: %s\n" % e)
                mcp_cfg_path = None

    env = dict(os.environ)
    # On Windows claude uses the logged-in credentials file; no token needed.
    if OAUTH:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = OAUTH
    if os.name != "nt" and not IS_MAC:
        # Linux/WSL: claude refuses --permission-mode bypassPermissions as root
        # unless it believes it is sandboxed. WSL is the dev's sandbox; risk
        # accepted. macOS runs as a normal (non-root) user, so it needs none of
        # this — and forcing HOME=/root there would break credential lookup.
        # On macOS the PATH (incl. Homebrew, for pdftoppm etc.) comes from the
        # launchd plist; CLAUDE_BIN must be absolute since launchd ignores the
        # shell PATH.
        env["HOME"] = "/root"
        env["IS_SANDBOX"] = "1"
        env.setdefault("PATH",
                       "/root/.local/bin:/usr/local/sbin:/usr/local/bin:"
                       "/usr/sbin:/usr/bin:/sbin:/bin")

    live = rec.get("live") or LiveStream(chat_id)
    rec["live"] = live  # so cancel/timeout can stamp the partial in place

    tools, result_text, new_sid, is_error, err = rec["tools"], None, None, False, ""
    duration_ms = None
    try:
        proc = subprocess.Popen(args, cwd=cwd, env=env,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True, encoding="utf-8",
                                errors="replace", bufsize=1,
                                creationflags=CREATE_NO_WINDOW)
        rec["proc"] = proc  # so /cc can kill this run

        def reader():
            nonlocal result_text, new_sid, is_error, duration_ms
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                et = ev.get("type")
                if et == "system" and ev.get("session_id"):
                    new_sid = ev["session_id"]
                elif et == "stream_event":
                    # Partial deltas: stream visible text, show a transient cue
                    # for thinking / tool use. Ignore thinking/signature/json
                    # deltas (not user-facing prose).
                    se = ev.get("event") or {}
                    st = se.get("type")
                    if st == "content_block_start":
                        blk = se.get("content_block") or {}
                        bt = blk.get("type")
                        if bt == "tool_use":
                            live.set_status("⚙️ %s…" % blk.get("name", "tool"))
                        elif bt == "thinking":
                            live.set_status("🧠 pensando…")
                        elif bt == "text":
                            live.set_status("")
                    elif st == "content_block_delta":
                        d = se.get("delta") or {}
                        if d.get("type") == "text_delta":
                            live.feed(d.get("text", ""))
                elif et == "assistant":
                    for blk in ev.get("message", {}).get("content", []):
                        if isinstance(blk, dict) and blk.get("type") == "tool_use":
                            tools.append(blk.get("name", "?"))
                elif et == "result":
                    if ev.get("session_id"):
                        new_sid = ev["session_id"]
                    is_error = bool(ev.get("is_error"))
                    result_text = ev.get("result")
                    duration_ms = ev.get("duration_ms")

        rt = threading.Thread(target=reader, daemon=True)
        rt.start()
        try:
            proc.wait(timeout=CLAUDE_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            _log_turn(cwd, "timeout", rec, tools)
            live.cap_in_place("⏱️ timeout (%ds)" % CLAUDE_TIMEOUT)
            return tools, None
        rt.join(timeout=10)
        try:
            err = proc.stderr.read() or ""
        except Exception:
            err = ""
    finally:
        if mcp_cfg_path:
            try:
                os.remove(mcp_cfg_path)
            except OSError:
                pass

    # Killed by /cc -> stamp the partial in place (the owner is already looking;
    # no new notifying message). run_claude owns all delivery, so hand back the
    # None sentinel.
    if rec.get("cancelled"):
        _log_turn(cwd, "cancelled", rec, tools)
        elapsed = int(time.time() - rec.get("t_start", time.time()))
        live.cap_in_place("🛑 cancelado (%ds)" % elapsed)
        return tools, None

    # Stale session id -> drop it and retry once from scratch (same LiveStream).
    if is_error and sid and not _retry and "session" in (result_text or "").lower():
        with STATE_LOCK:
            STATE["sessions"].pop(cwd, None)
            save_state()
        return run_claude(chat_id, cwd, prompt, _retry=True, rec=rec)

    if new_sid:
        with STATE_LOCK:
            STATE["sessions"][cwd] = new_sid
            save_state()

    if result_text is None:
        _log_turn(cwd, "no_result", rec, tools)
        msg = "⚠️ sem resultado do claude."
        if err:
            msg += "\n" + err[-1500:]
        live.cap_in_place("⚠️ sem resultado")
        return tools, msg if not live.msg_id else None
    _log_turn(cwd, "error" if is_error else "ok", rec, tools)
    secs = (duration_ms / 1000.0) if duration_ms else (time.time()
                                                       - rec.get("t_start",
                                                                 time.time()))
    footer = "⏱️ %.0fs" % secs
    if tools:
        footer += " · 🔧 " + _tools_summary(tools)
    live.finish_new(result_text, footer)
    return tools, None


def _log_turn(cwd, outcome, rec, tools):
    log_event("turn", cwd=cwd, outcome=outcome,
              model=STATE.get("model"), effort=STATE.get("effort"),
              tools=_tools_summary(tools),
              dur=round(time.time() - rec.get("t_start", time.time()), 1))


def run_quick(chat_id, prompt):
    """One-shot ephemeral lookup: a FRESH session (no --resume), the fast
    QUICK_MODEL, web search allowed. It never touches a project's stored
    session, so it runs concurrently with the main task without disturbing it.
    Returns the answer text."""
    args = [CLAUDE_BIN, "-p", prompt,
            "--model", QUICK_MODEL,
            "--permission-mode", "bypassPermissions",
            "--output-format", "stream-json", "--verbose"]

    env = dict(os.environ)
    if OAUTH:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = OAUTH
    if os.name != "nt" and not IS_MAC:
        env["HOME"] = "/root"
        env["IS_SANDBOX"] = "1"
        env.setdefault("PATH",
                       "/root/.local/bin:/usr/local/sbin:/usr/local/bin:"
                       "/usr/sbin:/usr/bin:/sbin:/bin")

    result_text = None
    try:
        proc = subprocess.Popen(args, cwd=STATE["cwd"], env=env,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True, encoding="utf-8",
                                errors="replace", bufsize=1,
                                creationflags=CREATE_NO_WINDOW)
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("type") == "result":
                result_text = ev.get("result")
        proc.wait(timeout=CLAUDE_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        return "⏱️ /btw: timeout."
    except Exception as e:
        return "💥 /btw erro: %s" % e
    return result_text or "⚠️ /btw: sem resultado."


# -------------------------------------------------------------- commands ----
HELP = (
    "Claude Code via Telegram (owner-only)\n"
    "Mande qualquer texto = prompt pro agente.\n\n"
    "/cd <proj|/caminho/abs> — troca o diretório (e a sessão)\n"
    "/pwd — mostra o diretório/sessão atual\n"
    "/ls — lista projetos em DEFAULT_CWD\n"
    "/new — começa uma sessão nova no dir atual\n"
    "/resume [nº|id] — lista sessões; /resume mais|menos pagina; "
    "retoma pelo nº/id\n"
    "/model <opus|sonnet|haiku> — troca o modelo\n"
    "/effort <low|medium|high|xhigh|max> — esforço de raciocínio\n"
    "/status — uptime, dir, modelo, esforço, MCP servers\n"
    "/menu — abre o menu de botões\n"
    "/btw <pergunta> — lookup rápido efêmero (sessão à parte)\n"
    "/cc — cancela a ação em andamento e reporta até onde foi\n"
    "/mcp — liga/desliga MCP servers user-scope\n"
    "/help — esta ajuda"
)

# Slash-command menu registered with Telegram once at startup (the ≡ button and
# the "/" autocomplete). Keep in sync with HELP and the handlers below.
BOT_COMMANDS = [
    ("menu", "🎛️ Menu interativo (botões)"),
    ("btw", "💡 Pesquisa rápida (sem pausar)"),
    ("cc", "🛑 Cancela e mostra até onde rodou"),
    ("status", "📊 Estado: dir, modelo, effort, sessão"),
    ("model", "🧠 Troca o modelo (Opus/Sonnet/Haiku)"),
    ("effort", "🎚️ Nível de raciocínio (low→max)"),
    ("mcp", "🔌 Liga/desliga os MCP servers"),
    ("ls", "📁 Lista projetos"),
    ("cd", "📂 Troca de diretório/projeto"),
    ("new", "🆕 Sessão nova + volta pra Documents"),
    ("resume", "🔄 Lista/retoma sessões"),
    ("pwd", "📍 Diretório/sessão atual"),
    ("help", "❓ Ajuda e lista de comandos"),
]


# --- menu keyboards (navigated in place via edit_kb) ------------------------
def pinned_dirs():
    """SHORTCUT_DIRS entries that currently exist, in declared order."""
    return [p for p in SHORTCUT_DIRS if os.path.isdir(p)]


def main_menu_kb():
    rows = [
        [("📁 Projetos", "ls"), ("↩️ Sessões", "resume")],
        [("🧠 Modelo", "menu:model"), ("⚡ Esforço", "menu:effort")],
        [("🧩 MCP", "mcp"), ("📊 Status", "status")],
    ]
    # One-tap shortcuts to pinned folders outside DEFAULT_CWD, 2 per row.
    pins = [("📚 %s" % os.path.basename(os.path.normpath(p)), "pin:%d" % i)
            for i, p in enumerate(pinned_dirs())]
    for k in range(0, len(pins), 2):
        rows.append(pins[k:k + 2])
    rows.append([("🆕 Nova sessão", "new")])
    return rows


def model_kb():
    """One row per model. Labels are capitalized for looks; the callback_data
    keeps the lowercase id claude expects."""
    cur = STATE.get("model")
    rows = [[(("✅ " if m == cur else "") + m.capitalize(), "model:" + m)]
            for m in ("opus", "sonnet", "haiku")]
    rows.append([("‹ voltar", "menu:main")])
    return rows


def effort_kb():
    cur = STATE.get("effort")
    rows = [[(("✅ " if e == cur else "") + e, "effort:" + e)]
            for e in EFFORT_LEVELS]
    rows.append([("‹ voltar", "menu:main")])
    return rows


def enter_cwd(chat_id, mid, path):
    """Point cwd at path, then either open the session chooser (when it has
    history) or confirm a fresh session. Shared by the /ls cd: taps and the
    pinned-folder shortcuts."""
    STATE["cwd"] = path
    save_state()
    if list_sessions(path):  # has history -> let the user pick a session
        return edit_kb(chat_id, mid, "📁 %s\nescolha a sessão:" % path,
                       session_choice_kb(path))
    return edit_kb(chat_id, mid, "📁 cwd → %s (sessão nova)" % path, [])


def handle_callback(cb):
    """Route an inline-keyboard tap. Menu navigation edits the message in
    place; actions (status/new) fall through to the text handler."""
    data = cb.get("data", "")
    cb_id = cb.get("id")
    m = cb.get("message") or {}
    chat_id = (m.get("chat") or {}).get("id")
    mid = m.get("message_id")
    if data == "menu:main":
        edit_kb(chat_id, mid, "📋 *Menu*", main_menu_kb())
    elif data == "menu:model":
        edit_kb(chat_id, mid, "🧠 *Modelo*", model_kb())
    elif data == "menu:effort":
        edit_kb(chat_id, mid, "⚡ *Esforço*", effort_kb())
    elif data.startswith("model:"):
        sel = data.split(":", 1)[1]
        if sel in ("opus", "sonnet", "haiku"):
            STATE["model"] = sel
            save_state()
        answer_cb(cb_id, "modelo: %s" % sel)
        return edit_kb(chat_id, mid, "🧠 *Modelo* → %s" % sel, model_kb())
    elif data.startswith("effort:"):
        sel = data.split(":", 1)[1]
        if sel in EFFORT_LEVELS:
            STATE["effort"] = sel
            save_state()
        answer_cb(cb_id, "esforço: %s" % sel)
        return edit_kb(chat_id, mid, "⚡ *Esforço* → %s" % sel, effort_kb())
    elif data == "mcp":
        answer_cb(cb_id)
        return edit_kb(chat_id, mid, mcp_menu_text(), mcp_menu_kb())
    elif data.startswith("mcpx:"):
        name = data.split(":", 1)[1]
        with STATE_LOCK:
            dis = STATE.setdefault("mcp_disabled", [])
            if name in dis:
                dis.remove(name)
                toast = "🟢 %s ligado" % name
            else:
                dis.append(name)
                toast = "🔴 %s desligado" % name
            save_state()
        answer_cb(cb_id, toast)
        return edit_kb(chat_id, mid, mcp_menu_text(), mcp_menu_kb())
    elif data == "mcpinfo":
        return answer_cb(cb_id, "Desligar tira o server deste e dos próximos "
                         "turns, até religar aqui.")
    elif data == "ls":  # open / return to the browse root, in place
        STATE["ls_base"] = DEFAULT_CWD
        answer_cb(cb_id)
        head, kb = ls_view(0)
        return edit_kb(chat_id, mid, head, kb)
    elif data.startswith("lsp:"):  # paginate the current browse base
        answer_cb(cb_id)
        head, kb = ls_view(int(data.split(":", 1)[1]))
        return edit_kb(chat_id, mid, head, kb)
    elif data.startswith("grp:"):  # enter a group folder
        ordered, _ = _ls_ordered(STATE.get("ls_base", DEFAULT_CWD))
        idx = int(data.split(":", 1)[1])
        if 0 <= idx < len(ordered):
            STATE["ls_base"] = os.path.join(STATE["ls_base"], ordered[idx])
        answer_cb(cb_id)
        head, kb = ls_view(0)
        return edit_kb(chat_id, mid, head, kb)
    elif data.startswith("cd:"):  # enter a project as the new cwd
        ordered, _ = _ls_ordered(STATE.get("ls_base", DEFAULT_CWD))
        idx = int(data.split(":", 1)[1])
        if not (0 <= idx < len(ordered)):
            return answer_cb(cb_id, "índice inválido")
        path = os.path.join(STATE["ls_base"], ordered[idx])
        answer_cb(cb_id, ordered[idx])
        return enter_cwd(chat_id, mid, path)
    elif data.startswith("pin:"):  # one-tap shortcut to a pinned folder
        pins = pinned_dirs()
        idx = int(data.split(":", 1)[1])
        if not (0 <= idx < len(pins)):
            return answer_cb(cb_id, "atalho inválido")
        path = pins[idx]
        answer_cb(cb_id, os.path.basename(os.path.normpath(path)))
        return enter_cwd(chat_id, mid, path)
    elif data == "sxnew":  # fresh session in the current cwd
        with STATE_LOCK:
            STATE["sessions"].pop(STATE["cwd"], None)
            save_state()
        answer_cb(cb_id, "sessão nova")
        return edit_kb(chat_id, mid, "🆕 sessão nova em %s" % STATE["cwd"], [])
    elif data.startswith("sx:"):  # resume a specific session in the current cwd
        sid = data.split(":", 1)[1]
        with STATE_LOCK:
            STATE["sessions"][STATE["cwd"]] = sid
            save_state()
        answer_cb(cb_id, "sessão %s" % sid[:8])
        return edit_kb(chat_id, mid, "↩️ sessão → %s\n📁 %s\n"
                       "a próxima mensagem continua essa conversa."
                       % (sid[:8], STATE["cwd"]), session_choice_kb(STATE["cwd"]))
    elif data == "new":  # reset to home + clear chat (pass the menu msg id)
        answer_cb(cb_id)
        return handle(chat_id, "/new", msg_id=mid)
    elif data == "resume":  # global session browser (current project + others)
        answer_cb(cb_id)
        head, kb = global_sessions_kb()
        return edit_kb(chat_id, mid, head, kb)
    elif data.startswith("gsx:"):  # jump to a session's project AND resume it
        sid = data.split(":", 1)[1]
        loc = find_session_file(sid)
        if not loc or not loc[1]:
            return answer_cb(cb_id, "sessão não encontrada")
        path, cwd = loc
        with STATE_LOCK:
            STATE["cwd"] = cwd
            STATE["sessions"][cwd] = sid
            save_state()
        answer_cb(cb_id, "→ %s" % (os.path.basename(os.path.normpath(cwd)) or cwd))
        return edit_kb(chat_id, mid, "↩️ retomando em %s\n🔗 sessão %s\n"
                       "a próxima mensagem continua essa conversa."
                       % (cwd, sid[:8]), [])
    elif data == "status":
        answer_cb(cb_id)
        return handle(chat_id, "/status")
    answer_cb(cb_id)


def set_commands():
    """Register the slash-command menu with Telegram (setMyCommands). Called
    once at startup so the ≡ menu and '/' autocomplete stay in sync."""
    try:
        cmds = json.dumps([{"command": c, "description": d}
                           for c, d in BOT_COMMANDS])
        tg("setMyCommands", {"commands": cmds})
    except Exception as e:
        sys.stderr.write("set_commands error: %s\n" % e)


def fmt_uptime(seconds):
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d:
        parts.append("%dd" % d)
    if h or d:
        parts.append("%dh" % h)
    if m or h or d:
        parts.append("%dm" % m)
    parts.append("%ds" % s)
    return " ".join(parts)


def mcp_servers():
    """Best-effort list of configured MCP server names via `claude mcp list`.
    Returns a short string; never raises. Empty/failed -> a friendly marker."""
    try:
        env = dict(os.environ)
        if OAUTH:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = OAUTH
        proc = subprocess.run([CLAUDE_BIN, "mcp", "list"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=20, env=env,
                              creationflags=CREATE_NO_WINDOW)
        names = []
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            # Lines look like "name: command ... - ✓ Connected"; take the name.
            m = re.match(r"^([A-Za-z0-9_.-]+):\s", line)
            if m:
                names.append(m.group(1))
        return ", ".join(names) if names else "(nenhum)"
    except Exception as e:
        sys.stderr.write("mcp_servers error: %s\n" % e)
        return "(n/d)"


def user_mcp_servers():
    """{name: config} of user-scope MCP servers from ~/.claude.json. Never
    raises; returns {} if the file is absent or unreadable."""
    try:
        p = os.path.join(os.path.expanduser("~"), ".claude.json")
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("mcpServers", {}) or {}
    except Exception:
        return {}


def mcp_menu_text():
    servers = user_mcp_servers()
    if not servers:
        return "🧩 nenhum MCP server user-scope em ~/.claude.json."
    dis = STATE.get("mcp_disabled", [])
    lines = ["🧩 *MCP servers* — toque pra ligar/desligar"]
    for n in sorted(servers):
        lines.append("%s %s" % ("🟢" if n not in dis else "🔴", n))
    lines.append("\n_Desligado continua desligado até religar._")
    return "\n".join(lines)


def mcp_menu_kb():
    servers = user_mcp_servers()
    dis = STATE.get("mcp_disabled", [])
    rows = [[(("🟢 " if n not in dis else "🔴 ") + n, "mcpx:" + n)]
            for n in sorted(servers)]
    rows.append([("ℹ️ info", "mcpinfo")])
    return rows


def _tools_summary(tools):
    if not tools:
        return "nenhuma"
    c = Counter(tools)
    return ", ".join("%s×%d" % (k, v) if v > 1 else k for k, v in c.items())


def cancel_report(cwd, rec):
    """Flag a run as cancelled and kill its subprocess. The run's own thread
    then stamps the partial in place (LiveStream.cap_in_place) — we don't send
    a message here, since run_claude owns all delivery."""
    rec["cancelled"] = True
    proc = rec.get("proc")
    if proc:
        try:
            proc.kill()
        except Exception:
            pass


def cancel_active(chat_id):
    """Cancel every in-flight run (there's usually at most one per cwd)."""
    with ACTIVE_LOCK:
        recs = list(ACTIVE.items())
    if not recs:
        return send(chat_id, "✋ nada rodando agora.")
    for cwd, rec in recs:
        cancel_report(cwd, rec)


def resolve_cwd(arg):
    arg = arg.strip().strip('"').strip("'")
    if not arg:
        return None
    path = arg if arg.startswith("/") else os.path.join(DEFAULT_CWD, arg)
    return path if os.path.isdir(path) else None


SESS_LIST_LIMIT = 10
RESUME_PAGE = 10  # sessions shown per /resume page


def project_dir_for(cwd):
    """Mirror Claude Code's cwd -> ~/.claude/projects/<slug> encoding:
    every non-alphanumeric char becomes '-' (robust to / vs \\)."""
    base = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    return os.path.join(base, re.sub(r"[^a-zA-Z0-9]", "-", cwd))


def list_sessions(cwd, limit=SESS_LIST_LIMIT):
    """Recent sessions for cwd as [(sid, mtime, path)], newest first.
    Only top-level *.jsonl (nested */subagents/*.jsonl are ignored)."""
    d = project_dir_for(cwd)
    try:
        files = [f for f in os.listdir(d) if f.endswith(".jsonl")
                 and os.path.isfile(os.path.join(d, f))]
    except Exception:
        return []
    items = []
    for f in files:
        p = os.path.join(d, f)
        try:
            items.append((f[:-6], os.path.getmtime(p), p))
        except Exception:
            continue
    items.sort(key=lambda x: x[1], reverse=True)
    return items if limit is None else items[:limit]


def session_preview(path, maxlen=60):
    """First real user line of a transcript, for a human-readable list."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 80:
                    break
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if ev.get("type") != "user":
                    continue
                content = ev.get("message", {}).get("content")
                txt = ""
                if isinstance(content, str):
                    txt = content
                elif isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "text":
                            txt = b.get("text", "")
                            break
                        if isinstance(b, str):
                            txt = b
                            break
                txt = (txt or "").strip().replace("\n", " ")
                if txt and not txt.startswith("<"):
                    return txt[:maxlen]
    except Exception:
        pass
    return ""


def find_session(cwd, sid):
    """Match a full id or short prefix against this cwd's sessions."""
    for s, _mt, _p in list_sessions(cwd, limit=None):
        if s == sid or s.startswith(sid):
            return s
    return None


# --- global session browser (sessions across ALL projects) ------------------
GLOBAL_MINE = 6     # current-project sessions shown at the top of /sessions
GLOBAL_OTHER = 8    # other-project sessions shown below, by recency


def _projects_base():
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def session_cwd(path):
    """The real cwd a transcript ran in, read from its events (the slug dir name
    is lossy, the embedded cwd is authoritative). None if not found."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 40:
                    break
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                c = ev.get("cwd")
                if c:
                    return c
    except Exception:
        pass
    return None


def find_session_file(sid):
    """Locate <sid>.jsonl across every project dir -> (path, cwd) or None."""
    base = _projects_base()
    try:
        slugs = os.listdir(base)
    except Exception:
        return None
    for slug in slugs:
        p = os.path.join(base, slug, sid + ".jsonl")
        if os.path.isfile(p):
            return p, session_cwd(p)
    return None


def global_sessions(cur_cwd):
    """(mine, others): mine = recent sessions of cur_cwd as [(sid, mt, path)];
    others = most-recent sessions from OTHER projects as [(sid, mt, path, cwd)]."""
    mine = list_sessions(cur_cwd, limit=GLOBAL_MINE)
    base = _projects_base()
    cur_dir = os.path.normpath(project_dir_for(cur_cwd))
    pool = []
    try:
        slugs = os.listdir(base)
    except Exception:
        slugs = []
    for slug in slugs:
        d = os.path.join(base, slug)
        if not os.path.isdir(d) or os.path.normpath(d) == cur_dir:
            continue
        try:
            files = os.listdir(d)
        except Exception:
            continue
        for f in files:
            if not f.endswith(".jsonl"):
                continue
            p = os.path.join(d, f)
            if not os.path.isfile(p):
                continue
            try:
                pool.append((f[:-6], os.path.getmtime(p), p))
            except Exception:
                continue
    pool.sort(key=lambda x: x[1], reverse=True)
    others = [(s, mt, p, session_cwd(p)) for s, mt, p in pool[:GLOBAL_OTHER]]
    return mine, others


def _sess_btn_label(mt, path, sid, prefix=""):
    when = time.strftime("%d/%m %H:%M", time.localtime(mt))
    prev = session_preview(path, maxlen=28) or sid[:8]
    return ("%s%s · %s" % (prefix, when, prev))[:60]


def global_sessions_kb():
    """(text, keyboard) for /sessions: current project's sessions first, then
    other recent projects. Each button jumps straight to that project+session."""
    cur = STATE["cwd"]
    cur_sid = STATE["sessions"].get(cur)
    mine, others = global_sessions(cur)
    rows = []
    for s, mt, p in mine:
        mark = "✅ " if s == cur_sid else "↩️ "
        rows.append([(mark + _sess_btn_label(mt, p, s), "gsx:" + s)])
    for s, mt, p, cwd in others:
        proj = os.path.basename(os.path.normpath(cwd or "?")) or "?"
        rows.append([(_sess_btn_label(mt, p, s, prefix="📁 %s · " % proj),
                      "gsx:" + s)])
    head = ("🗂️ *Sessões*\n📂 atual: %s\n"
            "as de baixo pulam direto pro projeto delas." % cur)
    if not rows:
        head = "🗂️ Nenhuma sessão encontrada ainda."
    return head, rows


# --- /ls project browser ----------------------------------------------------
PROJECTS_PAGE = 8  # project folders shown per /ls page


def _list_dirs(base):
    try:
        return sorted(d for d in os.listdir(base)
                      if os.path.isdir(os.path.join(base, d)))
    except Exception:
        return []


def _ls_ordered(base):
    """Folders in base as (ordered_list, at_root). At the browse root, GROUP_DIRS
    float to the top; the order is deterministic so cd:/grp: indices stay valid
    page to page."""
    at_root = os.path.normpath(base) == os.path.normpath(DEFAULT_CWD)
    dirs = _list_dirs(base)
    if at_root and GROUP_DIRS:
        groups = [d for d in dirs if d.lower() in GROUP_DIRS]
        normal = [d for d in dirs if d.lower() not in GROUP_DIRS]
        return groups + normal, at_root
    return dirs, at_root


def ls_view(page=0):
    """(text, keyboard) for STATE['ls_base'] at the given page."""
    base = STATE.get("ls_base", DEFAULT_CWD)
    ordered, at_root = _ls_ordered(base)
    total = len(ordered)
    pages = max(1, (total + PROJECTS_PAGE - 1) // PROJECTS_PAGE)
    page = max(0, min(page, pages - 1))
    start = page * PROJECTS_PAGE
    chunk = ordered[start:start + PROJECTS_PAGE]
    rows = []
    if not at_root:  # inside a group -> a row back to the browse root
        rows.append([("‹ %s" % os.path.basename(os.path.normpath(DEFAULT_CWD)),
                      "ls")])
    for j, name in enumerate(chunk):
        idx = start + j
        if at_root and name.lower() in GROUP_DIRS:
            rows.append([("🗂️ %s" % name, "grp:%d" % idx)])
        else:
            rows.append([("📁 %s" % name, "cd:%d" % idx)])
    nav = []
    if page > 0:
        nav.append(("‹", "lsp:%d" % (page - 1)))
    if page < pages - 1:
        nav.append(("›", "lsp:%d" % (page + 1)))
    if nav:
        rows.append(nav)
    head = ("📂 %s\npág. %d/%d · %d pasta(s)" % (base, page + 1, pages, total)
            if total else "📂 %s\n(vazio)" % base)
    return head, rows


def session_choice_kb(cwd):
    """Keyboard to pick a session after entering a project with history:
    🆕 nova + one button per recent session (full sid in callback_data)."""
    cur = STATE["sessions"].get(cwd)
    rows = [[("🆕 nova", "sxnew")]]
    for s, _mt, pth in list_sessions(cwd, limit=8):
        mark = "✅ " if s == cur else ""
        prev = session_preview(pth) or s[:8]
        rows.append([(mark + prev[:40], "sx:" + s)])
    return rows


def handle(chat_id, text, msg_id=None):
    t = text.strip()
    if t.startswith("/"):
        parts = t.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        if cmd in ("/help", "/start"):
            return send(chat_id, HELP)
        if cmd in ("/cc", "/cancel", "/stop"):
            return cancel_active(chat_id)
        if cmd == "/menu":
            return send_kb(chat_id, "📋 *Menu*", main_menu_kb())
        if cmd == "/mcp":
            return send_kb(chat_id, mcp_menu_text(), mcp_menu_kb())
        if cmd == "/btw":
            q = arg.strip()
            if not q:
                return send(chat_id, "use: /btw <pergunta> — lookup rápido "
                            "numa sessão à parte (não mexe no projeto).")
            typing(chat_id)
            return send(chat_id, "💡 " + run_quick(chat_id, q))
        if cmd == "/pwd":
            cwd = STATE["cwd"]
            sid = STATE["sessions"].get(cwd)
            return send(chat_id, "📁 %s\n🧠 modelo: %s\n⚡ esforço: %s\n"
                        "🔗 sessão: %s"
                        % (cwd, STATE.get("model"), STATE.get("effort"),
                           sid or "(nova)"))
        if cmd == "/ls":
            STATE["ls_base"] = DEFAULT_CWD  # always start at the browse root
            head, kb = ls_view(0)
            return send_kb(chat_id, head, kb)
        if cmd == "/cd":
            path = resolve_cwd(arg)
            if not path:
                return send(chat_id, "❌ diretório não existe: %s" % arg)
            STATE["cwd"] = path
            save_state()
            has = "sessão existente" if path in STATE["sessions"] else "sessão nova"
            return send(chat_id, "📁 cwd → %s (%s)" % (path, has))
        if cmd == "/new":
            # Land back home with a fresh session and a clean chat. Only the
            # home session is dropped — other projects' sessions are untouched.
            clear_chat(chat_id, msg_id)
            with STATE_LOCK:
                STATE["cwd"] = DEFAULT_CWD
                STATE["ls_base"] = DEFAULT_CWD
                STATE["sessions"].pop(DEFAULT_CWD, None)
                save_state()
            return send(chat_id, "🆕 de volta em casa (%s) com sessão nova."
                        % DEFAULT_CWD)
        if cmd == "/resume":
            arg = arg.strip()
            low = arg.lower()
            sessions = list_sessions(STATE["cwd"], limit=None)
            cur = STATE["sessions"].get(STATE["cwd"])
            total = len(sessions)
            # --- listing / pagination (blank, "mais"/"menos") ---
            if arg == "" or low in ("mais", "menos", "more", "prev", "+", "-"):
                if total == 0:
                    return send(chat_id, "nenhuma sessão encontrada em %s"
                                % STATE["cwd"])
                pages = (total + RESUME_PAGE - 1) // RESUME_PAGE
                p = STATE.get("resume_page", {}).get(STATE["cwd"], 0)
                if arg == "":
                    p = 0
                elif low in ("mais", "more", "+"):
                    p = min(p + 1, pages - 1)
                else:
                    p = max(p - 1, 0)
                with STATE_LOCK:
                    STATE.setdefault("resume_page", {})[STATE["cwd"]] = p
                    save_state()
                start = p * RESUME_PAGE
                chunk = sessions[start:start + RESUME_PAGE]
                blocks = []
                for j, (s, mt, pth) in enumerate(chunk):
                    when = time.strftime("%d/%m %H:%M", time.localtime(mt))
                    prev = session_preview(pth) or "(sem preview)"
                    mark = "  ◀ atual" if s == cur else ""
                    blocks.append("%d.%s\n🕒 %s   `%s`\n💬 %s"
                                  % (start + j + 1, mark, when, s[:8], prev))
                header = ("📂 Sessões em %s\n%d-%d de %d · pág. %d/%d\n"
                          % (STATE["cwd"], start + 1, start + len(chunk),
                             total, p + 1, pages))
                nav = []
                if p < pages - 1:
                    nav.append("➡️ /resume mais")
                if p > 0:
                    nav.append("⬅️ /resume menos")
                footer = "\n\n▶️ /resume <nº> pra continuar"
                if nav:
                    footer = "\n\n" + "   ".join(nav) + footer
                return send(chat_id, header + "\n" + "\n\n".join(blocks) + footer)
            # --- selection (number over the FULL list, or id/prefix) ---
            match = None
            if arg.isdigit():
                n = int(arg)
                if 1 <= n <= total:
                    match = sessions[n - 1][0]
            if not match:
                match = find_session(STATE["cwd"], arg)
            if not match:
                return send(chat_id, "❌ não achei a sessão: %s\n"
                            "use /resume pra ver a lista." % arg)
            with STATE_LOCK:
                STATE["sessions"][STATE["cwd"]] = match
                save_state()
            return send(chat_id, "↩️ sessão → %s\n"
                        "a próxima mensagem continua essa conversa." % match)
        if cmd == "/model":
            m = arg.strip().lower()
            if m not in ("opus", "sonnet", "haiku"):
                return send(chat_id, "use: /model opus|sonnet|haiku (atual: %s)"
                            % STATE.get("model"))
            STATE["model"] = m
            save_state()
            return send(chat_id, "🧠 modelo → %s" % m)
        if cmd == "/effort":
            e = arg.strip().lower()
            if e not in EFFORT_LEVELS:
                return send(chat_id, "use: /effort %s (atual: %s)"
                            % ("|".join(EFFORT_LEVELS), STATE.get("effort")))
            STATE["effort"] = e
            save_state()
            return send(chat_id, "⚡ esforço → %s" % e)
        if cmd == "/status":
            cwd = STATE["cwd"]
            sid = STATE["sessions"].get(cwd)
            typing(chat_id)  # mcp_servers() shells out; show activity meanwhile
            return send(chat_id,
                        "📊 *Status*\n"
                        "⏱️ uptime: %s\n"
                        "📁 cwd: %s\n"
                        "🧠 modelo: %s\n"
                        "⚡ esforço: %s\n"
                        "🔗 sessão: %s\n"
                        "🧩 MCP: %s"
                        % (fmt_uptime(time.time() - START_TIME), cwd,
                           STATE.get("model"), STATE.get("effort"),
                           sid or "(nova)", mcp_servers()))
        # unknown slash command -> fall through and treat as a prompt

    if RESTARTING.is_set():
        return send(chat_id, "🔄 reiniciando — manda de novo daqui a pouco.")
    cwd = STATE["cwd"]
    rec = {"tools": [], "t_start": time.time(), "proc": None, "cancelled": False}
    # Non-blocking project lock: if a run is already active in this cwd, refuse
    # rather than queue, so one stuck turn can't block later messages.
    if not cwd_lock(cwd, rec):
        return send(chat_id, "⏳ já tem uma ação rodando nesse projeto — /cc "
                    "pra cancelar.")
    INFLIGHT.increment()
    try:
        # run_claude owns delivery (streams live, sends the final answer as a
        # new message). It returns non-None text only on a rare fallback.
        _tools, out = run_claude(chat_id, cwd, text, rec=rec)
    finally:
        cwd_unlock(cwd)
        INFLIGHT.decrement()
    if out is not None:
        send(chat_id, out)


# ----------------------------------------------------------- dispatch ----
def _run_callback(cb):
    try:
        handle_callback(cb)
    except Exception as e:
        sys.stderr.write("callback error: %s\n" % e)
        try:
            answer_cb(cb.get("id"))
        except Exception:
            pass


def process_message(msg):
    """Handle one Telegram message end to end (owner gate, media ingest,
    dispatch to handle). Runs on its own daemon thread per message."""
    frm = (msg.get("from") or {}).get("id")
    chat_id = (msg.get("chat") or {}).get("id")
    text = msg.get("text", "")
    # ---- OWNER GATE: reject everyone else BEFORE touching claude ----
    if frm != OWNER_ID:
        sys.stderr.write("denied uid=%s\n" % frm)
        return

    # Photo (largest size) or image document -> download to tmp/ and hand the
    # agent the file path; its Read tool ingests the image. Caption becomes the
    # prompt; no caption -> a default instruction.
    photo = msg.get("photo")
    doc = msg.get("document")
    file_id = None
    if photo:
        file_id = photo[-1].get("file_id")
    elif doc and (doc.get("mime_type") or "").startswith("image/"):
        file_id = doc.get("file_id")
    if file_id:
        img_path = tg_download_file(file_id, TMP_DIR)
        if not img_path:
            return send(chat_id, "⚠️ não consegui baixar a imagem do Telegram.")
        instruction = (msg.get("caption") or "").strip() or "Analise esta imagem."
        text = ("[Imagem recebida via Telegram, salva em: %s]\n\n%s"
                % (img_path, instruction))

    # Voice / audio / round-video -> download, transcribe, use as prompt.
    voice = msg.get("voice") or msg.get("audio") or msg.get("video_note")
    is_audio_doc = doc and (doc.get("mime_type") or "").startswith("audio/")
    if not file_id and (voice or is_audio_doc):
        aud_path = tg_download_file((voice or doc).get("file_id"), TMP_DIR)
        if not aud_path:
            return send(chat_id, "⚠️ não consegui baixar o áudio do Telegram.")
        if not (STT_CMD or STT_API_KEY):
            return send(chat_id, "🎙️ áudio recebido, mas a transcrição não "
                        "está configurada (defina STT_CMD ou STT_API_KEY em "
                        "bridge.env).")
        typing(chat_id)
        transcript = transcribe_audio(aud_path)
        if not transcript:
            return send(chat_id, "⚠️ não consegui transcrever o áudio.")
        send(chat_id, "🎙️ *transcrição:* %s" % transcript)
        caption = (msg.get("caption") or "").strip()
        text = "%s\n\n%s" % (caption, transcript) if caption else transcript

    # Any other document (PDF, text, code, spreadsheet...) -> download and hand
    # the path to the agent. Its Read tool ingests text/PDF; other formats fall
    # back to its own tools.
    if not text and doc and not is_audio_doc:
        doc_path = tg_download_file(doc.get("file_id"), TMP_DIR,
                                    name_hint=doc.get("file_name"))
        if not doc_path:
            return send(chat_id, "⚠️ não consegui baixar o documento do Telegram.")
        fname = doc.get("file_name") or os.path.basename(doc_path)
        instruction = (msg.get("caption") or "").strip() or "Analise este documento."
        text = ("[Documento recebido via Telegram: %s, salvo em: %s]\n\n%s"
                % (fname, doc_path, instruction))

    if not text:
        return send(chat_id, "(só texto, imagem, áudio e documentos são "
                    "suportados)")
    try:
        handle(chat_id, text, msg_id=msg.get("message_id"))
    except Exception as e:
        send(chat_id, "💥 erro no bridge: %s" % e)
        sys.stderr.write("handle error: %s\n" % e)


# ----------------------------------------------------------- restart ----
def _finalize_orphans():
    """Crash-recovery net. If a hard restart interrupted a streaming run before
    its finish_new(), the snapshot (chat, msg id, partial text) survives in
    STATE["live_runs"]. Collapse the frozen progress message and re-send the
    captured text as a NEW (notifying) message flagged as recovered."""
    runs = list(STATE.get("live_runs", {}).values())
    if not runs:
        return
    for r in runs:
        chat, mid, txt = r.get("chat"), r.get("msg_id"), r.get("text") or ""
        try:
            if mid:
                tg("editMessageText", {"chat_id": chat, "message_id": mid,
                                       "text": "♻️ recuperado após reinício 👇"})
        except Exception:
            pass
        try:
            send(chat, "♻️ *recuperado após reinício*\n\n" + txt)
        except Exception:
            pass
    with STATE_LOCK:
        STATE["live_runs"] = {}
        save_state()


def _do_graceful_restart():
    """Drain in-flight runs (so each still delivers its final message), arm the
    RESTART_PENDING marker, then re-exec in the SAME process (os.execv) — no
    launchd/scheduler respawn throttle, and unlike a hard kickstart -k it never
    kills the child claude mid-response."""
    sys.stderr.write("graceful restart: draining %d run(s)\n" % inflight_count())
    deadline = time.time() + RESTART_DRAIN_TIMEOUT
    while inflight_count() > 0 and time.time() < deadline:
        time.sleep(0.5)
    try:
        if os.path.exists(RESTART_NOTE):
            os.replace(RESTART_NOTE, RESTART_PENDING)
    except Exception as e:
        sys.stderr.write("restart rename error: %s\n" % e)
    sys.stderr.flush()
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        sys.stderr.write("execv error: %s\n" % e)
        os._exit(1)


def _restart_watcher():
    """Daemon: when RESTART_NOTE appears (written deliberately after a change
    that needs a reload), set RESTARTING and fire the graceful restart."""
    while True:
        time.sleep(2)
        if not RESTARTING.is_set() and os.path.exists(RESTART_NOTE):
            RESTARTING.set()
            _do_graceful_restart()


# ------------------------------------------------------------------ main ----
def main():
    # OAUTH is optional: on Windows (and where ~/.claude/.credentials.json
    # exists) claude uses the logged-in credentials, so only the bot token and
    # owner id are strictly required.
    if not TOKEN or not OWNER_ID:
        sys.stderr.write("missing TELEGRAM_BOT_TOKEN / OWNER_ID in %s\n"
                         % ENV_PATH)
        sys.exit(1)
    # A reboot always lands home, never inside the last open project.
    with STATE_LOCK:
        STATE["cwd"] = DEFAULT_CWD
        save_state()
    sys.stderr.write("bridge up. owner=%d model=%s cwd=%s\n"
                     % (OWNER_ID, STATE.get("model"), STATE["cwd"]))
    set_commands()  # register the ≡ menu / "/" autocomplete once
    # Read the restart note from RESTART_PENDING (graceful restart) or
    # RESTART_NOTE (a hard restart that beat the watcher) and remove BOTH — a
    # leftover RESTART_NOTE would immediately re-arm the watcher into a loop.
    note = ""
    for marker in (RESTART_PENDING, RESTART_NOTE):
        try:
            if os.path.exists(marker):
                with open(marker, "r", encoding="utf-8",
                          errors="replace") as fh:
                    note = note or fh.read().strip()
                os.remove(marker)
        except Exception as e:
            sys.stderr.write("restart note error: %s\n" % e)
    # Re-deliver any streaming run a hard restart interrupted, then announce.
    _finalize_orphans()
    msg = ("♻️ *Bridge reiniciado* — no ar de novo.\n🧠 %s · 📁 %s"
           % (STATE.get("model"), STATE["cwd"]))
    if note:
        msg += "\n\n✅ *Antes do reboot:* %s" % note
    try:
        send(OWNER_ID, msg)
    except Exception as e:
        sys.stderr.write("startup notify error: %s\n" % e)
    threading.Thread(target=_restart_watcher, daemon=True).start()
    while True:
        try:
            resp = tg("getUpdates",
                      {"offset": STATE["offset"], "timeout": 50}, timeout=70)
        except Exception as e:
            sys.stderr.write("poll error: %s\n" % e)
            time.sleep(3)
            continue
        for upd in resp.get("result", []):
            STATE["offset"] = upd["update_id"] + 1
            save_state()
            # Inline-keyboard taps. Owner-gated like messages; handled on their
            # own thread so a menu navigation never waits on a running turn.
            cb = upd.get("callback_query")
            if cb:
                if (cb.get("from") or {}).get("id") != OWNER_ID:
                    answer_cb(cb.get("id"))
                    continue
                threading.Thread(target=_run_callback, args=(cb,),
                                 daemon=True).start()
                continue
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            # Each message gets its own daemon thread so a long-running turn
            # never blocks the poll loop — that's what lets /btw and the menu
            # stay responsive mid-action.
            threading.Thread(target=process_message, args=(msg,),
                             daemon=True).start()


if __name__ == "__main__":
    main()
