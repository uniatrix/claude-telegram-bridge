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
- `tg_download_file(file_id, dest_dir, name_hint=None)` — `getFile` + download
  Telegram media into `tmp/` (gitignored), returns the local path. `name_hint`
  (the original file name) only sets the extension so the agent's Read tool
  detects the type. Used for image input (photo / image document) and for any
  other document (PDF, text, code, spreadsheet…): the file's path is handed to
  the agent so its Read tool ingests it; the message caption becomes the prompt
  (no caption → "Analise esta imagem." / "Analise este documento."). Detection
  lives in `main`, after the owner gate, in order: image → audio → document.
- `transcribe_audio(path)` — voice/audio path. The Read tool can't ingest
  sound, so audio is transcribed first. Dispatches between two backends:
  `_transcribe_local` (preferred, when `STT_CMD` is set) and `_transcribe_cloud`
  (fallback, when `STT_API_KEY` is set).
  - `_transcribe_local` — shells out to `STT_CMD` (a local command, e.g. the
    bundled `stt_faster_whisper.py`), appending the audio path as the last arg
    and reading the transcript from stdout. `shlex.split(posix=False)` keeps
    Windows backslashes; wrapping quotes are stripped per token. Fully offline.
  - `_transcribe_cloud` — hand-built multipart POST (stdlib `urllib`) to an
    OpenAI-compatible `/audio/transcriptions` endpoint (`STT_API_URL`, default
    Groq `whisper-large-v3`; switch to OpenAI via env).
  `main` handles `voice` / `audio` / `video_note` / audio documents after the
  image block, echoes the transcript back, and skips with a notice when neither
  `STT_CMD` nor `STT_API_KEY` is set. OGG is accepted directly (no ffmpeg).
  `stt_faster_whisper.py` is the only file with a third-party dep
  (`faster-whisper`); `bridge.py` stays stdlib-only.
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
  `claude` call. On startup it sends the owner a "Bridge reiniciado" message so
  no reboot (manual, crash-restart or logon launch) ever passes silently. If a
  `RESTART_NOTE` file (`tmp/restart_note.txt`) is present, its text — the task
  that triggered the restart — is appended once, then the file is deleted; a
  plain restart has no note and just announces the reboot. **Workflow: before a
  deliberate restart after some change, write that note file describing what
  was done.**

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
