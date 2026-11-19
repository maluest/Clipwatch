"""Record a live stream into short rolling segments on disk.

Twitch is pulled with Streamlink (it skips Twitch's "Commercial break in progress" ads);
YouTube and Kick with yt-dlp. The video is piped into ffmpeg, which cuts it into ~4 second
.ts pieces without re-encoding. We keep the last few minutes so that when chat spikes we can
go back and grab the moment. Older pieces are deleted.

If no new video arrives for a while (stalled connection, stream switched tracks...), the
whole pipeline is restarted automatically.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .tools import ffmpeg_exe

SEGMENT_SECONDS = 4
STALL_SECONDS = 150  # no new video for this long -> reconnect (Twitch ad breaks can be ~2 min)
FIRST_DATA_SECONDS = 240  # allowed wait for the very first video after (re)connecting
MAX_FAILED_RESTARTS = 4  # reconnects in a row that produce nothing -> assume the stream ended

# Streamlink quality names, best first
TWITCH_QUALITY = "1080p60,1080p,936p60,900p60,720p60,720p,best"


@dataclass
class Segment:
    path: Path
    start: float  # wall-clock
    end: float


class Recorder:
    def __init__(
        self,
        url: str,
        workdir: Path,
        keep_seconds: float = 300,
        source_file: str | None = None,
        quality: str = "best[height<=1080]/best",
        on_segment: Callable[[Segment], None] | None = None,
        log: Callable[[str], None] = print,
        platform: str = "",
    ):
        self.url = url
        self.workdir = Path(workdir)
        self.keep_seconds = keep_seconds
        self.source_file = source_file
        self.quality = quality
        self.on_segment = on_segment
        self.log = log
        self.platform = platform
        self.segments: list[Segment] = []
        self._lock = threading.Lock()
        self._procs: list[subprocess.Popen] = []
        self._run = 0
        self._started_recording = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.finished = threading.Event()

    # ---- start / stop -------------------------------------------------------------
    def start(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        for old in list(self.workdir.glob("seg*.ts")) + list(self.workdir.glob("segments*.csv")):
            old.unlink(missing_ok=True)
        self._launch()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def _csv(self) -> Path:
        return self.workdir / f"segments_{self._run}.csv"

    def _launch(self) -> None:
        self._csv().unlink(missing_ok=True)
        seg_args = [
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
            "-f", "segment", "-segment_time", str(SEGMENT_SECONDS),
            "-segment_list", self._csv().name, "-segment_list_type", "csv",
            "-segment_list_flags", "+live", f"seg{self._run:03d}_%06d.ts",
        ]
        ff = ffmpeg_exe()
        if self.source_file:
            # Testing mode: play a local file in real time as if it were a live stream
            cmd = [ff, "-hide_banner", "-loglevel", "error", "-re", "-i", str(Path(self.source_file).resolve()), *seg_args]
            self._procs = [subprocess.Popen(cmd, cwd=self.workdir, stdin=subprocess.DEVNULL)]
            return
        if self.platform == "twitch":
            src = [sys.executable, "-m", "streamlink", "--stdout", "--loglevel", "error",
                   "--twitch-low-latency", "--retry-open", "3", self.url, TWITCH_QUALITY]
        else:
            src = [sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "--no-part",
                   "-f", self.quality, "-o", "-", self.url]
        p1 = subprocess.Popen(src, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", *seg_args]
        p2 = subprocess.Popen(cmd, cwd=self.workdir, stdin=p1.stdout)
        p1.stdout.close()  # let ffmpeg own the pipe
        self._procs = [p1, p2]

    def _kill(self) -> None:
        for p in self._procs:
            if p.poll() is None:
                p.terminate()
        for p in self._procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()

    def stop(self) -> None:
        self._stop.set()
        self._kill()

    # ---- segment bookkeeping -----------------------------------------------------
    def _read_new(self, seen: int) -> tuple[int, int]:
        """Pick up segments ffmpeg has finished. Returns (lines seen, new segment count)."""
        csv = self._csv()
        if not csv.exists():
            return seen, 0
        text = csv.read_text(encoding="utf-8", errors="replace")
        # only finished lines: ffmpeg may be halfway through writing the last one
        lines = text.split("\n")[:-1]
        new = []
        for line in lines[seen:]:
            m = re.match(r"^(.+),(-?[\d.e+-]+),(-?[\d.e+-]+)$", line.strip())
            if m:
                new.append((m.group(1), float(m.group(2)), float(m.group(3))))
        # Timing comes from this computer's clock, not the stream's own timestamps:
        # Twitch ad breaks and reconnects make stream timestamps jump or restart.
        end = time.time()
        timed = []
        for name, s, e in reversed(new):
            dur = min(max(e - s, 0.0), 30.0)
            timed.append((name, end - dur, end))
            end -= dur
        for name, start, end in reversed(timed):
            with self._lock:
                if self.segments:
                    start = max(start, self.segments[-1].end)
                    end = max(end, start)
                seg = Segment(self.workdir / name, start, end)
                self.segments.append(seg)
            if not self._started_recording:
                self._started_recording = True
                self.log("[record] stream is recording")
            if self.on_segment:
                try:
                    self.on_segment(seg)
                except Exception as ex:  # noqa: BLE001
                    self.log(f"[record] segment hook failed: {ex}")
        return len(lines), len(new)

    def _watch(self) -> None:
        seen = 0
        last_new = time.time()
        got_any_this_run = False
        failed_restarts = 0
        while not self._stop.is_set():
            seen, n = self._read_new(seen)
            if n:
                last_new = time.time()
                got_any_this_run = True
                failed_restarts = 0
            self._cleanup()

            dead = all(p.poll() is not None for p in self._procs)
            if self.source_file and dead:
                self._read_new(seen)
                self.log("[record] test video finished")
                break
            waited = time.time() - last_new
            stalled = waited > (STALL_SECONDS if got_any_this_run else FIRST_DATA_SECONDS)
            if dead or stalled:
                if not got_any_this_run:
                    failed_restarts += 1
                if failed_restarts >= MAX_FAILED_RESTARTS:
                    self.log("[record] the stream isn't sending video any more. It has probably ended.")
                    break
                why = "connection dropped" if dead else f"no new video for {waited:.0f}s"
                self.log(f"[record] {why}: reconnecting to the stream...")
                self._kill()
                self._read_new(seen)
                self._run += 1
                seen = 0
                got_any_this_run = False
                last_new = time.time()
                if self._stop.wait(3 if dead else 0):
                    break
                self._launch()
                continue
            self._stop.wait(0.5)
        self.finished.set()

    def _cleanup(self) -> None:
        cutoff = time.time() - self.keep_seconds
        with self._lock:
            while self.segments and self.segments[0].end < cutoff:
                seg = self.segments.pop(0)
                try:
                    seg.path.unlink(missing_ok=True)
                except OSError:
                    pass  # Windows may still have it open; next cleanup will get it

    def latest_segment(self) -> Path | None:
        with self._lock:
            return self.segments[-1].path if self.segments else None

    def latest_time(self) -> float:
        with self._lock:
            return self.segments[-1].end if self.segments else 0.0

    def pieces_for(self, t0: float, t1: float) -> tuple[list[Path], float, float] | None:
        """Segments covering [t0, t1], plus where the clip starts inside them and how long it is."""
        with self._lock:
            chosen = [s for s in self.segments if s.end > t0 and s.start < t1]
        if not chosen:
            return None
        start = max(t0, chosen[0].start)
        end = min(t1, chosen[-1].end)
        return [s.path for s in chosen], start - chosen[0].start, end - start


def check_live(url: str, platform: str) -> str:
    """Ask yt-dlp about the link before recording. Returns the stream title, or raises a clear error."""
    import json
    import shutil

    deno_hint = ""
    if platform == "youtube" and not shutil.which("deno"):
        deno_hint = ("\nYouTube needs a helper program called Deno. Install it by running install.bat again "
                     "(or: winget install DenoLand.Deno), then close and reopen this window.")
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "--no-warnings", "-J", "--playlist-items", "1", url],
                       capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        err = [line for line in r.stderr.splitlines() if line.strip()]
        msg = err[-1] if err else "unknown error"
        if "not currently live" in msg.lower() or "is offline" in msg.lower():
            raise RuntimeError("This channel isn't live right now. Try again when it's streaming.")
        raise RuntimeError(f"Couldn't open the stream: {msg}{deno_hint}")
    try:
        info = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError("Couldn't read the stream's details." + deno_hint) from None
    status = info.get("live_status")
    if status == "is_upcoming":
        raise RuntimeError("This stream hasn't started yet (it's scheduled). Run it again once it's live.")
    if status in ("was_live", "not_live", "post_live"):
        raise RuntimeError(
            "This link is a normal video or a finished stream, not a live one.\n"
            "clipwatch 'watch' only works on streams that are live right now.\n"
            "To make a short from a saved video, download it and use:  python -m clipwatch edit video.mp4 --start SECONDS")
    if status != "is_live" and info.get("is_live") is not True:
        raise RuntimeError(f"This doesn't look like a live stream (status: {status})." + deno_hint)
    return info.get("title") or info.get("fulltitle") or url


def write_concat_list(paths: list[Path], dest: Path) -> Path:
    with open(dest, "w", encoding="utf-8") as f:
        for p in paths:
            f.write("file '" + str(Path(p).resolve()).replace("\\", "/").replace("'", "'\\''") + "'\n")
    return dest


def mean_volume_db(path: Path) -> float:
    out = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", os.devnull],
        capture_output=True, text=True, errors="replace",
    ).stderr
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", out)
    return float(m.group(1)) if m else -91.0
