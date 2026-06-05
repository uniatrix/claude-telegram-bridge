# claude-telegram-bridge

Drive the **full Claude Code agent from Telegram** — read, edit and run code in
your projects from your phone, with persistent per-project sessions so the
conversation accumulates exactly like a long desktop session.

It is a single Python file, **standard library only** (no `pip` install), that
long-polls Telegram and shells out to the `claude` CLI in headless stream-json
mode. The same file runs on Windows, macOS, Linux and WSL. The only optional
extra is `stt_faster_whisper.py`, a small helper for fully-local voice
transcription.

---

## ⚠️ Security — read this first

This bridge runs `claude` with `--permission-mode bypassPermissions` and **all
tools enabled**. Any message it accepts is, in effect, **arbitrary remote code
execution on the host machine** (it can read your files, edit them, run shell
commands, push to git, call your MCP servers, etc.).

The **only** barrier is a single-owner allowlist: every update is checked
against `OWNER_ID` **before** `claude` is ever invoked; everyone else is
silently dropped. Because of that:

- Keep `bridge.env` private (`chmod 600` / locked ACL). It is gitignored.
- Treat the bot token like a password. If it ever leaks, rotate it in
  @BotFather immediately — anyone with the token can message your bot, and the
  owner gate is the only thing stopping them.
- Run it only on a machine you control. This is a personal/solo tool, not a
  multi-tenant service.

You accept this risk by running it.

---

## Features

- **Full agent, not a wrapper** — keeps your `CLAUDE.md`, memory, MCP servers
  and skills (it does *not* run `--bare`).
- **Persistent sessions per project** — each working directory has its own
  conversation, resumed automatically via `--resume` so context carries across
  messages and across bridge restarts.
- **Session switching** — list past sessions of the current project and resume
  any of them (`/resume`), with pagination.
- **Project hopping** — `/ls` to list projects, `/cd` to switch (each keeps its
  own session).
- **Mobile-friendly output** — Claude's markdown is converted to Telegram HTML
  (bold, italic, strikethrough, inline `code`, fenced blocks, links, headings,
  bullets, and `>` blockquotes — long quotes collapse to tap-to-expand).
  Markdown tables become aligned monospace blocks (Telegram has no `<table>`).
  Long replies are chunked at 4000 chars, and a "typing…" indicator runs while
  the agent works.
- **Tool transparency** — each reply is prefixed with a `🔧` summary of the
  tools the agent used that turn.
- **Image input** — attach a photo (or an image file) with a caption; the
  caption becomes the prompt and the agent sees the image via its Read tool.
  No caption falls back to "Analise esta imagem."
- **Voice / audio input** — voice messages, audio files and round video notes
  are transcribed to text and sent to the agent as a prompt; the transcript is
  echoed back so you can see what was understood. Two backends: a **local**
  command (`STT_CMD`, e.g. the bundled faster-whisper helper — fully offline,
  no key) or a **cloud** OpenAI-compatible Whisper endpoint (`STT_API_KEY`,
  Groq by default). Optional — without either, audio gets a "not configured"
  notice.
- **Document input** — send any file (PDF, text, code, spreadsheet…) with a
  caption; it is downloaded and its path handed to the agent, which reads it
  with its Read tool (text/PDF) or its other tools. No caption falls back to
  "Analise este documento."
- **Restart notification** — on every startup the bridge messages the owner
  ("Bridge reiniciado — no ar"), so no reboot (manual, crash-restart or logon
  launch) ever passes silently, even one that killed an in-flight turn. A
  deliberate restart after a change can leave a one-shot note
  (`tmp/restart_note.txt`) whose text is appended to that message, so you learn
  *what* was done; a plain restart just announces the reboot.
- **Owner-only** — hard allowlist on a single Telegram user id.

---

## How it works

```
Telegram getUpdates (long poll)
   └─ owner gate (OWNER_ID)            ← rejects everyone else first
        ├─ photo / image doc?          ← download to tmp/, caption = prompt
        ├─ voice / audio / video note? ← download, transcribe → prompt
        ├─ other document?             ← download to tmp/, path → prompt
        └─ handle(text)
             ├─ /commands              ← cd / pwd / ls / new / resume / model
             └─ run_claude(cwd, text)
                   └─ claude -p <text> --model <m>
                        --permission-mode bypassPermissions
                        --output-format stream-json --verbose
                        [--resume <session-id-for-this-cwd>]
                   └─ parse stream-json → final result + tool names
        └─ send() → markdown → Telegram HTML, chunked
```

