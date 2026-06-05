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
  `claude` call. Each message and each callback tap runs on its own daemon
  thread (`process_message` / `_run_callback`) so a long turn never blocks the
  poll loop. On startup it resets `cwd` to home (`DEFAULT_CWD`), re-delivers any
  interrupted streaming run (`_finalize_orphans`), and sends the owner a
  "Bridge reiniciado" message so no reboot ever passes silently. If a restart
  note (`tmp/restart_note.txt`, or `restart_pending.txt` after a graceful
  restart) is present, its text is appended once and both markers are removed.
- **Restart workflow (`_restart_watcher` / `os.execv`).** To reload code after
  a change, **write `tmp/restart_note.txt` describing what changed and stop — do
  NOT run `launchctl kickstart` / kill the process yourself.** A daemon watcher
  sees the note, sets `RESTARTING` (new prompts are deferred), drains in-flight
  runs (`inflight_count()` → 0, capped at `RESTART_DRAIN_TIMEOUT`) so each still
  delivers its final message, renames the note to `restart_pending.txt`, and
  re-execs in the same PID via `os.execv`. This replaces a hard kickstart that
  would kill the child `claude` mid-response and drop the final message.
- **Live streaming (`LiveStream`).** A turn edits one message in place as a
  throttled "working" view; on completion it collapses to a `✅` trace and sends
  the full answer as a **new** message (edits don't notify, new messages do).
  `run_claude` owns delivery and returns `(tools, None)` on success. While a run
  streams, `persist_live` snapshots it into `STATE["live_runs"]` for crash
  recovery; `clear_live` removes the snapshot on delivery.

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

Reloading code requires restarting the process. **Do not kill or kickstart the
bridge yourself** — write `tmp/restart_note.txt` describing what changed and
stop. The `_restart_watcher` daemon drains in-flight runs and re-execs the
process gracefully (`os.execv`), so the triggering reply is delivered first and
the child `claude` is never killed mid-response.
