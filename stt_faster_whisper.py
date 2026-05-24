#!/usr/bin/env python3
"""Local speech-to-text helper for the Telegram bridge.

Usage: python stt_faster_whisper.py <audio-path>

Prints the transcript to stdout (nothing else), so the bridge can capture it.
Runs fully offline via faster-whisper (CTranslate2) — no API key, no audio
leaves the machine. Decodes OGG/Opus directly (bundled PyAV), so no ffmpeg.

This file is the ONLY part of the project with a third-party dependency
(`pip install faster-whisper`); bridge.py itself stays stdlib-only and merely
shells out to this script when STT_CMD points at it.

Tunables via environment:
  STT_WHISPER_MODEL    model size: tiny|base|small|medium|large-v3 (default base)
  STT_WHISPER_DEVICE   cpu|cuda|auto (default cpu — robust, no CUDA libs needed)
  STT_WHISPER_COMPUTE  int8|int8_float16|float16|float32 (default int8)
  STT_WHISPER_LANG     force a language e.g. pt (default: auto-detect)
"""
import os
import sys


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("usage: stt_faster_whisper.py <audio-path>\n")
        sys.exit(2)
    path = sys.argv[1]

    from faster_whisper import WhisperModel  # imported here so usage errors are cheap

    model = WhisperModel(
        os.environ.get("STT_WHISPER_MODEL", "base"),
        device=os.environ.get("STT_WHISPER_DEVICE", "cpu"),
        compute_type=os.environ.get("STT_WHISPER_COMPUTE", "int8"),
    )
    segments, _info = model.transcribe(
        path,
        language=os.environ.get("STT_WHISPER_LANG") or None,
        vad_filter=True,
    )
    sys.stdout.write("".join(seg.text for seg in segments).strip())


if __name__ == "__main__":
    main()
