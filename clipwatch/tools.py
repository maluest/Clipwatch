"""Find ffmpeg / ffprobe."""
from __future__ import annotations

import json
import shutil
import subprocess


def _find(name: str) -> str:
    exe = shutil.which(name)
    if not exe:
        raise SystemExit(
            f"Couldn't find {name}. Install it first:\n"
            "  Windows:  winget install Gyan.FFmpeg   (then close and reopen the terminal)\n"
            "  Mac:      brew install ffmpeg"
        )
    return exe


def ffmpeg_exe() -> str:
    return _find("ffmpeg")


def ffprobe_exe() -> str:
    return _find("ffprobe")


def probe(path: str) -> dict:
    out = subprocess.run(
        [ffprobe_exe(), "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, errors="replace", check=True,
    ).stdout
    return json.loads(out)


def video_size(path: str) -> tuple[int, int]:
    for s in probe(path)["streams"]:
        if s.get("codec_type") == "video":
            return int(s["width"]), int(s["height"])
    raise ValueError(f"No video in {path}")


def has_audio(path: str) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe(path)["streams"])