Per-directory session ids are stored in `state.json` and reused on the next
message. If a stored session goes stale, the bridge drops it and retries once
from scratch.

### Extending beyond Telegram

The Telegram-specific parts (polling, HTML formatting, the `send()`/`tg()`
helpers) are thin. The core — `run_claude(cwd, prompt)`, the per-cwd session
map, and the `/command` handling in `handle()` — is frontend-agnostic. To add
another frontend (Discord, Slack, a CLI, an HTTP endpoint, …) you reuse
`run_claude` and `handle`, and swap the transport layer (how you receive a
message and how you deliver `send()`'s text). Nothing in the agent core assumes
Telegram.

---

## Requirements

- Python 3.8+ (stdlib only).
- [Claude Code](https://claude.com/claude-code) CLI installed and logged in
  (`claude` on PATH, or set `CLAUDE_BIN`).
- A Telegram bot token and your numeric Telegram user id.
- *(optional, for local voice transcription)* `pip install faster-whisper` —
  used by the bundled `stt_faster_whisper.py` helper. This is the only optional
  third-party dependency; `bridge.py` itself stays stdlib-only and just shells
  out to the helper.

## Setup

1. **Create a bot.** Message [@BotFather](https://t.me/BotFather) → `/newbot` →
   copy the token.
2. **Find your user id.** Message [@userinfobot](https://t.me/userinfobot), or
   send your new bot a message and read the id from `bridge.log`.
3. **Log in to Claude Code** on the host (`claude` once interactively), or set
   `CLAUDE_CODE_OAUTH_TOKEN` in `bridge.env`.
4. **Configure.** `cp bridge.env.example bridge.env` and fill it in
   (see the table below). Lock the file down.
5. **Run.** `python bridge.py` — or set it up to autostart (below).

### Autostart

**Windows (Scheduled Task, windowless via `pythonw.exe`):** run at logon,
"Restart on failure", no time limit. Example action:

```
Program:   C:\Python313\pythonw.exe
Arguments: bridge.py
Start in:  C:\path\to\claude-telegram-bridge
```

**Linux / WSL (systemd user service or a simple unit):**

```ini
[Service]
ExecStart=/usr/bin/python3 /path/to/bridge.py
Restart=always
WorkingDirectory=/path/to/claude-telegram-bridge
```

**macOS (launchd LaunchAgent):** drop the plist below at
`~/Library/LaunchAgents/com.you.claude-telegram-bridge.plist`, then
`launchctl load ~/Library/LaunchAgents/com.you.claude-telegram-bridge.plist`
(runs at login; `KeepAlive` restarts it on crash; `launchctl unload <plist>` to
stop). launchd does **not** inherit your shell `PATH`, so two things matter:
set `CLAUDE_BIN` to the absolute path in `bridge.env`
(e.g. `/Users/you/.local/bin/claude`), and give the plist a `PATH` that includes
Homebrew so the agent can still find tools like `pdftoppm`.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.you.claude-telegram-bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/you/claude-telegram-bridge/bridge.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/you/claude-telegram-bridge</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/you/claude-telegram-bridge/bridge.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/you/claude-telegram-bridge/bridge.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/you/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
```

---

## Commands

| Command | What it does |
| --- | --- |
| *(any text)* | Sent to the agent as a prompt in the current project's session |
| *(photo + caption)* | Photo is downloaded to `tmp/`; the caption is the prompt and the agent reads the image (no caption → "Analise esta imagem.") |
| *(voice / audio note)* | Audio is downloaded, transcribed to text (Whisper), echoed back, and sent to the agent as the prompt (needs `STT_CMD` or `STT_API_KEY`) |
| *(document + caption)* | File is downloaded to `tmp/`; the caption is the prompt and the agent reads the file (no caption → "Analise este documento.") |
| `/cd <name\|/abs/path>` | Switch project (relative names resolve under `DEFAULT_CWD`); each project keeps its own session |
| `/pwd` | Show the current directory, model and session id |
| `/ls` | List project folders under `DEFAULT_CWD` |
| `/new` | Start a fresh session in the current directory |
| `/resume` | List this project's past sessions (paginated, newest first) |
| `/resume <n>` | Resume session number `n` from the list |
| `/resume mais` / `/resume menos` | Next / previous page of sessions |
| `/model <opus\|sonnet\|haiku>` | Switch model |
| `/effort <low\|medium\|high\|xhigh\|max>` | Set reasoning effort (passed to `claude --effort`) |
| `/status` | Uptime, cwd, model, effort, session id, active MCP servers |
| `/menu` | Open the inline-button menu (model / effort / status / projects / sessions) |
| `/btw <question>` | Quick one-shot lookup in a **separate** ephemeral session (fast model, never touches the project session — runs concurrently with the main task) |
| `/cc` (`/cancel`, `/stop`) | Cancel the in-flight run and report how far it got (tools used, elapsed) |
| `/help` | Show help |

A prompt acquires a **non-blocking per-project lock**: if a run is already
active in that directory, the bridge replies "busy — use `/cc`" instead of
queueing, so one stuck turn can't block later messages.

Each message and each button tap is handled on its own daemon thread, so a
long-running turn never blocks the poll loop — `/btw` and the menu stay
responsive mid-action.

The slash-command menu is registered with Telegram at startup
(`setMyCommands`), so the ≡ menu and `/` autocomplete stay in sync.

## Configuration (`bridge.env`)

| Key | Required | Description |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | ✅ | Bot token from @BotFather |
| `OWNER_ID` | ✅ | Your numeric Telegram user id (the only allowed sender) |
| `CLAUDE_BIN` | – | Path to the `claude` binary (default: `claude` on PATH) |
| `CLAUDE_MODEL` | – | `opus` / `sonnet` / `haiku` (default `opus`) |
| `CLAUDE_EFFORT` | – | `low` / `medium` / `high` / `xhigh` / `max` reasoning effort (default `high`) |
| `DEFAULT_CWD` | – | Starting directory; parent of your projects |
| `STATE_PATH` | – | Where session/offset state is persisted |
| `CLAUDE_TIMEOUT` | – | Max seconds per turn before the agent is killed (default 1800) |
| `CLAUDE_CODE_OAUTH_TOKEN` | – | Only if `claude` is not already logged in on the host |
| `STT_CMD` | – | Local transcription command (tried first); audio path appended as last arg, transcript read from stdout. Empty = use cloud |
| `STT_API_KEY` | – | Cloud Whisper key (fallback when `STT_CMD` empty). Also accepts `GROQ_API_KEY` / `OPENAI_API_KEY` |
| `STT_API_URL` | – | Cloud transcription endpoint (default Groq; set to OpenAI's to switch) |
| `STT_MODEL` | – | Cloud transcription model (default `whisper-large-v3`) |

## State & logs

- `state.json` — Telegram offset, current cwd, model, and the per-project
  session id map. Gitignored.
- `bridge.log` — stderr (used when run windowless, where stderr is otherwise
  unavailable). Gitignored.
- `tmp/` — media downloaded from Telegram (images, audio, documents), kept so
  the agent can re-read them within the session, plus the one-shot
  `restart_note.txt`. Gitignored.

## Notes / gotchas

- **UTF-8:** the bridge reads the `claude` stream with `encoding="utf-8"` to
  avoid mojibake of emoji and accented text on Windows (where the subprocess
  default is cp1252).
- **No console flash:** on Windows under `pythonw.exe`, child `claude.exe` is
  spawned with `CREATE_NO_WINDOW` so no console window pops up.
- **Telegram formatting:** HTML (`parse_mode=HTML`) is used rather than
  MarkdownV2 (which would require escaping too many characters); if a chunk's
  HTML is somehow invalid, `send()` falls back to plain text for that chunk.
- **PDF analysis:** documents are analysed by the agent's own Read tool, not by
  the bridge. Text-based PDFs work anywhere, but **scanned / image-only PDFs
  need `poppler` (`pdftoppm`) installed on the host** so the Read tool can
  rasterize pages for vision (`scoop install poppler` on Windows,
  `brew install poppler` on macOS, `apt install poppler-utils` on Linux).

## License

MIT — see [LICENSE](LICENSE).
