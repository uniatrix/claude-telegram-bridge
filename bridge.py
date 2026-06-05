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
DEFAULT_CWD = CFG.get("DEFAULT_CWD", "/mnt/c/Coding")
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


# ----------------------------------------------------------------- state ----
# STATE = {"offset": int, "cwd": str, "model": str, "sessions": {cwd: sid}}
STATE_LOCK = threading.Lock()


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

# Process start time, for /status uptime. time.time() is fine here (this is
# bridge.py, not a workflow script).
START_TIME = time.time()


def save_state():
    with STATE_LOCK:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(STATE, fh)
        os.replace(tmp, STATE_PATH)


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
def run_claude(chat_id, cwd, prompt, _retry=False):
    sid = STATE["sessions"].get(cwd)
    args = [CLAUDE_BIN, "-p", prompt,
            "--model", STATE.get("model", MODEL),
            "--effort", STATE.get("effort", EFFORT),
            "--permission-mode", "bypassPermissions",
            "--output-format", "stream-json", "--verbose"]
    if sid:
        args += ["--resume", sid]

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

    stop = threading.Event()

    def keepalive():
        while not stop.is_set():
            typing(chat_id)
            stop.wait(4)

    threading.Thread(target=keepalive, daemon=True).start()

    tools, result_text, new_sid, is_error, err = [], None, None, False, ""
    try:
        proc = subprocess.Popen(args, cwd=cwd, env=env,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True, encoding="utf-8",
                                errors="replace", bufsize=1,
                                creationflags=CREATE_NO_WINDOW)

        def reader():
            nonlocal result_text, new_sid, is_error
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
                elif et == "assistant":
                    for blk in ev.get("message", {}).get("content", []):
                        if isinstance(blk, dict) and blk.get("type") == "tool_use":
                            tools.append(blk.get("name", "?"))
                elif et == "result":
                    if ev.get("session_id"):
                        new_sid = ev["session_id"]
                    is_error = bool(ev.get("is_error"))
                    result_text = ev.get("result")

        rt = threading.Thread(target=reader, daemon=True)
        rt.start()
        try:
            proc.wait(timeout=CLAUDE_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            stop.set()
            return tools, "⏱️ timeout: ação passou de %ds e foi abortada." % CLAUDE_TIMEOUT
        rt.join(timeout=10)
        try:
            err = proc.stderr.read() or ""
        except Exception:
            err = ""
    finally:
        stop.set()

    # Stale session id -> drop it and retry once from scratch.
    if is_error and sid and not _retry and "session" in (result_text or "").lower():
        STATE["sessions"].pop(cwd, None)
        save_state()
        return run_claude(chat_id, cwd, prompt, _retry=True)

    if new_sid:
        STATE["sessions"][cwd] = new_sid
        save_state()

    if result_text is None:
        msg = "⚠️ sem resultado do claude."
        if err:
            msg += "\n" + err[-1500:]
        return tools, msg
    return tools, result_text


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
    "/help — esta ajuda"
)

# Slash-command menu registered with Telegram once at startup (the ≡ button and
# the "/" autocomplete). Keep in sync with HELP and the handlers below.
BOT_COMMANDS = [
    ("cd", "troca o diretório (e a sessão)"),
    ("pwd", "diretório/sessão atual"),
    ("ls", "lista projetos em DEFAULT_CWD"),
    ("new", "sessão nova no dir atual"),
    ("resume", "lista/retoma sessões"),
    ("model", "troca o modelo (opus|sonnet|haiku)"),
    ("effort", "esforço (low|medium|high|xhigh|max)"),
    ("status", "uptime, dir, modelo, esforço, MCP"),
    ("menu", "menu de botões"),
    ("btw", "lookup rápido efêmero (sessão à parte)"),
    ("help", "esta ajuda"),
]


# --- menu keyboards (navigated in place via edit_kb) ------------------------
def main_menu_kb():
    return [
        [("📁 Projetos", "ls"), ("↩️ Sessões", "resume")],
        [("🧠 Modelo", "menu:model"), ("⚡ Esforço", "menu:effort")],
        [("📊 Status", "status"), ("🆕 Nova sessão", "new")],
    ]


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
    elif data in ("status", "new", "ls", "resume"):
        answer_cb(cb_id)
        return handle(chat_id, "/" + data)
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


def handle(chat_id, text):
    t = text.strip()
    if t.startswith("/"):
        parts = t.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        if cmd in ("/help", "/start"):
            return send(chat_id, HELP)
        if cmd == "/menu":
            return send_kb(chat_id, "📋 *Menu*", main_menu_kb())
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
            try:
                items = sorted(d for d in os.listdir(DEFAULT_CWD)
                               if os.path.isdir(os.path.join(DEFAULT_CWD, d)))
                return send(chat_id, "Projetos em %s:\n%s"
                            % (DEFAULT_CWD, "\n".join(items) or "(vazio)"))
            except Exception as e:
                return send(chat_id, "erro: %s" % e)
        if cmd == "/cd":
            path = resolve_cwd(arg)
            if not path:
                return send(chat_id, "❌ diretório não existe: %s" % arg)
            STATE["cwd"] = path
            save_state()
            has = "sessão existente" if path in STATE["sessions"] else "sessão nova"
            return send(chat_id, "📁 cwd → %s (%s)" % (path, has))
        if cmd == "/new":
            STATE["sessions"].pop(STATE["cwd"], None)
            save_state()
            return send(chat_id, "🆕 sessão reiniciada em %s" % STATE["cwd"])
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

    tools, out = run_claude(chat_id, STATE["cwd"], text)
    prefix = ""
    if tools:
        c = Counter(tools)
        summ = ", ".join("%s×%d" % (k, v) if v > 1 else k for k, v in c.items())
        prefix = "🔧 " + summ + "\n\n"
    send(chat_id, prefix + (out or ""))


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
        handle(chat_id, text)
    except Exception as e:
        send(chat_id, "💥 erro no bridge: %s" % e)
        sys.stderr.write("handle error: %s\n" % e)


# ------------------------------------------------------------------ main ----
def main():
    # OAUTH is optional: on Windows (and where ~/.claude/.credentials.json
    # exists) claude uses the logged-in credentials, so only the bot token and
    # owner id are strictly required.
    if not TOKEN or not OWNER_ID:
        sys.stderr.write("missing TELEGRAM_BOT_TOKEN / OWNER_ID in %s\n"
                         % ENV_PATH)
        sys.exit(1)
    sys.stderr.write("bridge up. owner=%d model=%s cwd=%s\n"
                     % (OWNER_ID, STATE.get("model"), STATE["cwd"]))
    set_commands()  # register the ≡ menu / "/" autocomplete once
    # Announce every startup to the owner so no reboot (mine, a crash-restart
    # or a logon launch) ever passes silently — even one that killed the
    # session before a reply could be sent. If a restart note was left behind
    # (a deliberate restart after some task), append it once, then clear it.
    note = ""
    try:
        if os.path.exists(RESTART_NOTE):
            with open(RESTART_NOTE, "r", encoding="utf-8", errors="replace") as fh:
                note = fh.read().strip()
            os.remove(RESTART_NOTE)
    except Exception as e:
        sys.stderr.write("restart note error: %s\n" % e)
    msg = ("♻️ *Bridge reiniciado* — no ar de novo.\n🧠 %s · 📁 %s"
           % (STATE.get("model"), STATE["cwd"]))
    if note:
        msg += "\n\n✅ *Antes do reboot:* %s" % note
    try:
        send(OWNER_ID, msg)
    except Exception as e:
        sys.stderr.write("startup notify error: %s\n" % e)
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
