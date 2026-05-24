# CLAUDE.md

Guidance for Claude Code when working in this repo.

## What this is

A single-file Python bridge (`bridge.py`, stdlib only) that exposes the full
Claude Code agent over Telegram. It long-polls `getUpdates` and shells out to
the `claude` CLI in headless stream-json mode, with persistent per-project
sessions. Runs unchanged on Windows, Linux and WSL (branches on `os.name`).

## Architecture (where things live in bridge.py)

- `load_env` / `CFG` — config from `bridge.env` (see `bridge.env.example`).
- `STATE` / `save_state` — persisted to `state.json`: Telegram `offset`,
  current `cwd`, `model`, per-cwd `sessions` map, and `resume_page` (pagination).
- `md_to_html` — converts Claude's markdown to Telegram HTML.
- `send` / `tg` / `typing` — Telegram transport; `send` chunks at 4000 chars
  and falls back to plain text on bad HTML.
- `run_claude(chat_id, cwd, prompt)` — the agent core. Spawns `claude -p` with
  `--permission-mode bypassPermissions --output-format stream-json --verbose`
  and `--resume <sid>` when a session exists for that cwd. Parses the stream
  for the final `result` and tool-use names. **Frontend-agnostic** — reuse this
  to add a non-Telegram frontend.
- `list_sessions` / `session_preview` / `find_session` — read past session
  transcripts from `~/.claude/projects/<slug>/*.jsonl`, where `<slug>` is the
  cwd with every non-alphanumeric char replaced by `-`.
- `handle` — command router (`/cd /pwd /ls /new /resume /model /help`); unknown
  slash commands fall through and are treated as prompts.
- `main` — poll loop with the **owner gate** (`OWNER_ID`) enforced before any
  `claude` call.

## Invariants / gotchas (do not regress)

- **Owner gate first.** Reject `frm != OWNER_ID` before touching `claude`.
- **UTF-8.** Read the subprocess with `encoding="utf-8", errors="replace"`
  (Windows defaults to cp1252 → mojibake of emoji/accents).
- **No console flash.** Spawn child with `CREATE_NO_WINDOW` on Windows; reroute
  `sys.stderr` to a log file when running windowless (pythonw).
- **Stale session recovery.** If a turn errors mentioning "session", drop the
  stored sid and retry once from scratch.
- **Secrets.** `bridge.env`, `state.json`, `*.log` and `tmp/` are gitignored.
  Never commit them. Never print the bot token.

## Testing a change safely

`bridge.py` runs as a long-lived process (e.g. a scheduled task / systemd unit).
Validate edits without touching the running instance:

```
python -m py_compile bridge.py
python -c "import sys; sys.path.insert(0,'.'); import bridge; print(len(bridge.list_sessions(bridge.STATE['cwd'], limit=None)))"
```

Reloading code requires restarting the process. If you restart from within a
session driven by the bridge itself, launch the restart detached (so it
survives the parent being killed) and add a few seconds of delay so the
triggering reply is delivered first.
