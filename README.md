# claude-telegram-bridge

Drive the **full Claude Code agent from Telegram** — read, edit and run code in
your projects from your phone, with persistent per-project sessions so the
conversation accumulates exactly like a long desktop session.

It is a single ~500-line Python file, **standard library only** (no `pip`
install), that long-polls Telegram and shells out to the `claude` CLI in
headless stream-json mode. The same file runs on Windows, Linux and WSL.

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
  (bold, italic, inline `code`, fenced blocks, links, headings, bullets),
  long replies are chunked at 4000 chars, and a "typing…" indicator runs while
  the agent works.
- **Tool transparency** — each reply is prefixed with a `🔧` summary of the
  tools the agent used that turn.
- **Owner-only** — hard allowlist on a single Telegram user id.

---

## How it works

```
Telegram getUpdates (long poll)
   └─ owner gate (OWNER_ID)            ← rejects everyone else first
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

---

## Commands

| Command | What it does |
| --- | --- |
| *(any text)* | Sent to the agent as a prompt in the current project's session |
| `/cd <name\|/abs/path>` | Switch project (relative names resolve under `DEFAULT_CWD`); each project keeps its own session |
| `/pwd` | Show the current directory, model and session id |
| `/ls` | List project folders under `DEFAULT_CWD` |
| `/new` | Start a fresh session in the current directory |
| `/resume` | List this project's past sessions (paginated, newest first) |
| `/resume <n>` | Resume session number `n` from the list |
| `/resume mais` / `/resume menos` | Next / previous page of sessions |
| `/model <opus\|sonnet\|haiku>` | Switch model |
| `/help` | Show help |

## Configuration (`bridge.env`)

| Key | Required | Description |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | ✅ | Bot token from @BotFather |
| `OWNER_ID` | ✅ | Your numeric Telegram user id (the only allowed sender) |
| `CLAUDE_BIN` | – | Path to the `claude` binary (default: `claude` on PATH) |
| `CLAUDE_MODEL` | – | `opus` / `sonnet` / `haiku` (default `opus`) |
| `DEFAULT_CWD` | – | Starting directory; parent of your projects |
| `STATE_PATH` | – | Where session/offset state is persisted |
| `CLAUDE_TIMEOUT` | – | Max seconds per turn before the agent is killed (default 1800) |
| `CLAUDE_CODE_OAUTH_TOKEN` | – | Only if `claude` is not already logged in on the host |

## State & logs

- `state.json` — Telegram offset, current cwd, model, and the per-project
  session id map. Gitignored.
- `bridge.log` — stderr (used when run windowless, where stderr is otherwise
  unavailable). Gitignored.

## Notes / gotchas

- **UTF-8:** the bridge reads the `claude` stream with `encoding="utf-8"` to
  avoid mojibake of emoji and accented text on Windows (where the subprocess
  default is cp1252).
- **No console flash:** on Windows under `pythonw.exe`, child `claude.exe` is
  spawned with `CREATE_NO_WINDOW` so no console window pops up.
- **Telegram formatting:** HTML (`parse_mode=HTML`) is used rather than
  MarkdownV2 (which would require escaping too many characters); if a chunk's
  HTML is somehow invalid, `send()` falls back to plain text for that chunk.

## License

MIT — see [LICENSE](LICENSE).
