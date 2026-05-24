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
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter

# pythonw.exe (used by the Windows scheduled task) has no console, so
# sys.stderr is None and any .write() would crash. Route it to a log file.
if sys.stderr is None:
    _logdir = os.path.dirname(os.path.abspath(__file__))
    sys.stderr = open(os.path.join(_logdir, "bridge.log"), "a",
                      buffering=1, encoding="utf-8", errors="replace")

if os.name == "nt":
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
DEFAULT_CWD = CFG.get("DEFAULT_CWD", "/mnt/c/Coding")
STATE_PATH = CFG.get("STATE_PATH", "/root/telegram-bridge/state.json")
CLAUDE_TIMEOUT = int(CFG.get("CLAUDE_TIMEOUT", "1800") or "1800")

API = "https://api.telegram.org/bot%s" % TOKEN

# Stop child claude.exe from flashing a console window when the bridge itself
# runs windowless (pythonw.exe via the scheduled task). No-op off Windows.
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


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
STATE.setdefault("sessions", {})
STATE.setdefault("resume_page", {})


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


def _inline_md(s):  # s is already HTML-escaped
    s = _HEADING.sub(r"<b>\1</b>", s)
    s = _BULLET.sub(r"\1• ", s)
    s = _LINK.sub(lambda m: '<a href="%s">%s</a>' % (m.group(2), m.group(1)), s)
    s = _BOLD.sub(r"<b>\1</b>", s)
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


def md_to_html(text):
    out, pos = [], 0
    for m in _CODE_BLOCK.finditer(text):
        out.append(_segment_to_html(text[pos:m.start()]))
        out.append("<pre>%s</pre>" % html.escape(m.group(1), quote=False))
        pos = m.end()
    out.append(_segment_to_html(text[pos:]))
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


# ---------------------------------------------------------------- claude ----
def run_claude(chat_id, cwd, prompt, _retry=False):
    sid = STATE["sessions"].get(cwd)
    args = [CLAUDE_BIN, "-p", prompt,
            "--model", STATE.get("model", MODEL),
            "--permission-mode", "bypassPermissions",
            "--output-format", "stream-json", "--verbose"]
    if sid:
        args += ["--resume", sid]

    env = dict(os.environ)
    # On Windows claude uses the logged-in credentials file; no token needed.
    if OAUTH:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = OAUTH
    if os.name != "nt":
        # claude refuses --permission-mode bypassPermissions as root unless it
        # believes it is sandboxed. WSL is the dev's sandbox; risk accepted.
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
    "/help — esta ajuda"
)


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
        if cmd == "/pwd":
            cwd = STATE["cwd"]
            sid = STATE["sessions"].get(cwd)
            return send(chat_id, "📁 %s\n🧠 modelo: %s\n🔗 sessão: %s"
                        % (cwd, STATE.get("model"), sid or "(nova)"))
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
        # unknown slash command -> fall through and treat as a prompt

    tools, out = run_claude(chat_id, STATE["cwd"], text)
    prefix = ""
    if tools:
        c = Counter(tools)
        summ = ", ".join("%s×%d" % (k, v) if v > 1 else k for k, v in c.items())
        prefix = "🔧 " + summ + "\n\n"
    send(chat_id, prefix + (out or ""))


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
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            frm = (msg.get("from") or {}).get("id")
            chat_id = (msg.get("chat") or {}).get("id")
            text = msg.get("text", "")
            # ---- OWNER GATE: reject everyone else BEFORE touching claude ----
            if frm != OWNER_ID:
                sys.stderr.write("denied uid=%s\n" % frm)
                continue
            if not text:
                send(chat_id, "(só texto é suportado)")
                continue
            try:
                handle(chat_id, text)
            except Exception as e:
                send(chat_id, "💥 erro no bridge: %s" % e)
                sys.stderr.write("handle error: %s\n" % e)


if __name__ == "__main__":
    main()
